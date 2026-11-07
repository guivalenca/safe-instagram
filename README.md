# safe-instagram

Instagram for the things you actually need (search people, follow, look at a
profile's posts and stories, DMs, posting), with none of the things built to
keep you scrolling. There's no feed, Reels, Explore, stories tray, suggestions or
notifications, and no setting to turn them back on.

It's a small Python server (FastAPI + [instagrapi](https://github.com/subzeroid/instagrapi))
plus a phone web app you add to your iPhone home screen from Safari.

## How it fits together

```
iPhone (home-screen app)  ──HTTPS──▶  Railway: app.py  ──▶  Instagram private API
        static/*                       ig.py  (session, pacing, safety)
```

- `app.py`: JSON API plus the passcode gate. Only the allowed features have endpoints.
- `ig.py`: the only code that talks to Instagram. It reuses one saved session, serialises
  calls, paces actions 2–4s apart, caps follows/likes at 40/day, and backs off for 30
  minutes (throttling) or 24 hours (action block) when Instagram complains. It never
  re-logs in on its own.
- `static/`: the app (Preact + htm, no build step). It caches everything locally, so screens
  open instantly and refresh quietly. Follow, like and send update the screen immediately.

## Deploying on Railway

1. Create a service from this repo.
2. Add a **volume** mounted at `/data`. The Instagram session lives there and survives deploys.
3. Set these variables:
   - `APP_PASSCODE`: what you type once per phone to unlock the app (6+ characters).
   - `IG_PROXY` (optional): `http://user:pass@host:port` for a residential proxy, if Instagram
     starts asking for verification from Railway's servers.
4. Generate a domain, open it in Safari on the iPhone, tap Share → **Add to Home Screen**.
5. Unlock with the passcode, then log in to Instagram. 2FA and "we sent you a code"
   prompts are handled in the app.

## Running locally

```sh
APP_PASSCODE=something ./dev.sh   # http://localhost:8642
```

Local data (session, state) goes in `./data/`, which git ignores.
