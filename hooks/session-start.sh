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
python3 - <<'OBSEOF'
import json, sqlite3
from pathlib import Path
from datetime import datetime, timezone

CTU_DIR     = Path.home() / ".ctu"
DB_FILE     = CTU_DIR / "usage.db"
PROJECTS    = Path.home() / ".claude" / "projects"
SCAN_FILE   = CTU_DIR / "observer-scan.txt"

if not DB_FILE.exists():
    exit(0)

# Load last scan timestamp
last_scan = datetime.min.replace(tzinfo=timezone.utc)
if SCAN_FILE.exists():
    try:
        last_scan = datetime.fromisoformat(SCAN_FILE.read_text().strip())
    except Exception:
        pass

# Find observer session directories (any project ending with claude-mem-observer-sessions)
obs_dirs = list(PROJECTS.glob("*claude-mem-observer-sessions"))
if not obs_dirs:
    exit(0)

def is_real_user_entry(entry):
    if entry.get("type") != "user":
        return False
    content = entry.get("message", {}).get("content", "")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        return any(isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip() for b in content)
    return False

account = (CTU_DIR / "current-account.txt").read_text().strip() if (CTU_DIR / "current-account.txt").exists() else "unknown"

conn = sqlite3.connect(DB_FILE)
conn.row_factory = sqlite3.Row
imported = 0

for obs_dir in obs_dirs:
    for jsonl in sorted(obs_dir.glob("*.jsonl")):
        if jsonl.stat().st_mtime < last_scan.timestamp():
            continue

        session_id = jsonl.stem
        # Skip if already in DB
        exists = conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
        if exists:
            continue

        try:
            entries = [json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]
        except Exception:
            continue

        # Load pricing
        pricing_file = CTU_DIR / "pricing.json"
        PRICING = {}
        for pf in [pricing_file, CTU_DIR / "pricing_defaults.json"]:
            if pf.exists():
                try:
                    PRICING = json.loads(pf.read_text()).get("models", {})
                    break
                except Exception:
                    pass

        # Sum tokens per turn with dedup
        total_in=total_out=total_cr=total_cw=0
        seen = set()
        model = ""
        first_ts = last_ts = None

        for entry in entries:
            if is_real_user_entry(entry):
                seen = set()
            elif entry.get("type") == "assistant":
                msg = entry.get("message", {})
                u   = msg.get("usage", {})
                if not model:
                    model = msg.get("model", "")
                ts_str = entry.get("timestamp", "")
                if ts_str:
                    if not first_ts:
                        first_ts = ts_str
                    last_ts = ts_str

                key = (u.get("input_tokens",0), u.get("output_tokens",0),
                       u.get("cache_read_input_tokens",0), u.get("cache_creation_input_tokens",0))
                if key not in seen:
                    seen.add(key)
                    total_in += u.get("input_tokens",0)
                    total_out+= u.get("output_tokens",0)
                    total_cr += u.get("cache_read_input_tokens",0)
                    total_cw += u.get("cache_creation_input_tokens",0)

        if total_in + total_out + total_cr + total_cw == 0:
            continue

        # Pricing lookup
        mid = (model or "").lower()
        prices = None
        if mid in PRICING:
            prices = PRICING[mid]
        else:
            for key in PRICING:
                if mid.startswith(key) or key.startswith(mid.rsplit("-2",1)[0]):
                    prices = PRICING[key]
                    break
        if prices:
            p_in, p_out, p_cr, p_cw = prices
        else:
            p_in, p_out, p_cr, p_cw = 3.0, 15.0, 0.30, 3.75

        cost = (total_in/1e6*p_in + total_out/1e6*p_out +
                total_cr/1e6*p_cr + total_cw/1e6*p_cw)

        ts_now = (first_ts or datetime.now(timezone.utc).isoformat())

        # Insert session
        conn.execute("""
            INSERT OR IGNORE INTO sessions
                (id, project, account_email, started_at, model,
                 total_input_tokens, total_output_tokens, total_cache_read, total_cache_write, total_cost)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (session_id, "claude-mem-observer", account, ts_now, model,
              total_in, total_out, total_cr, total_cw, cost))

        # Insert single prompt row representing the whole session
        conn.execute("""
            INSERT INTO prompts
                (session_id, prompt_index, prompt_text, model, timestamp,
                 input_tokens, output_tokens, cache_read, cache_write, cost, tool_count, agent_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (session_id, 0, "[claude-mem observer session]", model, ts_now,
              total_in, total_out, total_cr, total_cw, cost, 0, 0))

        conn.commit()
        imported += 1

conn.close()

# Update scan timestamp
SCAN_FILE.write_text(datetime.now(timezone.utc).isoformat())
OBSEOF
