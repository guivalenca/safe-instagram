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
    ClientNotFoundError,
    ClientThrottledError,
    FeedbackRequired,
    MediaNotFound,
    NotFoundError,
    PleaseWaitFewMinutes,
    PrivateAccount,
    RateLimitError,
)
import requests
from instagrapi import config as ig_config
from instagrapi.extractors import extract_media_v1, extract_story_v1, extract_user_short, extract_user_v1
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
    if isinstance(exc, (NotFoundError, ClientNotFoundError)):
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


SOCIAL_KEYS_LOGGED = set()


def social(raw: dict, where: str) -> str | None:
    """Instagram's "Followed by ana, joão and 3 others" line, or a count if that's all it sent."""
    keys = sorted(k for k in raw if "mutual" in k or "context" in k)
    if where not in SOCIAL_KEYS_LOGGED:  # field names only: lets us see what each endpoint carries
        SOCIAL_KEYS_LOGGED.add(where)
        log.info("social fields on %s: %s", where, keys)
    for k in ("profile_context", "social_context"):
        if isinstance(raw.get(k), str) and raw[k].strip():
            return raw[k].strip()
    n = raw.get("mutual_followers_count")
    if isinstance(n, int) and n > 0:
        return f"{n} follower{'s' if n != 1 else ''} you know"
    return None


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


def media_from_raw(raw: dict):
    """extract_media_v1, keeping whether you saved it: the private API calls that
    `has_viewer_saved`, which instagrapi doesn't read."""
    raw = dict(raw)
    raw.setdefault("viewer_has_saved", bool(raw.get("has_viewer_saved")))
    return extract_media_v1(raw)


def media_info(cl, media_pk):
    try:
        items = cl.private_request(f"media/{media_pk}/info/").get("items") or []
    except ClientError as e:  # a deleted post: "Media not found or unavailable" (not trouble, as in media_info_v1)
        if "not found" in str(e).lower() or "unavailable" in str(e).lower():
            raise MediaNotFound(media_pk=media_pk) from e
        raise
    if not items:
        raise MediaNotFound(media_pk=media_pk)
    return media_from_raw(items[0])


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
        "mine": is_mine(m, my_id),
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


def is_mine(m, my_id) -> bool:
    return bool(m.is_sent_by_viewer) or str(m.user_id) == my_id


def thread_unread(t, my_id) -> bool:
    """A message from someone else arrived after the one you last saw.

    (instagrapi's is_seen() calls a thread unread whenever the other person
    looked at it after you did, e.g. after reading your reply.)
    """
    last = max(t.messages, key=lambda m: m.timestamp) if t.messages else None
    if not last or is_mine(last, my_id):
        return False
    seen = t.last_seen_at.get(str(my_id))
    return not seen or (seen.item_id != last.id and last.timestamp >= seen.timestamp)


def thread_json(t, my_id):
    last = max(t.messages, key=lambda m: m.timestamp) if t.messages else None
    return {
        "id": t.id,
        "title": t.thread_title or ", ".join(u.username for u in t.users),
        "users": [user_short(u) for u in t.users],
        "group": t.is_group,
        "pending": t.pending,
        "unread": thread_unread(t, my_id),
        "at": t.last_activity_at.isoformat(),
        "last": message_json(last, my_id) if last else None,
    }


# -- search & profiles -------------------------------------------------------


@app.get("/api/search")
def search(q: str = ""):
    q = q.strip().lstrip("@")
    if not q:
        return []
    raw = ig.cached(f"search:{q.lower()}", 300, lambda cl: cl.private_request("users/search/", params={"query": q, "count": 50}))
    return [{**user_short(extract_user_short(u)), "social": social(u, "search")} for u in (raw.get("users") or [])[:25]]


