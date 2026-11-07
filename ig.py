"""Instagram client wrapper.

Everything that talks to Instagram goes through `IG.run()`, which:
  * reuses one saved session (the server never re-logs in by itself),
  * serialises calls (instagrapi's Client is not thread-safe),
  * paces actions (follow, like, send, post) like a person, and
  * backs off hard when Instagram complains, so we never hammer it.
"""

import datetime as dt
import json
import os
import queue
import random
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from instagrapi import Client
from instagrapi.exceptions import (
    AccountSuspended,
    ChallengeError,
    ClientError,
    ClientLoginRequired,
    ClientThrottledError,
    ClientUnauthorizedError,
    FeedbackRequired,
    LoginRequired,
    NotFoundError,
    PleaseWaitFewMinutes,
    PrivateAccount,
    ProxyAddressIsBlocked,
    RateLimitError,
    SentryBlock,
    TwoFactorRequired,
)

# On Railway, attach a volume and this picks up its mount path automatically.
DATA = Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or Path(__file__).parent / "data")
SESSION_FILE = DATA / "session.json"
DEVICE_FILE = DATA / "device.json"  # the emulated phone, kept after disconnecting so we can reuse it
STATE_FILE = DATA / "state.json"
# Optional: route Instagram traffic through a residential proxy, e.g. http://user:pass@host:port
PROXY = os.environ.get("IG_PROXY", "").strip()

# Per kind of action: (minimum seconds between two of them, max per hour, max per day).
# Well under what people report for the official app; a person doing normal
# things never notices them.
LIMITS = {
    "follow": (30, 8, 20),  # follows and unfollows
    "like": (4, 30, 70),
    "dm_new": (20, 5, 10),  # first message to someone
    "dm": (1.5, 60, 300),  # replies in an existing conversation
    "upload": (60, 3, 5),
    "approve": (3, 40, 150),  # confirming / deleting follow requests
    "other": (3, 30, 100),  # accepting requests etc.
}
LIMIT_NAMES = {
    "follow": "follows",
    "like": "likes",
    "dm_new": "new conversations",
    "upload": "posts",
    "approve": "follow request answers",
}

# How long to stop talking to Instagram after it pushes back.
COOLDOWN_SOFT = dt.timedelta(minutes=30)  # "please wait a few minutes", throttling, repeated errors
COOLDOWN_ACTION_BLOCK = dt.timedelta(hours=24)  # "feedback_required" = action block
COOLDOWN_REPEAT_BLOCK = dt.timedelta(hours=72)  # a second action block within a week
COOLDOWN_STOP = dt.timedelta(hours=24)  # account/IP flagged: check the official app


class NotConnected(Exception):
    pass


class SessionExpired(Exception):
    pass


class CoolingDown(Exception):
    def __init__(self, until: dt.datetime, reason: str):
        self.until = until
        self.reason = reason


class DailyLimit(Exception):
    def __init__(self, what: str, per: str):
        self.what = what
        self.per = per


def write_private_json(path: Path, data: dict):
    """Atomically write a JSON file only this user can read (it holds session cookies)."""
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def new_client() -> Client:
    cl = Client()
    # No delay on reads: one person tapping around is nothing like a scraper,
    # and the official app fires requests back to back. Actions are paced in run().
    cl.delay_range = None
    cl.request_timeout = 0  # instagrapi sleeps this long before *every* request otherwise
    # The server never answers verification challenges by itself: it surfaces
    # them so you can approve the login in the official app.
    cl.with_challenge_flow = False
    if PROXY:
        cl.set_proxy(PROXY)
    return cl


def apply_timezone(cl: Client, tz_name: str):
    """Report the phone's current UTC offset (it shifts with daylight saving)."""
    try:
        offset = dt.datetime.now(ZoneInfo(tz_name)).utcoffset()
    except Exception:
        return
    cl.set_timezone_offset(int(offset.total_seconds()), timezone_name=tz_name)


