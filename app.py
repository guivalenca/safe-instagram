"""safe-instagram: Instagram without the feed.

A small JSON API for the phone app in static/. There is deliberately no
endpoint for the home feed, Reels, Explore, the Stories tray, suggestions or
notifications, and no setting to turn them on. To get them back you'd have to
write the code.
"""

import datetime as dt
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from instagrapi.exceptions import (
    BadPassword,
    ClientError,
    ClientThrottledError,
    FeedbackRequired,
    NotFoundError,
    PleaseWaitFewMinutes,
    PrivateAccount,
    RateLimitError,
)
import requests
from instagrapi import config as ig_config
from instagrapi.extractors import extract_media_v1, extract_user_v1
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from ig import (
    DATA,
    IG,
    CoolingDown,
    DailyLimit,
    LoginFlow,
    NotConnected,
    SessionExpired,
    read_json,
    write_private_json,
)

register_heif_opener()
log = logging.getLogger("uvicorn.error")

HERE = Path(__file__).parent
STATIC = HERE / "static"
PAGE_SIZE = 12  # posts per page on a profile; there is no infinite scroll

PASSCODE = os.environ.get("APP_PASSCODE", "")
if len(PASSCODE) < 6:
    raise SystemExit("Set APP_PASSCODE (at least 6 characters) - it's what unlocks the app.")


def secret_key() -> str:
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = DATA / "secret.json"
    key = read_json(path).get("key")
    if not key:
        key = secrets.token_hex(32)
        write_private_json(path, {"key": key})
    return key


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
ig = IG()
login_flow = LoginFlow(ig)


# -- unlocking the app ------------------------------------------------------

OPEN_PATHS = ("/api/unlock", "/api/status")
failures = []  # timestamps of wrong passcodes