def user_and_friendship(cl, username):
    """One request: the profile response already carries our relationship to them."""
    raw = cl.private_request(f"users/{username}/usernameinfo/")["user"]
    fs = raw.get("friendship_status") or {}
    context = social(raw, "profile")
    user = extract_user_v1(dict(raw))
    if "friendship" not in SOCIAL_KEYS_LOGGED:
        SOCIAL_KEYS_LOGGED.add("friendship")
        log.info("profile friendship_status fields: %s", sorted(fs))
    if "following" not in fs and str(user.pk) != str(cl.user_id):  # not included this time: ask separately
        # (not user_friendship_v1: it turns throttling and errors into None)
        fs = cl.private_request(f"friendships/show/{user.pk}/")
    return user, fs, context


@app.get("/api/u/{username}")
def profile(username: str):
    u, fs, context = ig.cached(f"user:{username.lower()}", 120, user_and_friendship, username)
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
        "social": None if me else context,
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


def stories_from_items(items: list) -> list:
    """Story JSON from raw reel items, with whether you liked each (instagrapi's Story drops it)."""
    out = [{**story_json(extract_story_v1(it)), "liked": bool(it.get("has_liked"))} for it in items]
    return sorted(out, key=lambda x: x["taken_at"])


def user_stories(cl, user_id):
    params = {"supported_capabilities_new": json.dumps(ig_config.SUPPORTED_CAPABILITIES)}
    reel = cl.private_request(f"feed/user/{int(user_id)}/story/", params=params).get("reel") or {}
    return stories_from_items(reel.get("items") or [])


@app.get("/api/u/{user_id}/stories")
def stories(user_id: str):
    return ig.cached(f"stories:{user_id}", 120, user_stories, user_id, soft=True)


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
    owner = body.media_id.split("_")[-1]
    cached = ig.peek(f"stories:{owner}")
    if cached:  # keep the cached copy in step instead of fetching it again
        ig.put(f"stories:{owner}", 120, [{**x, "liked": body.like} if x["media_id"] == body.media_id else x for x in cached])
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


class SeenStory(BaseModel):
    media_id: str  # "<story pk>_<owner pk>"
    taken_at: str


class SeenStories(BaseModel):
    items: list[SeenStory] = []
    ids: list[str] = []  # older app versions; nothing to send without the owner
    done: bool = False  # watched to their last story


def mark_stories_seen(cl, items):
    """Tell Instagram these stories were watched, the way the app does.

    (instagrapi's story_seen looks up each story's owner with an extra request
    and reports a made-up posting time, which Instagram ignores.)
    """
    now = int(time.time())
    reels = {}
    for it in items:
        taken = int(dt.datetime.fromisoformat(it.taken_at).timestamp())
        owner = it.media_id.split("_")[-1]
        reels[f"{it.media_id}_{owner}"] = [f"{taken}_{max(now, taken)}"]
    data = {
        "container_module": "feed_timeline",
        "live_vods_skipped": {},
        "nuxes_skipped": {},
        "nuxes": {},
        "reels": reels,
        "live_vods": {},
        "reel_media_skipped": {},
    }
    result = cl.private_request("/v2/media/seen/?reel=1&live_vod=0", cl.with_default_data(data))
    if result.get("status") != "ok":
        raise ClientError(f"seen: {result.get('status')}")
    return True


# "Watched" reports wait a few seconds and go out together: sent at once, each
# would hold the Instagram lock just as you tap the next person's stories.
SEEN_DELAY = 4.0
SEEN = {"items": {}, "timer": None}
SEEN_LOCK = threading.Lock()


def flush_seen():
    with SEEN_LOCK:
        items = list(SEEN["items"].values())
        SEEN.update(items={}, timer=None)
    if not items:
        return
    try:
        ok = ig.run(mark_stories_seen, items, kind="seen")
    except Exception:  # disconnected meanwhile: nothing to tell Instagram
        ok = False
    log.info("stories seen sent=%d ok=%s", len(items), bool(ok))


@app.post("/api/stories/seen")
def stories_seen(body: SeenStories):
    items = [it for it in body.items if "_" in it.media_id]
    if items:
        if body.done:  # stopping partway leaves them unwatched in the tray, as Instagram does
            watched(local_now().date().isoformat()).update(it.media_id.split("_")[-1] for it in items)
        with SEEN_LOCK:
            SEEN["items"].update({it.media_id: it for it in items})
            if SEEN["timer"] is None:
                SEEN["timer"] = threading.Timer(SEEN_DELAY, flush_seen)
                SEEN["timer"].daemon = True
                SEEN["timer"].start()
    return {"ok": True}


