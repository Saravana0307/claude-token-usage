#!/bin/bash
# Detect and cache account identity using `claude auth status`

CTU_DIR="$HOME/.ctu"
ACCOUNT_FILE="$CTU_DIR/current-account.txt"
mkdir -p "$CTU_DIR"

# Priority 1: Explicit env var override
if [ -n "$CLAUDE_ACCOUNT_EMAIL" ]; then
    echo "$CLAUDE_ACCOUNT_EMAIL" > "$ACCOUNT_FILE"
    exit 0
fi

# Priority 2: `claude auth status` — reflects whoever is actually logged in right now
python3 - <<'PYEOF'
import json, os, sys, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

CTU_DIR = Path.home() / ".ctu"
ACCOUNT_FILE = CTU_DIR / "current-account.txt"
ACCOUNT_CACHE = CTU_DIR / "account-cache.json"

# Use cached result if fresh (< 1h) and cache key matches current auth
def get_auth_fingerprint():
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=5
        )
        data = json.loads(result.stdout.strip())
        return data.get("email", "") + "|" + data.get("subscriptionType", "")
    except Exception:
        return ""

fingerprint = get_auth_fingerprint()

if ACCOUNT_CACHE.exists() and fingerprint:
    try:
        cached = json.loads(ACCOUNT_CACHE.read_text())
        fetched = datetime.fromisoformat(cached.get("fetched_at", "2000-01-01"))
        if (datetime.now(timezone.utc) - fetched) < timedelta(hours=1) and cached.get("fp") == fingerprint:
            ACCOUNT_FILE.write_text(cached["account"])
            sys.exit(0)
    except Exception:
        pass

# Parse the auth status JSON we already fetched
try:
    result = subprocess.run(
        ["claude", "auth", "status"],
        capture_output=True, text=True, timeout=5
    )
    data = json.loads(result.stdout.strip())
    email = data.get("email", "")
    sub_type = data.get("subscriptionType", "")
    logged_in = data.get("loggedIn", False)

    if not logged_in:
        account = "not-logged-in"
    elif email and sub_type:
        account = f"{email} ({sub_type})"
    elif email:
        account = email
    elif sub_type:
        account = f"unknown ({sub_type})"
    else:
        account = "unknown"
except Exception:
    account = "unknown"

# Cache result
fp = fingerprint or account
cache = {"account": account, "fp": fp, "fetched_at": datetime.now(timezone.utc).isoformat()}
ACCOUNT_CACHE.write_text(json.dumps(cache))
ACCOUNT_FILE.write_text(account)
PYEOF

# Final fallback if Python script wrote nothing
if [ ! -s "$ACCOUNT_FILE" ]; then
    echo "unknown" > "$ACCOUNT_FILE"
fi

# ── Import uncaptured claude-mem observer sessions ───────────────────────────
# Observer sessions run by the claude-mem plugin never emit Stop events, so
# the main hook never fires for them. We scan for new ones on each session start.
python3 "$HOME/.ctu/bin/import-observer-sessions.py" 2>/dev/null || true
