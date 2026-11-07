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
- `ig.py`: the only code that talks to Instagram. It reuses one saved session and the same
  emulated phone, serialises calls, and never logs in or answers a verification
  challenge on its own. Actions are paced and capped per kind:

  | Action | Min gap | Per hour | Per day |
  |---|---|---|---|
  | Follow / unfollow | 30 s | 8 | 20 |
  | Like | 4 s | 30 | 70 |
  | Message someone new | 20 s | 5 | 10 |
  | Reply in a conversation | ~2 s | 60 | 300 |
  | Post / story | 60 s | 3 | 5 |

  When Instagram pushes back it stops: 30 minutes for throttling or repeated errors,
  24 hours for an action block (72 hours for a second one within a week), and a full
  stop if the account or IP is flagged.
- `static/`: the app (Preact + htm, no build step). It caches everything locally, so screens
  open instantly and refresh quietly. Follow, like and send update the screen immediately.

## Deploying on Railway

1. Create a service from this repo.
2. Add a **volume** mounted at `/data`. The Instagram session lives there and survives deploys.
3. Set these variables:
   - `APP_PASSCODE`: what you type once per phone to unlock the app (6+ characters).
   - `IG_PROXY` (recommended): `http://user:pass@host:port` for a **static** residential
     ("ISP") proxy. Railway runs in a data center, and a data-center IP is the biggest
     avoidable red flag. Use one fixed IP, not a rotating pool.
4. Generate a domain, open it in Safari on the iPhone, tap Share → **Add to Home Screen**.
5. Unlock with the passcode, then log in to Instagram. 2FA and "we sent you a code"
   prompts are handled in the app. If Instagram asks "Was this you?" in the official app,
   approve it, and don't remove this device from "Where you're logged in".

The app pins an exact instagrapi version because the library (and Instagram) change
often. Upgrade only when something breaks.

## Running locally

```sh
APP_PASSCODE=something ./dev.sh   # http://localhost:8642
```

Local data (session, state) goes in `./data/`, which git ignores.