class Follow(BaseModel):
    follow: bool


def follow_request(cl, user_id: str, follow: bool) -> dict:
    """friendships/create or destroy, without instagrapi's extra pre-check; the
    answer already carries the new relationship."""
    data = cl.with_action_data({"user_id": user_id, "_uid": str(cl.user_id), "container_module": "profile"})
    if follow:
        data["include_follow_friction_check"] = "1"
    result = cl.private_request(f"friendships/{'create' if follow else 'destroy'}/{user_id}/", data)
    return result.get("friendship_status") or {}


@app.post("/api/u/{user_id}/follow")
def follow(user_id: str, body: Follow):
    fs = ig.run(follow_request, user_id, body.follow, kind="follow")
    ig.forget("user:")
    if not body.follow:
        tray_forget(user_id)
    return {"following": bool(fs.get("following")), "followed_by": bool(fs.get("followed_by")), "requested": bool(fs.get("outgoing_request"))}


CDN = requests.Session()  # keeps connections to the image servers open


@app.get("/api/img")
def image(u: str):
    host = urlparse(u).hostname or ""
    if not u.startswith("https://") or not (host.endswith(".cdninstagram.com") or host.endswith(".fbcdn.net")):
        return err(400, "bad_url", "Not an Instagram image.")
    r = CDN.get(u, timeout=10)  # Instagram's public CDN; no account involved
    return Response(
        r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=86400"},
    )


# -- today: posts and stories from people you follow, 5 minutes once a day -----

DAILY_MINUTES = 8
DAILY_TIMER = True  # False keeps Today open all day with no countdown (handy while tuning it)
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
    if not DAILY_TIMER:
        return {**base, "status": "open", "remaining": None}
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
    """'keep' if it belongs in Following, or what it is instead: an ad, a suggestion, someone you don't follow."""
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


def timeline_page(cl, max_id, reason="cold_start_fetch"):
    """One feed/timeline request. instagrapi would report every post it has
    fetched as fully viewed, which takes them out of your feed in the official
    app too, so send no views. With a max_id it switches the reason to "pagination"."""
    return cl.get_timeline_feed(reason, max_id=max_id, seen_posts=[])


def page_medias(raw: dict, tally: Counter) -> list:
    """A timeline page's posts. Other modules (carousels of suggested accounts,
    "You're all caught up") aren't shown."""
    medias = []
    for it in raw.get("feed_items") or []:
        if it.get("media_or_ad"):
            medias.append(it["media_or_ad"])
        else:
            tally.update(f"module:{k}" for k in it)
    return medias


def to_post(media: dict, tally: Counter):
    try:
        m = media_full(media_from_raw(media))
    except Exception:
        tally["unparsed"] += 1
        return None
    if not m["ratio"] and media.get("original_width") and media.get("original_height"):
        m["ratio"] = media["original_width"] / media["original_height"]
    return m


MARKS = {}  # media pk -> (when, {"liked"/"saved": ...}): what you changed, laid over cached feed pages
MARK_TTL = 900  # as long as a feed page stays cached; after that Instagram's own answer is fresh


def with_mark(m: dict, mark: dict) -> dict:
    """A post with your like/save applied, keeping the like count in step with the heart."""
    out = {**m, **mark}
    if "liked" in mark and bool(m.get("liked")) != mark["liked"] and m.get("likes") is not None:
        out["likes"] = max(0, m["likes"] + (1 if mark["liked"] else -1))
    return out


def mark_of(media_pk: str) -> dict:
    hit = MARKS.get(media_pk)
    if hit and time.monotonic() - hit[0] < MARK_TTL:
        return hit[1]
    MARKS.pop(media_pk, None)
    return {}


def remember_mark(media_pk: str, **fields):
    """A like/save you just made, shown on feed pages cached from before it."""
    MARKS[media_pk] = (time.monotonic(), {**mark_of(media_pk), **fields})


