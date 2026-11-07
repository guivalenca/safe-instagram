#!/bin/sh
# Run locally: APP_PASSCODE=something ./dev.sh  (then open http://localhost:8642)
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -r requirements.txt; }
exec .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8642 --reload