@app.middleware("http")
async def require_unlock(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and not path.startswith(OPEN_PATHS) and not request.session.get("unlocked"):
        return JSONResponse({"error": "locked"}, status_code=401)
    return await call_next(request)


# Added after require_unlock so it wraps it (the last middleware added runs first).
app.add_middleware(
    SessionMiddleware,
    secret_key=secret_key(),
    max_age=60 * 60 * 24 * 180,
    same_site="strict",
    https_only=bool(os.environ.get("RAILWAY_ENVIRONMENT")),
)


class Passcode(BaseModel):
    passcode: str


@app.post("/api/unlock")
def unlock(request: Request, body: Passcode):
    now = time.time()
    failures[:] = [t for t in failures if now - t < 15 * 60]
    if len(failures) >= 8:
        return JSONResponse({"error": "too_many", "message": "Too many tries. Wait 15 minutes."}, status_code=429)
    if hmac.compare_digest(body.passcode.encode(), PASSCODE.encode()):
        request.session["unlocked"] = True
        return {"ok": True}
    failures.append(now)
    time.sleep(1)
    return JSONResponse({"error": "wrong", "message": "Wrong passcode."}, status_code=401)


@app.post("/api/lock")
def lock(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/status")
def status(request: Request):
    unlocked = bool(request.session.get("unlocked"))
    connected = unlocked and ig.connected()
    return {
        "unlocked": unlocked,
        "connected": connected,
        "username": ig.username if connected else None,
        "user_id": ig.user_id if connected else None,
        "cooldown": ig.cooldown() if unlocked else None,
    }


# -- connecting Instagram ----------------------------------------------------


class Login(BaseModel):
    username: str = ""
    password: str = ""
    code: str = ""  # authenticator code up front, if you use one: saves a second login attempt
    sessionid: str = ""
    timezone: str = "UTC"
    locale: str = ""


class Code(BaseModel):
    code: str


@app.post("/api/connect")
def connect(body: Login):
    if body.sessionid:
        return login_flow.with_sessionid(body.sessionid, body.timezone, body.locale)
    return login_flow.start(body.username, body.password, body.code, body.timezone, body.locale)


@app.post("/api/connect/code")
def connect_code(body: Code):
    return login_flow.submit_code(body.code)


@app.post("/api/disconnect")
def disconnect():
    ig.disconnect()
    return {"ok": True}


# -- errors ------------------------------------------------------------------


def err(status: int, error: str, message: str, **extra):
    return JSONResponse({"error": error, "message": message, **extra}, status_code=status)


@app.exception_handler(NotConnected)
def not_connected(request, exc):
    return err(409, "not_connected", "Connect your Instagram account.")


@app.exception_handler(SessionExpired)
def session_expired(request, exc):
    return err(409, "not_connected", "Instagram ended the session. Log in again.")


@app.exception_handler(CoolingDown)
def cooling_down(request, exc: CoolingDown):
    return err(
        429,
        "cooldown",
        f"{exc.reason}. Pausing until {exc.until:%H:%M} so your account stays safe.",
        until=exc.until.isoformat(),
    )


@app.exception_handler(DailyLimit)
def daily_limit(request, exc: DailyLimit):
    return err(429, "limit", f"That's the limit for {exc.what} {exc.per}. It keeps your account safe.")


@app.exception_handler(ClientError)
def instagram_error(request, exc: ClientError):
    if isinstance(exc, NotFoundError):
        return err(404, "not_found", "That doesn't exist anymore.")
    if isinstance(exc, PrivateAccount):
        return err(403, "private", "This account is private.")
    if isinstance(exc, (FeedbackRequired, PleaseWaitFewMinutes, RateLimitError, ClientThrottledError)):
        return err(429, "cooldown", "Instagram asked us to slow down. Pausing for a while.")
    if isinstance(exc, BadPassword):
        return err(400, "bad_password", "Wrong password.")
    log.warning("instagram error on %s: %s: %s", request.url.path, type(exc).__name__, str(exc)[:200])
    return err(502, "instagram", str(exc) or type(exc).__name__)


# -- turning Instagram objects into small JSON --------------------------------


def s(v):
    return str(v) if v else None


def user_short(u):
    return {
        "id": str(u.pk),
        "username": u.username,
        "name": u.full_name or "",
        "pic": s(u.profile_pic_url),
        "verified": bool(u.is_verified),
        "private": bool(u.is_private),
    }


def small_image(m):
    """Grid-sized image (~320-640px) instead of the full 1080px one."""
    versions = getattr(m, "image_versions2", None)
    cands = [c for c in (versions.candidates if versions else []) if getattr(c, "width", 0)]
    if cands:
        fit = sorted((c for c in cands if c.width >= 320), key=lambda c: c.width)
        return s((fit or sorted(cands, key=lambda c: -c.width))[0].url)
    if m.thumbnail_url:
        return s(m.thumbnail_url)
    if getattr(m, "resources", None):
        return s(m.resources[0].thumbnail_url)
    return None


def media_tile(m):
    return {"id": str(m.pk), "thumb": small_image(m), "type": m.media_type}


def media_full(m):
    if m.media_type == 8:
        items = [{"type": r.media_type, "img": s(r.thumbnail_url), "video": s(r.video_url)} for r in m.resources]
    else:
        items = [{"type": m.media_type, "img": s(m.thumbnail_url), "video": s(m.video_url)}]
    dims = m.dimensions
    if not (dims and dims.width and dims.height):
        cands = getattr(getattr(m, "image_versions2", None), "candidates", None) or []
        dims = next((c for c in cands if getattr(c, "width", 0) and getattr(c, "height", 0)), None)
    return {
        "id": str(m.pk),
        "media_id": m.id,
        "code": m.code,
        "user": user_short(m.user),
        "caption": m.caption_text or "",
        "taken_at": m.taken_at.isoformat(),
        "liked": bool(m.has_liked),
        "saved": bool(m.viewer_has_saved),
        "likes": None if m.like_and_view_counts_disabled else m.like_count,
        "comments": m.comment_count or 0,
        "ratio": (dims.width / dims.height) if dims and dims.width and dims.height else None,
        "items": items,
    }


def story_json(st):
    return {
        "id": str(st.pk),
        "media_id": st.id,
        "owner": str(st.user.pk) if st.user else None,
        "type": st.media_type,
        "img": s(st.thumbnail_url),
        "video": s(st.video_url),
        "duration": st.video_duration or 0,
        "taken_at": st.taken_at.isoformat(),
    }


CODE_RE = re.compile(r"instagram\.com/(?:[\w.]+/)?(?:p|reel|reels|tv)/([\w-]+)")


def code_from(url) -> str | None:
    m = CODE_RE.search(str(url or ""))
    return m.group(1) if m else None


def shared_media(m, label):
    return {"label": label, "user": m.user.username if m.user else "", "img": small_image(m), "media": str(m.pk)}


def message_json(m, my_id):
    out = {
        "id": m.id,
        "mine": bool(m.is_sent_by_viewer) or str(m.user_id) == my_id,
        "user_id": str(m.user_id) if m.user_id else None,
        "t": m.timestamp.isoformat(),
        "kind": m.item_type or "unknown",
        "text": None,
        "context": m.client_context,
        "reactions": [
            {"emoji": r.emoji, "mine": str(r.sender_id) == my_id} for r in (m.reactions.emojis if m.reactions else [])
        ],
    }
    if m.item_type == "text":
        out["text"] = m.text
    elif m.item_type == "link":
        out["text"] = m.link.text if m.link else m.text
    elif m.item_type == "like":
        out["text"] = "❤️"
    elif m.media_share:
        out["share"] = shared_media(m.media_share, "Post")
    elif m.clip:
        out["share"] = shared_media(m.clip, "Reel")
    elif m.xma_share or m.generic_xma:
        x = m.xma_share or m.generic_xma[0]
        out["share"] = {
            "label": x.header_title_text or x.title or "Shared post",
            "img": x.preview_url,
            "code": code_from(x.video_url),
        }
    elif m.media:
        out["media"] = {"img": s(m.media.thumbnail_url), "video": s(m.media.video_url), "audio": s(m.media.audio_url)}
    elif m.story_share:
        out["note"] = "Shared a story"
        out["text"] = m.story_share.get("message") or None
    elif m.reel_share:
        out["note"] = "Replied to a story"
        out["text"] = m.reel_share.get("text") or None
    elif m.placeholder:
        out["note"] = m.placeholder.get("message") or "Unavailable"
    else:
        out["text"] = m.text
        if not m.text:
            out["note"] = (m.item_type or "message").replace("_", " ")
    return out


def thread_json(t, my_id):
    last = max(t.messages, key=lambda m: m.timestamp) if t.messages else None
    return {
        "id": t.id,
        "title": t.thread_title or ", ".join(u.username for u in t.users),
        "users": [user_short(u) for u in t.users],
        "group": t.is_group,
        "pending": t.pending,
        "unread": not t.is_seen(my_id),
        "at": t.last_activity_at.isoformat(),
        "last": message_json(last, my_id) if last else None,
    }


# -- search & profiles -------------------------------------------------------


@app.get("/api/search")
def search(q: str = ""):
    q = q.strip().lstrip("@")
    if not q:
        return []
    users = ig.cached(f"search:{q.lower()}", 300, lambda cl: cl.search_users(q))
    return [user_short(u) for u in users[:25]]


def user_and_friendship(cl, username):
    """One request: the profile response already carries our relationship to them."""
    raw = cl.private_request(f"users/{username}/usernameinfo/")["user"]
    fs = raw.get("friendship_status") or {}
    user = extract_user_v1(dict(raw))
    if "following" not in fs:  # not included this time: ask separately
        r = cl.user_friendship_v1(user.pk)
        fs = {
            "following": r.following,
            "followed_by": r.followed_by,
            "outgoing_request": r.outgoing_request,
            "incoming_request": r.incoming_request,
            "muting": r.muting,
            "is_muting_reel": r.is_muting_reel,
        }
    return user, fs


@app.get("/api/u/{username}")
def profile(username: str):
    u, fs = ig.cached(f"user:{username.lower()}", 120, user_and_friendship, username)
    me = str(u.pk) == ig.user_id
    rel = None if me else SimpleNamespace(**{k: bool(fs.get(k)) for k in ("following", "followed_by", "outgoing_request", "incoming_request", "muting", "is_muting_reel")})
    hd = s(u.profile_pic_url_hd)
    return {
        **user_short(u),
        # Instagram marks HD profile photos "same-origin only", so Safari won't show
        # them from instagram's CDN directly; they go through /api/img instead.
        "pic_hd": f"/api/img?u={quote(hd, safe='')}" if hd else s(u.profile_pic_url),
        "bio": u.biography or "",
        "url": u.external_url,
        "posts": u.media_count,
        "followers": u.follower_count,
        "following": u.following_count,
        "me": me,
        "rel": None
        if me
        else {
            "following": rel.following,
            "followed_by": rel.followed_by,
            "requested": rel.outgoing_request,
            "incoming": rel.incoming_request,  # they asked to follow you
            "muted_posts": rel.muting,
            "muted_stories": rel.is_muting_reel,
        },
        "can_see": me or not u.is_private or rel.following,
    }


@app.get("/api/u/{user_id}/posts")
def posts(user_id: str, cursor: str = ""):
    medias, next_cursor = ig.cached(
        f"posts:{user_id}:{cursor}",
        300,
        lambda cl: cl.user_medias_paginated_v1(user_id, PAGE_SIZE, end_cursor=cursor),
        soft=True,
    )
    return {"items": [media_tile(m) for m in medias], "next": next_cursor or None}


@app.get("/api/u/{user_id}/stories")
def stories(user_id: str):
    items = ig.cached(f"stories:{user_id}", 120, lambda cl: cl.user_stories_v1(user_id), soft=True)
    return [story_json(st) for st in sorted(items, key=lambda st: st.taken_at)]


def highlight_cover(h):
    c = h.cover_media or {}
    url = (c.get("cropped_image_version") or {}).get("url") or (c.get("full_image_version") or {}).get("url")
    return url


@app.get("/api/u/{user_id}/highlights")
def highlights(user_id: str):
    items = ig.cached(f"highlights:{user_id}", 600, lambda cl: cl.user_highlights_v1(user_id), soft=True)
    return [{"id": h.pk, "title": h.title, "cover": highlight_cover(h)} for h in items]


@app.get("/api/highlight/{highlight_id}")
def highlight(highlight_id: str):
    h = ig.cached(f"highlight:{highlight_id}", 600, lambda cl: cl.highlight_info_v1(highlight_id), soft=True)
    return {"id": h.pk, "title": h.title, "items": [story_json(st) for st in h.items]}


class StoryLike(BaseModel):
    media_id: str
    like: bool


@app.post("/api/stories/like")
def story_like(body: StoryLike):
    ig.run(lambda cl: cl.story_like(body.media_id, revert=not body.like, mark_seen=False), kind="like")
    return {"liked": body.like}


class StoryReply(BaseModel):
    media_id: str
    owner: str
    text: str
    video: bool = False


def send_story_reply(cl, media_id: str, owner: str, text: str, video: bool):
    """A reply that shows the story it's answering, like the official app's.
    instagrapi has no helper for this, so this is the app's own reel_share call."""
    token = cl.generate_mutation_token()
    data = {
        "action": "send_item",
        "is_shh_mode": "0",
        "send_attribution": "reel_feed_timeline",
        "client_context": token,
        "mutation_token": token,
        "offline_threading_id": token,
        "nav_chain": "1qT:feed_timeline:1,ReelViewerFragment:reel_feed_timeline:4",
        "recipient_users": json.dumps([[int(owner)]]),
        "media_id": media_id,
        "reel_id": str(owner),
        "text": text,
        "entry": "reel",
        "media_type": "video" if video else "photo",
        "containermodule": "reel_feed_timeline",
    }
    return cl.private_request("direct_v2/threads/broadcast/reel_share/", data=cl.with_default_data(data), with_signature=False)


@app.post("/api/stories/reply")
def story_reply(body: StoryReply):
    text = body.text.strip()
    try:
        ig.run(send_story_reply, body.media_id, body.owner, text, body.video, kind="dm", soft=True)
        linked = True
    except ClientError:
        # If Instagram won't take a story reply, still get the words to them.
        ig.run(lambda cl: cl.direct_send(text, user_ids=[int(body.owner)]), kind="dm")
        linked = False
    ig.forget("inbox:")
    return {"ok": True, "linked": linked}


class Ids(BaseModel):
    ids: list[str]


@app.post("/api/stories/seen")
def stories_seen(body: Ids):
    if body.ids:
        ig.run(lambda cl: cl.story_seen([int(i) for i in body.ids]), kind="seen")
    return {"ok": True}


class Follow(BaseModel):
    follow: bool


@app.post("/api/u/{user_id}/follow")
def follow(user_id: str, body: Follow):
    if body.follow:
        ig.run(lambda cl: cl.user_follow(user_id), kind="follow")
    else:
        ig.run(lambda cl: cl.user_unfollow(user_id), kind="follow")
    ig.forget("user:")
    rel = ig.run(lambda cl: cl.user_friendship_v1(user_id))
    return {"following": rel.following, "followed_by": rel.followed_by, "requested": rel.outgoing_request}


@app.get("/api/img")
def image(u: str):
    host = urlparse(u).hostname or ""
    if not u.startswith("https://") or not (host.endswith(".cdninstagram.com") or host.endswith(".fbcdn.net")):
        return err(400, "bad_url", "Not an Instagram image.")
    r = requests.get(u, timeout=10)  # Instagram's public CDN; no account involved
    return Response(
        r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=86400"},
    )


# -- today: posts and stories from people you follow, 5 minutes once a day -----

DAILY_MINUTES = 8
DAILY_MAX_AGE = dt.timedelta(days=7)  # older posts are skipped


def local_now() -> dt.datetime:
    try:
        tz = ZoneInfo(ig.state().get("timezone") or "UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    return dt.datetime.now(tz)


def daily_status() -> dict:
    now = local_now()
    tomorrow = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    base = {"minutes": DAILY_MINUTES, "resets_in": int((tomorrow - now).total_seconds())}
    d = ig.state().get("daily") or {}
    if d.get("date") != now.date().isoformat():
        return {**base, "status": "ready"}
    left = (dt.datetime.fromisoformat(d["ends"]) - dt.datetime.now(dt.timezone.utc)).total_seconds()
    if left > 0:
        return {**base, "status": "open", "remaining": left}
    return {**base, "status": "done"}


class DailyOver(Exception):
    pass


@app.exception_handler(DailyOver)
def daily_over(request, exc):
    return err(403, "daily_over", f"That's today's {DAILY_MINUTES} minutes. See you tomorrow.")


def require_daily_open():
    if daily_status()["status"] != "open":
        raise DailyOver()


@app.get("/api/daily")
def daily():
    return daily_status()


@app.post("/api/daily/start")
def daily_start():
    st = daily_status()
    if st["status"] == "ready":
        ends = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=DAILY_MINUTES)
        ig.update_state(daily={"date": local_now().date().isoformat(), "ends": ends.isoformat()})
        st = daily_status()
    return st


def raw_user(u: dict) -> dict:
    return {
        "id": str(u.get("pk") or u.get("pk_id") or u.get("id")),
        "username": u.get("username", ""),
        "name": u.get("full_name") or "",
        "pic": u.get("profile_pic_url"),
        "verified": bool(u.get("is_verified")),
        "private": bool(u.get("is_private")),
    }


# Fields that mark an ad or a recommendation. They only count when they have a value:
# ordinary posts often carry some of them empty.
PROMO_FIELDS = ("ad_id", "ad_action", "ad_metadata", "injected", "is_ad", "dr_ad_type", "is_paid_partnership")
SUGGESTION_FIELDS = ("explore_context", "is_recommended", "recommendation_data", "suggested_users")


def feed_verdict(media: dict) -> str:
    """'keep', or why a timeline item is dropped: ads, suggestions, accounts you don't follow."""
    if any(media.get(k) for k in PROMO_FIELDS):
        return "ad"
    if any(media.get(k) for k in SUGGESTION_FIELDS):
        return "suggested"
    fs = (media.get("user") or {}).get("friendship_status")
    if fs is None:
        return "keep_no_fs"  # Instagram didn't say; only people you follow are in the timeline otherwise
    if fs.get("muting"):
        return "muted"
    return "keep" if fs.get("following") else "not_following"


def timeline_page(cl, max_id, source):
    """One feed/timeline request.

    instagrapi always paginates with pagination_source=feed_recs, which is the
    "Suggested posts" list; "Older posts" (what you've already seen) needs its own
    source. It also reports every post it has fetched as fully viewed, which takes
    them out of your feed in the official app too, so send no views.
    """
    if source and source != "feed_recs":
        send = cl.private_request

        def private_request(endpoint, data=None, *args, **kwargs):
            if endpoint == "feed/timeline/":
                data = json.dumps({**json.loads(data), "pagination_source": source})
            return send(endpoint, data, *args, **kwargs)

        cl.private_request = private_request
    try:  # with a max_id, instagrapi switches the reason to "pagination"
        return cl.get_timeline_feed("cold_start_fetch", max_id=max_id, seen_posts=[])
    finally:
        cl.__dict__.pop("private_request", None)


def page_medias(raw: dict, tally: Counter):
    """The posts in a timeline page, and the cursor for "Older posts" if it offers one.

    Past "You're all caught up", Instagram nests the posts you've already seen
    in a group of the end-of-feed module instead of the main list.
    """
    medias, past = [], None
    for it in raw.get("feed_items") or []:
        if it.get("media_or_ad"):
            medias.append(it["media_or_ad"])
            continue
        tally.update(f"module:{k}" for k in it)
        groups = ((it.get("end_of_feed_demarcator") or {}).get("group_set") or {}).get("groups") or []
        for g in groups:
            tally[f"group:{g.get('id')}"] += 1
            if g.get("id") != "past_posts":
                continue
            for x in g.get("feed_items") or []:
                media = x.get("media_or_ad") or (x if x.get("media_type") else None)
                if media:
                    tally["past_posts"] += 1
                    medias.append(media)
            if g.get("next_max_id"):
                past = (g["next_max_id"], g.get("pagination_source") or "past_posts")
    return medias, past


def daily_page(day: str, cursor: tuple | None):
    """One timeline page, filtered, and where to go next. Instagram sends ~5 posts a page."""
    max_id, source = cursor or (None, None)
    # Later pages are extras: a refusal there shouldn't count as trouble.
    raw = ig.cached(f"daily:feed:{day}:{source}:{max_id}", 600, timeline_page, max_id, source, soft=bool(cursor))
    cutoff = dt.datetime.now(dt.timezone.utc) - DAILY_MAX_AGE
    items, tally = [], Counter()
    medias, past = page_medias(raw, tally)
    for media in medias:
        verdict = feed_verdict(media)
        tally[verdict] += 1
        if not verdict.startswith("keep"):
            continue
        try:
            m = media_full(extract_media_v1(dict(media)))
        except Exception:
            tally["unparsed"] += 1
            continue
        if not m["ratio"] and media.get("original_width") and media.get("original_height"):
            m["ratio"] = media["original_width"] / media["original_height"]
        if dt.datetime.fromisoformat(m["taken_at"]) < cutoff:
            tally["too_old"] += 1
            continue
        items.append(m)
    # Once Instagram offers older posts from people you follow, follow those
    # rather than the main list, which carries on into suggestions.
    more = raw.get("more_available") and raw.get("next_max_id")
    return items, past or ((more, source or "feed_recs") if more else None), tally


POOL_FILE = DATA / "daily_pool.json"
POOL_LOCK = threading.Lock()  # the feed and likes/saves both rewrite the pool
SCAN_MAX_PAGES = 8


def pool_update(media_pk: str, **fields):
    """Keep likes/saves made from Today in the stored copy."""
    with POOL_LOCK:
        pool = read_json(POOL_FILE)
        if media_pk in pool:
            pool[media_pk]["m"].update(fields)
            write_private_json(POOL_FILE, pool)


@app.get("/api/daily/feed")
def daily_feed():
    """Everything Instagram's feed has from people you follow, newest first.

    Instagram's own chronological "Following" feed isn't reachable here, and its
    regular feed drifts into suggestions and repeats after a page or two. So read
    pages until nothing new turns up, and remember posts for a week so they don't
    vanish between visits.
    """
    require_daily_open()
    day = local_now().date().isoformat()
    found, tally, cursor, pages, dry = {}, Counter(), None, 0, 0
    while pages < SCAN_MAX_PAGES:
        try:
            page, cursor, t = daily_page(day, cursor)
        except ClientError as e:
            if not pages:
                raise
            tally[f"failed:{type(e).__name__}"] += 1
            break
        pages += 1
        tally.update(t)
        new = [m for m in page if m["id"] not in found]
        for m in page:
            found[m["id"]] = m
        dry = 0 if new else dry + 1
        if not cursor or dry >= 2:
            break

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - DAILY_MAX_AGE
    with POOL_LOCK:
        pool = {k: v for k, v in read_json(POOL_FILE).items() if dt.datetime.fromisoformat(v["m"]["taken_at"]) >= cutoff}
        for pk, m in found.items():
            first = pool.get(pk, {}).get("first_seen", now.isoformat())
            pool[pk] = {"m": m, "first_seen": first}
        write_private_json(POOL_FILE, pool)

    items = sorted((v["m"] for v in pool.values()), key=lambda m: m["taken_at"], reverse=True)
    # Counts only, no content: lets us tune this from the logs.
    log.info("daily feed pages=%d found=%d pool=%d %s", pages, len(found), len(items), dict(tally))
    return {"items": items}


def reels_media(cl, user_ids):
    """Several people's stories in one private request (as highlight_info_v1 does)."""
    data = {
        "exclude_media_ids": "[]",
        "supported_capabilities_new": json.dumps(ig_config.SUPPORTED_CAPABILITIES),
        "source": "reel_feed_timeline",
        "_uid": str(cl.user_id),
        "_uuid": cl.uuid,
        "user_ids": [str(i) for i in user_ids],
    }
    return list((cl.private_request("feed/reels_media/", data).get("reels") or {}).values())


def tray_entry(reel):
    u = reel.get("user") or {}
    fs = u.get("friendship_status") or {}
    if reel.get("reel_type", "user_reel") != "user_reel" or fs.get("following") is False or not u.get("username"):
        return None
    if reel.get("muted") or fs.get("is_muting_reel"):  # stories you've muted
        return None
    if str(u.get("pk") or u.get("pk_id")) == ig.user_id:
        return None
    unseen = (reel.get("seen") or 0) < (reel.get("latest_reel_media") or 0)
    return {**raw_user(u), "unseen": unseen}


@app.get("/api/daily/stories")
def daily_stories():
    require_daily_open()
    day = local_now().date().isoformat()
    raw = ig.cached(f"daily:tray:{day}", 600, lambda cl: cl.get_reels_tray_feed("cold_start"))
    reels = list(raw.get("tray") or [])
    # The tray's first page is new stories only; the people you've already
    # watched come as bare ids. Fetch up to 100 of them so you can rewatch.
    rest = [r for r in raw.get("remaining_reel_ids_to_fetch") or [] if str(r).isdigit()][:100]
    for i in range(0, len(rest), 50):
        chunk = rest[i : i + 50]
        try:  # an extra: if it fails, still show the new ones
            reels += ig.cached(f"daily:reels:{day}:{i}", 600, reels_media, chunk, soft=True)
        except ClientError as e:
            log.warning("daily tray: reels_media failed: %s: %s", type(e).__name__, str(e)[:200])
            break
    out, seen_ids = [], set()
    for reel in reels:
        e = tray_entry(reel)
        if e and e["id"] not in seen_ids:
            seen_ids.add(e["id"])
            out.append(e)
    out.sort(key=lambda x: not x["unseen"])  # new ones first, like the real app
    log.info("daily tray first=%d rest=%d shown=%d unseen=%d", len(raw.get("tray") or []), len(rest), len(out), sum(x["unseen"] for x in out))
    return out


# -- your saved and liked posts --------------------------------------------------


@app.get("/api/me/{which}")
def my_collection(which: str, cursor: str = ""):
    if which not in ("saved", "liked"):
        return err(404, "not_found", "No such list.")
    medias, nxt = ig.cached(
        f"mine:{which}:{cursor}",
        120,
        lambda cl: cl.collection_medias_v1_chunk("liked" if which == "liked" else "saved", max_id=cursor),
    )
    return {"items": [media_tile(m) for m in medias], "next": nxt or None}


# -- follow requests -----------------------------------------------------------


@app.get("/api/requests")
def follow_requests():
    users, _ = ig.cached("requests", 30, lambda cl: cl.user_follow_requests_chunk(50))
    return [user_short(u) for u in users]


class Decision(BaseModel):
    approve: bool


@app.post("/api/requests/{user_id}")
def answer_request(user_id: str, body: Decision):
    if body.approve:
        ig.run(lambda cl: cl.user_follow_request_approve(user_id), kind="approve")
    else:
        ig.run(lambda cl: cl.user_follow_request_decline(user_id), kind="approve")
    ig.forget("requests", "user:")
    return {"ok": True}


# -- single posts ------------------------------------------------------------


@app.get("/api/p/{media_pk}")
def post(media_pk: str):
    return media_full(ig.cached(f"media:{media_pk}", 120, lambda cl: cl.media_info_v1(media_pk)))


@app.get("/api/c/{code}")
def post_by_code(code: str):
    """Shared links (instagram.com/p/CODE, /reel/CODE) -> a media id."""
    return {"id": str(ig.cached(f"code:{code}", 86400, lambda cl: cl.media_pk_from_code(code)))}


class Mute(BaseModel):
    posts: bool | None = None
    stories: bool | None = None


@app.post("/api/u/{user_id}/mute")
def mute(user_id: str, body: Mute):
    if body.posts is not None:
        ig.run(lambda cl: cl.mute_posts_from_follow(user_id, revert=not body.posts), kind="other")
    if body.stories is not None:
        ig.run(lambda cl: cl.mute_stories_from_follow(user_id, revert=not body.stories), kind="other")
    ig.forget("user:", "daily:")
    return {"ok": True}


@app.get("/api/p/{media_pk}/comments")
def comments(media_pk: str, media_id: str, cursor: str = ""):
    items, nxt = ig.cached(
        f"comments:{media_pk}:{cursor}",
        120,
        lambda cl: cl.media_comments_chunk(media_id, 20, min_id=cursor or None),
        soft=True,
    )
    return {
        "items": [
            {
                "id": c.pk,
                "user": user_short(c.user),
                "text": c.text,
                "t": c.created_at_utc.isoformat(),
                "likes": c.like_count or 0,
                "reply": bool(c.replied_to_comment_id),
            }
            for c in items
        ],
        "next": nxt or None,
    }


class Save(BaseModel):
    media_id: str
    save: bool


@app.post("/api/p/{media_pk}/save")
def save(media_pk: str, body: Save):
    ig.run(lambda cl: cl.media_save(body.media_id, revert=not body.save), kind="like")
    ig.forget(f"media:{media_pk}", "mine:saved")
    pool_update(media_pk, saved=body.save)
    return {"saved": body.save}


class Share(BaseModel):
    media_id: str
    video: bool = False
    thread_id: str | None = None
    user_id: str | None = None


@app.post("/api/p/{media_pk}/share")
def share(media_pk: str, body: Share):
    kind = "video" if body.video else "photo"
    if body.thread_id:
        ig.run(lambda cl: cl.direct_media_share(body.media_id, thread_ids=[int(body.thread_id)], media_type=kind), kind="dm")
    else:
        ig.run(lambda cl: cl.direct_media_share(body.media_id, user_ids=[int(body.user_id)], media_type=kind), kind="dm_new")
    ig.forget("inbox:")
    return {"ok": True}


class Like(BaseModel):
    media_id: str
    like: bool


@app.post("/api/p/{media_pk}/like")
def like(media_pk: str, body: Like):
    ig.run(lambda cl: cl.media_like(body.media_id, revert=not body.like), kind="like")
    ig.forget(f"media:{media_pk}", "mine:liked")
    pool_update(media_pk, liked=body.like)
    return {"liked": body.like}


# -- direct messages ---------------------------------------------------------


@app.get("/api/dm")
def inbox(requests: bool = False):
    my_id = ig.user_id
    if requests:
        threads = ig.cached("inbox:requests", 10, lambda cl: cl.direct_pending_inbox(20))
    else:
        # 20 threads = one page; we only show each thread's latest message.
        threads = ig.cached("inbox:main", 10, lambda cl: cl.direct_threads(20, thread_message_limit=1))
    return [thread_json(t, my_id) for t in threads]


@app.get("/api/dm/{thread_id}")
def thread(thread_id: str, n: int = 20):  # Instagram pages messages by 20
    n = max(10, min(n, 200))
    my_id = ig.user_id
    t = ig.cached(f"thread:{thread_id}:{n}", 4, lambda cl: cl.direct_thread(int(thread_id), amount=n))
    messages = sorted(t.messages, key=lambda m: m.timestamp)
    if messages and not messages[-1].is_sent_by_viewer and not t.pending and not t.is_seen(my_id):
        ig.run(lambda cl: cl.direct_send_seen(int(thread_id)), kind="seen")
        ig.forget("inbox:")
    return {**thread_json(t, my_id), "messages": [message_json(m, my_id) for m in messages], "n": n}


class React(BaseModel):
    message_id: str
    emoji: str = "❤️"
    remove: bool = False
    context: str | None = None


@app.post("/api/dm/{thread_id}/react")
def react(thread_id: str, body: React):
    if body.remove:
        ig.run(lambda cl: cl.direct_delete_reaction(int(thread_id), int(body.message_id), body.emoji, client_context=body.context), kind="dm")
    else:
        ig.run(
            lambda cl: cl.direct_send_reaction(int(thread_id), int(body.message_id), body.emoji, client_context=body.context),
            kind="dm",
        )
    ig.forget(f"thread:{thread_id}:")
    return {"ok": True}


class Send(BaseModel):
    text: str
    accept: bool = False


@app.post("/api/dm/{thread_id}/send")
def send(thread_id: str, body: Send):
    if body.accept:
        ig.run(lambda cl: cl.direct_pending_approve(int(thread_id)), kind="other")
    msg = ig.run(lambda cl: cl.direct_answer(int(thread_id), body.text.strip()), kind="dm")
    ig.forget(f"thread:{thread_id}:", "inbox:")
    return message_json(msg, ig.user_id)


@app.post("/api/dm/to/{user_id}")
def send_to_user(user_id: str, body: Send):
    msg = ig.run(lambda cl: cl.direct_send(body.text.strip(), user_ids=[int(user_id)]), kind="dm_new")
    ig.forget("inbox:")
    return {"thread_id": str(msg.thread_id) if msg.thread_id else None}


# -- posting -----------------------------------------------------------------


def prepare_photo(upload: UploadFile, for_story: bool) -> Path:
    img = Image.open(upload.file)
    img = ImageOps.exif_transpose(img).convert("RGB")
    if not for_story:
        # Feed photos must be between 4:5 (portrait) and 1.91:1 (landscape).
        w, h = img.size
        if w / h < 0.8:
            new_h = int(w / 0.8)
            top = (h - new_h) // 2
            img = img.crop((0, top, w, top + new_h))
        elif w / h > 1.91:
            new_w = int(h * 1.91)
            left = (w - new_w) // 2
            img = img.crop((left, 0, left + new_w, h))
    img.thumbnail((1440, 2560 if for_story else 1800))
    fd, name = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    img.save(name, "JPEG", quality=92)
    return Path(name)


@app.post("/api/post")
def new_post(photo: UploadFile = File(...), caption: str = Form(""), where: str = Form("feed")):
    for_story = where == "story"
    path = prepare_photo(photo, for_story)
    try:
        if for_story:
            ig.run(lambda cl: cl.photo_upload_to_story(path), kind="upload")
        else:
            ig.run(lambda cl: cl.photo_upload(path, caption), kind="upload")
    finally:
        path.unlink(missing_ok=True)
    ig.forget("posts:", "stories:", "user:")
    return {"ok": True}


# -- the phone app -------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/{path:path}")
def spa(path: str):
    if path.startswith("api/"):
        return err(404, "not_found", "No such endpoint.")
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