# Every real feed/timeline request is counted against one
# daily budget (kept in state.json, so a restart doesn't reset it) and spaced out.
TIMELINE_DAILY_MAX = 40
TIMELINE_GAP = 3.0  # seconds between two real requests
TIMELINE_LOCK = threading.Lock()  # one real fetch at a time; waiting here doesn't hold ig.lock
TIMELINE_LAST = [0.0]


class FeedBudgetSpent(Exception):
    pass


def timeline_used(day: str) -> int:
    t = ig.state().get("timeline") or {}
    return t.get("n", 0) if t.get("day") == day else 0


def fetch_page(day: str, max_id, reason="cold_start_fetch") -> dict:
    key = f"daily:feed:{day}:{max_id}"
    hit = ig.peek(key)
    if hit is not None:
        return hit
    with TIMELINE_LOCK:
        hit = ig.peek(key)
        if hit is not None:
            return hit
        if timeline_used(day) >= TIMELINE_DAILY_MAX:
            raise FeedBudgetSpent()
        wait = TIMELINE_GAP - (time.monotonic() - TIMELINE_LAST[0])
        if wait > 0:
            time.sleep(wait)
        try:
            # Later pages are extras: a refusal there shouldn't count as trouble.
            return ig.cached(key, 900, timeline_page, max_id, reason, soft=bool(max_id))
        finally:
            TIMELINE_LAST[0] = time.monotonic()
            if not max_id:  # the top of the feed was just asked for, whatever started it
                FEED["refreshed"] = time.monotonic()
            ig.update_state(timeline={"day": day, "n": timeline_used(day) + 1})


def daily_page(day: str, cursor: str | None, reason="cold_start_fetch"):
    """One timeline page, fetched from Instagram once (it stays cached 15 minutes).

    Returns the page's posts as Instagram ordered them, the ids among them that
    are ads, and where the feed goes next.
    """
    raw = fetch_page(day, cursor, reason)
    tally = Counter()
    shown, ads = [], set()
    for media in page_medias(raw, tally):
        verdict = feed_verdict(media)
        tally[verdict] += 1
        m = to_post(media, tally)
        if not m:
            continue
        shown.append(with_mark(m, mark_of(m["id"])))
        if verdict == "ad":
            ads.add(m["id"])
    more = raw.get("more_available") and raw.get("next_max_id")
    return shown, ads, more or None, tally


FEED_REFRESH_GAP = 30  # seconds after the top of the feed was fetched before a pull-to-refresh asks Instagram again
FEED = {"day": None, "served": set(), "dry": 0, "refreshed": -1e9}


@app.get("/api/daily/feed")
def daily_feed(cursor: str = "", fresh: bool = False):
    """Instagram's own feed, a page at a time, as it sends it: ads and suggestions included.

    Past "You're all caught up" Instagram can keep answering with the same few
    posts (plus a new ad) under a new cursor; two pages in a row with nothing new
    but ads end the feed.

    fresh=1 is a pull-to-refresh: it asks Instagram for the top of the feed again
    (saying it's a pull, as the app does) unless that was done in the last
    FEED_REFRESH_GAP seconds; then "wait" says how long until it will.
    """
    require_daily_open()
    day = local_now().date().isoformat()
    if FEED["day"] != day:
        FEED.update(day=day, served=set(), dry=0)
    wait = 0
    if not cursor:  # a new pass through the feed
        if fresh and timeline_used(day) < TIMELINE_DAILY_MAX:
            wait = FEED_REFRESH_GAP - (time.monotonic() - FEED["refreshed"])
            if wait <= 0:
                ig.forget(f"daily:feed:{day}:None")
        FEED.update(served=set(), dry=0)
    elif FEED["dry"] >= 2:
        return {"items": [], "next": None}
    try:
        shown, ads, nxt, tally = daily_page(day, cursor or None, "pull_to_refresh" if fresh else "cold_start_fetch")
    except FeedBudgetSpent:
        log.info("daily feed: today's %d pages are used", TIMELINE_DAILY_MAX)
        return {"items": [], "next": None, "spent": True}
    except ClientError:
        if not cursor:
            raise
        return {"items": [], "next": None}  # a later page failing just ends the list
    new = [m for m in shown if m["id"] not in FEED["served"] and m["id"] not in ads]
    FEED["served"].update(m["id"] for m in shown)
    FEED["dry"] = 0 if new or not cursor else FEED["dry"] + 1
    done = not nxt or FEED["dry"] >= 2
    # Counts only, no content: lets us tune this from the logs.
    log.info("daily feed page=%s shown=%d new=%d used=%d %s", "next" if cursor else "first", len(shown), len(new), timeline_used(day), dict(tally))
    return {"items": shown, "next": None if done else nxt, "wait": max(0, int(wait + 0.999))}


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


