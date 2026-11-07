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

from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeError,
    ClientLoginRequired,
    ClientThrottledError,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
    TwoFactorRequired,
)

# On Railway, attach a volume and this picks up its mount path automatically.
DATA = Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or Path(__file__).parent / "data")
SESSION_FILE = DATA / "session.json"
STATE_FILE = DATA / "state.json"
# Optional: route Instagram traffic through a residential proxy, e.g. http://user:pass@host:port
PROXY = os.environ.get("IG_PROXY", "").strip()

# Follows + unfollows + likes per day. A person doing normal things stays far
# below this; hitting it is a sign something is off.
DAILY_ACTION_LIMIT = 40
# How long to stop talking to Instagram after it pushes back.
COOLDOWN_SOFT = dt.timedelta(minutes=30)  # "please wait a few minutes", throttling
COOLDOWN_ACTION_BLOCK = dt.timedelta(hours=24)  # "feedback_required" = action block


class NotConnected(Exception):
    pass


class SessionExpired(Exception):
    pass


class CoolingDown(Exception):
    def __init__(self, until: dt.datetime, reason: str):
        self.until = until
        self.reason = reason


class DailyLimit(Exception):
    pass


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
    # No delay on reads: one person tapping around is nothing like a scraper,
    # and the official app fires requests back to back too. Actions are paced in run().
    cl = Client()
    if PROXY:
        cl.set_proxy(PROXY)
    return cl


class IG:
    def __init__(self):
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self._cl = None
        self._cache = {}  # key -> (expires_at, value)
        self._last_write = 0.0

    # -- session ---------------------------------------------------------

    def _client(self) -> Client:
        if self._cl is None:
            if not SESSION_FILE.exists():
                raise NotConnected()
            cl = new_client()
            cl.load_settings(SESSION_FILE)
            if not cl.user_id:
                raise NotConnected()
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

    def adopt(self, cl: Client, username: str):
        """Take over a freshly logged-in client."""
        with self.lock:
            self._cl = cl
            self._cache.clear()
            self._save()
            s = self._state()
            s["username"] = username
            s.pop("cooldown_until", None)
            write_private_json(STATE_FILE, s)

    def disconnect(self):
        with self.lock:
            self._cl = None
            self._cache.clear()
            SESSION_FILE.unlink(missing_ok=True)

    @property
    def user_id(self) -> str:
        return str(self._client().user_id)

    @property
    def username(self) -> str:
        return self._state().get("username", "")

    # -- short-lived cache: collapses prefetch + tap, back/forward, refreshes --

    def cached(self, key: str, ttl: float, fn, *args, **kwargs):
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

    def _spend_action(self):
        s = self._state()
        today = dt.date.today().isoformat()
        if s.get("actions_day") != today:
            s["actions_day"], s["actions"] = today, 0
        if s["actions"] >= DAILY_ACTION_LIMIT:
            raise DailyLimit()
        s["actions"] += 1
        write_private_json(STATE_FILE, s)

    # -- the one way in --------------------------------------------------

    def run(self, fn, *args, write: bool = False, counts: bool = False, **kwargs):
        """Call `fn(client, *args, **kwargs)` safely.

        write  -> it changes something on Instagram (paced, subject to action blocks)
        counts -> it uses up the daily follow/like budget
        """
        if not write:
            return self._call(fn, args, kwargs, write, counts)
        with self.write_lock:
            # A couple of seconds between actions, like a thumb would. Sleeping
            # here (not under self.lock) keeps browsing instant meanwhile.
            gap = random.uniform(2.0, 4.0) - (time.monotonic() - self._last_write)
            if gap > 0:
                time.sleep(gap)
            try:
                return self._call(fn, args, kwargs, write, counts)
            finally:
                self._last_write = time.monotonic()

    def _call(self, fn, args, kwargs, write, counts):
        with self.lock:
            self._check_cooldown(write)
            if counts:
                self._spend_action()
            cl = self._client()
            try:
                result = fn(cl, *args, **kwargs)
            except (LoginRequired, ClientLoginRequired, ChallengeError) as e:
                # Don't log back in automatically: repeated automatic logins
                # are exactly what gets accounts flagged. The app asks you.
                self._cl = None
                raise SessionExpired() from e
            except FeedbackRequired:
                self._start_cooldown(COOLDOWN_ACTION_BLOCK, "Instagram blocked an action", writes_only=True)
                raise
            except (PleaseWaitFewMinutes, RateLimitError, ClientThrottledError):
                self._start_cooldown(COOLDOWN_SOFT, "Instagram asked us to slow down", writes_only=False)
                raise
            self._save()
            return result


class LoginFlow:
    """Interactive Instagram login driven from the phone.

    instagrapi asks for challenge codes ("we emailed you a code") by calling a
    function in the middle of login(). We run login() on a worker thread and
    let that function wait for the code to arrive with the next HTTP request.
    """

    WAIT = 90  # seconds an HTTP request waits for the login thread to say something

    def __init__(self, ig: IG):
        self.ig = ig
        self.lock = threading.Lock()
        self._events = queue.Queue()
        self._codes = queue.Queue()
        self._thread = None
        self._creds = None
        self._pending_2fa = None

    def _new_client(self, timezone_offset: int, locale: str) -> Client:
        cl = new_client()
        if SESSION_FILE.exists():
            # Keep the same emulated phone across logins: a new device every time is a red flag.
            old = read_json(SESSION_FILE)
            cl.set_settings({k: old[k] for k in ("uuids", "device_settings", "user_agent") if k in old})
            if PROXY:
                cl.set_proxy(PROXY)
        cl.set_timezone_offset(int(timezone_offset))
        loc = (locale or "").replace("-", "_")
        if "_" in loc:
            cl.set_locale(loc)
            cl.set_country(loc.split("_")[1].upper())
        cl.challenge_code_handler = self._ask_for_code
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
            cl.login(username, password, verification_code=verification_code)
            info = cl.account_info()
            self.ig.adopt(cl, info.username)
            self._creds = None  # forget the password as soon as we're in
            self._events.put(("ok", info.username))
        except TwoFactorRequired:
            self._events.put(("2fa", cl))
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

    def start(self, username: str, password: str, timezone_offset: int, locale: str) -> dict:
        with self.lock:
            self._events, self._codes = queue.Queue(), queue.Queue()
            self._creds = (username.strip().lstrip("@"), password)
            self._pending_2fa = None
            cl = self._new_client(timezone_offset, locale)
            self._thread = threading.Thread(target=self._worker, args=(cl,), daemon=True)
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

    def with_sessionid(self, sessionid: str, timezone_offset: int, locale: str) -> dict:
        with self.lock:
            cl = self._new_client(timezone_offset, locale)
            try:
                cl.login_by_sessionid(sessionid.strip())
                info = cl.account_info()
            except Exception as e:
                return {"status": "error", "message": f"{type(e).__name__}: {e}"}
            self.ig.adopt(cl, info.username)
            return {"status": "ok", "username": info.username}