class IG:
    def __init__(self):
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self._cl = None
        self._cache = {}  # key -> (expires_at, value)
        self._last_write = 0.0
        self._last_by_kind = {}

    # -- session ---------------------------------------------------------

    def _client(self) -> Client:
        if self._cl is None:
            if not SESSION_FILE.exists():
                raise NotConnected()
            cl = new_client()
            cl.load_settings(SESSION_FILE)
            if not cl.user_id:
                raise NotConnected()
            if self._state().get("timezone"):
                apply_timezone(cl, self._state()["timezone"])
            self._cl = cl
        return self._cl

    def _save(self):
        if self._cl is not None:
            write_private_json(SESSION_FILE, self._cl.get_settings())

    def connected(self) -> bool:
        try:
            with self.lock:
                self._client()
            return True
        except NotConnected:
            return False

    def adopt(self, cl: Client, username: str, tz_name: str):
        """Take over a freshly logged-in client."""
        with self.lock:
            cl.with_challenge_flow = False
            cl.request_timeout = 0
            self._cl = cl
            self._cache.clear()
            self._save()
            s = self._state()
            s.update(username=username, timezone=tz_name, errors=0)
            s.pop("cooldown_until", None)
            write_private_json(STATE_FILE, s)

    def disconnect(self):
        with self.lock:
            self._cl = None
            self._cache.clear()
            if SESSION_FILE.exists():
                os.replace(SESSION_FILE, DEVICE_FILE)

    @property
    def user_id(self) -> str:
        return str(self._client().user_id)

    @property
    def username(self) -> str:
        return self._state().get("username", "")

    # -- short-lived cache: collapses prefetch + tap, back/forward, refreshes --

    def cached(self, key: str, ttl: float, fn, *args, **kwargs):  # kwargs go to run()
        with self.lock:  # checked under the lock, so a duplicate request waits for the first one
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            value = self.run(fn, *args, **kwargs)
            self._cache[key] = (time.monotonic() + ttl, value)
            return value

    def forget(self, *prefixes: str):
        with self.lock:
            for k in [k for k in self._cache if k.startswith(prefixes)]:
                del self._cache[k]

    # -- safety state ----------------------------------------------------

    def _state(self) -> dict:
        return read_json(STATE_FILE)

    def state(self) -> dict:
        return self._state()

    def update_state(self, **values):
        with self.lock:
            s = self._state()
            s.update(values)
            write_private_json(STATE_FILE, s)

    def cooldown(self):
        s = self._state()
        until = s.get("cooldown_until")
        if until and dt.datetime.fromisoformat(until) > dt.datetime.now():
            return {"until": until, "reason": s.get("cooldown_reason", ""), "writes_only": s.get("cooldown_writes_only")}
        return None

    def _check_cooldown(self, write: bool):
        c = self.cooldown()
        if c and (write or not c["writes_only"]):
            raise CoolingDown(dt.datetime.fromisoformat(c["until"]), c["reason"])

    def _start_cooldown(self, length: dt.timedelta, reason: str, writes_only: bool):
        s = self._state()
        s["cooldown_until"] = (dt.datetime.now() + length).isoformat(timespec="seconds")
        s["cooldown_reason"] = reason
        s["cooldown_writes_only"] = writes_only
        write_private_json(STATE_FILE, s)

    def _action_block(self, cl: Client):
        s = self._state()
        now = time.time()
        recent = [t for t in s.get("blocks", []) if now - t < 7 * 86400]
        s["blocks"] = recent + [now]
        write_private_json(STATE_FILE, s)
        message = (cl.last_json or {}).get("feedback_message") or "Instagram blocked an action"
        length = COOLDOWN_REPEAT_BLOCK if recent else COOLDOWN_ACTION_BLOCK
        self._start_cooldown(length, message.rstrip("."), writes_only=True)

    def _spend(self, kind: str):
        """Check and record an action against its hourly/daily limits."""
        _, per_hour, per_day = LIMITS[kind]
        s = self._state()
        now = time.time()
        log = [(t, k) for t, k in s.get("actions_log", []) if now - t < 86400]
        mine = [t for t, k in log if k == kind]
        what = LIMIT_NAMES.get(kind, "actions")
        if len(mine) >= per_day:
            raise DailyLimit(what, "today")
        if len([t for t in mine if now - t < 3600]) >= per_hour:
            raise DailyLimit(what, "this hour")
        s["actions_log"] = log + [(now, kind)]
        write_private_json(STATE_FILE, s)

    # -- the one way in --------------------------------------------------

    def run(self, fn, *args, kind: str | None = None, soft: bool = False, **kwargs):
        """Call `fn(client, *args, **kwargs)` safely.

        kind=None    -> a read
        kind="seen"  -> marking stories/messages seen: free, silently skipped while paused
        kind=<LIMITS key> -> an action: paced, counted, and blocked during cool-downs
        soft=True    -> Instagram answers this with a generic error in normal cases
                        (e.g. a private account's posts), so don't count it as trouble
        """
        if kind is None:
            return self._call(fn, args, kwargs, write=False, soft=soft)
        if kind == "seen":
            try:
                return self._call(fn, args, kwargs, write=True)
            except (CoolingDown, ClientError):
                return None
        with self.write_lock:
            self._check_cooldown(write=True)
            self._spend(kind)
            # Space actions out like a thumb would. Sleeping here (not under
            # self.lock) keeps browsing instant meanwhile; the phone already
            # shows the result optimistically.
            min_gap = LIMITS[kind][0]
            since_kind = time.monotonic() - self._last_by_kind.get(kind, -1e9)
            since_any = time.monotonic() - self._last_write
            wait = max(min_gap * random.uniform(1.0, 1.4) - since_kind, random.uniform(1.5, 4.0) - since_any)
            if wait > 0:
                time.sleep(wait)
            try:
                return self._call(fn, args, kwargs, write=True)
            finally:
                self._last_write = self._last_by_kind[kind] = time.monotonic()

    def _call(self, fn, args, kwargs, write, soft=False):
        with self.lock:
            self._check_cooldown(write)
            cl = self._client()
            try:
                result = fn(cl, *args, **kwargs)
            except (LoginRequired, ClientLoginRequired, ClientUnauthorizedError, ChallengeError) as e:
                # Don't log back in automatically: repeated automatic logins
                # are exactly what gets accounts flagged. The app asks you.
                self._cl = None
                raise SessionExpired() from e
            except (AccountSuspended, SentryBlock, ProxyAddressIsBlocked) as e:
                self._start_cooldown(
                    COOLDOWN_STOP,
                    f"Instagram flagged this login ({type(e).__name__}). Open the official app and check your account",
                    writes_only=False,
                )
                raise
            except FeedbackRequired:
                self._action_block(cl)
                raise
            except (PleaseWaitFewMinutes, RateLimitError, ClientThrottledError):
                self._start_cooldown(COOLDOWN_SOFT, "Instagram asked us to slow down", writes_only=False)
                raise
            except (NotFoundError, PrivateAccount):
                raise  # normal answers, not trouble
            except ClientError:
                if soft:
                    raise
                s = self._state()
                s["errors"] = s.get("errors", 0) + 1
                write_private_json(STATE_FILE, s)
                if s["errors"] >= 3:  # something's off; stop poking at it
                    self._start_cooldown(COOLDOWN_SOFT, "Instagram keeps returning errors", writes_only=False)
                raise
            s = self._state()
            if s.get("errors"):
                s["errors"] = 0
                write_private_json(STATE_FILE, s)
            self._save()
            return result