WATCHED = {"day": None, "owners": set()}  # whose stories you watched here today


def watched(day: str) -> set:
    if WATCHED["day"] != day:
        WATCHED.update(day=day, owners=set())
    return WATCHED["owners"]


def tray_entry(reel, seen_here: set, tally: Counter):
    u = reel.get("user") or {}
    fs = u.get("friendship_status") or {}
    if reel.get("reel_type", "user_reel") != "user_reel" or not u.get("username"):
        tally["not_a_person"] += 1
        return None
    if fs.get("following") is False:
        tally["not_following"] += 1
        return None
    if reel.get("muted") or fs.get("is_muting_reel"):  # stories you've muted
        tally["muted"] += 1
        return None
    pk = str(u.get("pk") or u.get("pk_id"))
    if pk == ig.user_id:
        tally["mine"] += 1
        return None
    if reel.get("items"):  # the stories came along: opening them needs no request while the tray is cached
        tally["with_stories"] += 1
        if ig.peek(f"stories:{pk}") is None:
            try:
                ig.put(f"stories:{pk}", TRAY_TTL, stories_from_items(reel["items"]))
            except Exception:
                pass
    unseen = (reel.get("seen") or 0) < (reel.get("latest_reel_media") or 0) and pk not in seen_here
    return {**raw_user(u), "unseen": unseen}


TRAY_TTL = 600
# feed/reels_media has answered "something went wrong" every time in production,
# so the already-watched people aren't fetched; True tries again (once a day).
TRAY_FETCH_WATCHED = False
TRAY_REST_FAILED = set()
TRAY_REFRESHED = [-1e9]

# Everyone the tray has shown today. Once you've watched someone, Instagram's
# tray only names them by id, so this is what keeps them at the end of yours.
# Kept in a file, so a restart doesn't empty it.
TRAY_FILE = DATA / "tray.json"
TRAY_PEOPLE = {"day": None, "people": {}}
TRAY_LOCK = threading.Lock()  # two tray requests can overlap (opening Today, then pulling)


def tray_people(day: str) -> dict:
    if TRAY_PEOPLE["day"] != day:
        saved = read_json(TRAY_FILE)
        TRAY_PEOPLE.update(day=day, people=(saved.get("people") or {}) if saved.get("day") == day else {})
    return TRAY_PEOPLE["people"]


def tray_forget(user_id: str):
    """Someone you've just unfollowed or muted shouldn't stay in the tray from memory."""
    day = local_now().date().isoformat()
    with TRAY_LOCK:
        people = tray_people(day)
        if people.pop(str(user_id), None):
            write_private_json(TRAY_FILE, {"day": day, "people": people})


@app.get("/api/daily/stories")
def daily_stories(fresh: bool = False):
    require_daily_open()
    day = local_now().date().isoformat()
    if fresh and time.monotonic() - TRAY_REFRESHED[0] > FEED_REFRESH_GAP:
        TRAY_REFRESHED[0] = time.monotonic()
        ig.forget(f"daily:tray:{day}", f"daily:reels:{day}")
    raw = ig.cached(f"daily:tray:{day}", TRAY_TTL, lambda cl: cl.get_reels_tray_feed("cold_start"))
    reels = list(raw.get("tray") or [])
    # The tray's first page is new stories only; the people you've already
    # watched come as bare ids.
    rest = [str(r) for r in raw.get("remaining_reel_ids_to_fetch") or [] if str(r).isdigit()]
    for i in range(0, min(len(rest), 100) if TRAY_FETCH_WATCHED and day not in TRAY_REST_FAILED else 0, 50):
        chunk = rest[i : i + 50]
        try:  # an extra: if it fails, still show the new ones
            reels += ig.cached(f"daily:reels:{day}:{','.join(chunk)}", TRAY_TTL, reels_media, chunk, soft=True)
        except ClientError as e:
            TRAY_REST_FAILED.add(day)  # don't keep asking today
            log.warning("daily tray: reels_media failed: %s: %s", type(e).__name__, str(e)[:200])
            break
    out, ids, seen_here, tally = [], set(), watched(day), Counter()
    for reel in reels:
        e = tray_entry(reel, seen_here, tally)
        if e and e["id"] not in ids:
            ids.add(e["id"])
            out.append(e)
    # People shown earlier today who've left the first page: you watched them
    # (here or in the app). They stay, at the end, while Instagram still lists
    # them among the watched; when it doesn't, their stories are gone.
    out.sort(key=lambda x: not x["unseen"])  # new ones first, like the real app
    with TRAY_LOCK:
        people, watched_ids = tray_people(day), set(rest)
        kept = [{**e, "unseen": False} for pk, e in people.items() if pk not in ids and (pk in watched_ids or not rest)]
        out += kept
        if any(people.get(e["id"]) != e for e in out):
            people.update({e["id"]: e for e in out})
            write_private_json(TRAY_FILE, {"day": day, "people": people})
    # Counts only, no names: lets us tune this from the logs.
    log.info("daily tray first=%d rest=%d shown=%d unseen=%d kept=%d %s", len(raw.get("tray") or []), len(rest), len(out), sum(x["unseen"] for x in out), len(kept), dict(tally))
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
    raw = ig.cached("requests", 30, lambda cl: cl.private_request("friendships/pending/", params={"count": 50}))
    return [{**user_short(extract_user_short(u)), "social": social(u, "requests")} for u in raw.get("users") or []]


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
    return media_full(ig.cached(f"media:{media_pk}", 120, media_info, media_pk))


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
        if body.stories:
            tray_forget(user_id)
    ig.forget("user:", "daily:tray:", "daily:reels:")
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
    remember_mark(media_pk, saved=body.save)
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
    if not ig.run(lambda cl: cl.media_like(body.media_id, revert=not body.like), kind="like"):
        return err(502, "instagram", "Instagram didn't take the like. Try again.")
    ig.forget(f"media:{media_pk}", "mine:liked")
    remember_mark(media_pk, liked=body.like)
    return {"liked": body.like}


# -- direct messages ---------------------------------------------------------


@app.get("/api/dm")
def inbox(requests: bool = False):
    my_id = ig.user_id
    if requests:
        threads = ig.cached("inbox:requests", 10, lambda cl: cl.direct_pending_chunk()[0])
    else:
        # 20 threads = one page; we only show each thread's latest message.
        threads = ig.cached("inbox:main", 10, lambda cl: cl.direct_threads_chunk(thread_message_limit=1)[0])
    return [thread_json(t, my_id) for t in threads]


@app.get("/api/dm/{thread_id}")
def thread(thread_id: str, n: int = 20):  # Instagram pages messages by 20
    n = max(10, min(n, 200))
    my_id = ig.user_id
    t = ig.cached(f"thread:{thread_id}:{n}", 4, lambda cl: cl.direct_thread(int(thread_id), amount=n))
    messages = sorted(t.messages, key=lambda m: m.timestamp)
    if not t.pending and thread_unread(t, my_id):
        last = max(t.messages, key=lambda m: m.timestamp)  # instagrapi's direct_send_seen refetches the thread first
        ig.run(lambda cl: cl.direct_message_seen(int(thread_id), int(last.id)), kind="seen")
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