class LoginFlow:
    """Interactive Instagram login driven from the phone.

    instagrapi asks for challenge codes ("we emailed you a code") by calling a
    function in the middle of login(). We run login() on a worker thread and
    let that function wait for the code to arrive with the next HTTP request.
    """

    WAIT = 90  # seconds an HTTP request waits for the login thread to say something
    BACKOFF = dt.timedelta(minutes=60)  # after Instagram rate-limits a login, don't try again for this long

    def __init__(self, ig: IG):
        self.ig = ig
        self.lock = threading.Lock()
        self._events = queue.Queue()
        self._codes = queue.Queue()
        self._thread = None
        self._creds = None
        self._pending_2fa = None
        self._relogin = False
        self._tz = "UTC"

    def _new_client(self, username: str, tz_name: str, locale: str) -> Client:
        cl = new_client()
        cl.with_challenge_flow = True  # here we *do* want "enter the code we sent you"
        cl.request_timeout = 1
        cl.challenge_code_handler = self._ask_for_code
        # Keep the same emulated phone across logins: a new device every time is a red flag.
        # login() re-validates a saved session first and only logs in fresh if it's dead.
        self._relogin = False
        if SESSION_FILE.exists():
            cl.load_settings(SESSION_FILE)
            previous = self.ig.username.lower()
            self._relogin = bool(previous and username and username.lower() != previous)
        elif DEVICE_FILE.exists():
            cl.load_settings(DEVICE_FILE)
            self._relogin = True
        if PROXY:
            cl.set_proxy(PROXY)
        apply_timezone(cl, tz_name)
        loc = (locale or "").replace("-", "_")
        if "_" in loc:
            cl.set_locale(loc)
            cl.set_country(loc.split("_")[1].upper())
        return cl

    def _ask_for_code(self, username, choice):
        self._events.put(("challenge", str(getattr(choice, "value", choice)).lower()))
        try:
            return self._codes.get(timeout=600)
        except queue.Empty:
            raise ChallengeError("Timed out waiting for the verification code")

    def _worker(self, cl: Client, verification_code: str = ""):
        username, password = self._creds
        try:
            cl.login(username, password, relogin=self._relogin, verification_code=verification_code)
            info = cl.account_info()
            self.ig.adopt(cl, info.username, self._tz)
            self._creds = None  # forget the password as soon as we're in
            self._events.put(("ok", info.username))
        except TwoFactorRequired:
            self._events.put(("2fa", cl))
        except (PleaseWaitFewMinutes, RateLimitError, ClientThrottledError):
            # Hammering the login endpoint is the fastest way to get flagged.
            self._creds = None
            s = self.ig._state()
            s["login_blocked_until"] = (dt.datetime.now() + self.BACKOFF).isoformat(timespec="seconds")
            write_private_json(STATE_FILE, s)
            self._events.put(("error", self._blocked_message()))
        except Exception as e:  # report anything else to the phone verbatim
            self._creds = None
            self._events.put(("error", f"{type(e).__name__}: {e}"))

    def _wait(self) -> dict:
        try:
            kind, value = self._events.get(timeout=self.WAIT)
        except queue.Empty:
            return {"status": "error", "message": "Instagram is taking too long. Try again."}
        if kind == "ok":
            return {"status": "ok", "username": value}
        if kind == "2fa":
            self._pending_2fa = value
            return {"status": "need_code", "kind": "2fa"}
        if kind == "challenge":
            return {"status": "need_code", "kind": value}
        return {"status": "error", "message": value}

    def _blocked_message(self) -> str | None:
        until = self.ig._state().get("login_blocked_until")
        if not until:
            return None
        left = dt.datetime.fromisoformat(until) - dt.datetime.now()
        if left.total_seconds() <= 0:
            return None
        minutes = max(1, round(left.total_seconds() / 60))
        return (
            "Instagram is limiting new logins from this server, so it's pausing to protect your account. "
            f"Try again in about {minutes} min, just once."
        )

    def start(self, username: str, password: str, code: str, tz_name: str, locale: str) -> dict:
        with self.lock:
            if blocked := self._blocked_message():
                return {"status": "error", "message": blocked}
            self._events, self._codes = queue.Queue(), queue.Queue()
            username = username.strip().lstrip("@")
            self._creds = (username, password)
            self._pending_2fa = None
            self._tz = tz_name
            cl = self._new_client(username, tz_name, locale)
            code = code.strip().replace(" ", "")
            self._thread = threading.Thread(target=self._worker, args=(cl, code), daemon=True)
            self._thread.start()
            return self._wait()

    def submit_code(self, code: str) -> dict:
        with self.lock:
            code = code.strip().replace(" ", "")
            if self._pending_2fa is not None and self._creds:
                cl, self._pending_2fa = self._pending_2fa, None
                self._thread = threading.Thread(target=self._worker, args=(cl, code), daemon=True)
                self._thread.start()
            elif self._thread and self._thread.is_alive():
                self._codes.put(code)
            else:
                return {"status": "error", "message": "Login expired. Start again."}
            return self._wait()

    def with_sessionid(self, sessionid: str, tz_name: str, locale: str) -> dict:
        """Fallback only: a browser session on an emulated phone is less sturdy."""
        with self.lock:
            if blocked := self._blocked_message():
                return {"status": "error", "message": blocked}
            cl = self._new_client("", tz_name, locale)
            try:
                cl.login_by_sessionid(sessionid.strip())
                info = cl.account_info()
            except Exception as e:
                return {"status": "error", "message": f"{type(e).__name__}: {e}"}
            self.ig.adopt(cl, info.username, tz_name)
            return {"status": "ok", "username": info.username}
