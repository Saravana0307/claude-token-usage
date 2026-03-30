#!/usr/bin/env python3
"""
Import claude-mem observer sessions into the CTU database.
Observer sessions never fire a Stop event, so this script is called:
  - At SessionStart (session-start.sh)
  - At UserPromptSubmit (token-display.sh) — catches sessions from the previous turn
"""
import json, sqlite3, time
from pathlib import Path
from datetime import datetime, timezone

CTU_DIR  = Path.home() / ".ctu"
DB_FILE  = CTU_DIR / "usage.db"
PROJECTS = Path.home() / ".claude" / "projects"
SCAN_FILE = CTU_DIR / "observer-scan.txt"
GRACE_SECONDS = 5  # skip files modified < 5s ago (may still be writing)

if not DB_FILE.exists():
    raise SystemExit(0)

# Load last scan timestamp
last_scan = datetime.min.replace(tzinfo=timezone.utc)
if SCAN_FILE.exists():
    try:
        last_scan = datetime.fromisoformat(SCAN_FILE.read_text().strip())
    except Exception:
        pass

obs_dirs = list(PROJECTS.glob("*claude-mem-observer-sessions"))
if not obs_dirs:
    SCAN_FILE.write_text(datetime.now(timezone.utc).isoformat())
    raise SystemExit(0)

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

# Load pricing once
PRICING = {}
for pf in [CTU_DIR / "pricing.json", CTU_DIR / "pricing_defaults.json"]:
    if pf.exists():
        try:
            PRICING = json.loads(pf.read_text()).get("models", {})
            break
        except Exception:
            pass

def lookup_pricing(mid):
    if not mid:
        return None
    mid = mid.lower()
    if mid in PRICING:
        return PRICING[mid]
    for key in PRICING:
        if mid.startswith(key) or key.startswith(mid.rsplit("-2", 1)[0]):
            return PRICING[key]
    return None

account = (CTU_DIR / "current-account.txt").read_text().strip() \
          if (CTU_DIR / "current-account.txt").exists() else "unknown"

now_ts = time.time()
conn = sqlite3.connect(DB_FILE)

for obs_dir in obs_dirs:
    for jsonl in sorted(obs_dir.glob("*.jsonl")):
        mtime = jsonl.stat().st_mtime
        if mtime < last_scan.timestamp():
            continue
        if now_ts - mtime < GRACE_SECONDS:
            continue  # still possibly being written

        session_id = jsonl.stem
        if conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            continue

        try:
            entries = [json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]
        except Exception:
            continue

        total_in = total_out = total_cr = total_cw = 0
        seen = set()
        model = ""
        first_ts = None

        for entry in entries:
            if is_real_user_entry(entry):
                seen = set()
            elif entry.get("type") == "assistant":
                msg = entry.get("message", {})
                u   = msg.get("usage", {})
                if not model:
                    model = msg.get("model", "")
                if not first_ts:
                    first_ts = entry.get("timestamp", "")
                key = (u.get("input_tokens", 0), u.get("output_tokens", 0),
                       u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))
                if key not in seen:
                    seen.add(key)
                    total_in += u.get("input_tokens", 0)
                    total_out += u.get("output_tokens", 0)
                    total_cr  += u.get("cache_read_input_tokens", 0)
                    total_cw  += u.get("cache_creation_input_tokens", 0)

        if total_in + total_out + total_cr + total_cw == 0:
            continue

        prices = lookup_pricing(model)
        if prices:
            p_in, p_out, p_cr, p_cw = prices
        else:
            p_in, p_out, p_cr, p_cw = 3.0, 15.0, 0.30, 3.75

        cost = (total_in/1e6*p_in + total_out/1e6*p_out +
                total_cr/1e6*p_cr + total_cw/1e6*p_cw)

        ts_now = first_ts or datetime.now(timezone.utc).isoformat()

        conn.execute("""
            INSERT OR IGNORE INTO sessions
                (id, project, account_email, started_at, model,
                 total_input_tokens, total_output_tokens, total_cache_read, total_cache_write, total_cost)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (session_id, "claude-mem-observer", account, ts_now, model,
              total_in, total_out, total_cr, total_cw, cost))

        conn.execute("""
            INSERT INTO prompts
                (session_id, prompt_index, prompt_text, model, timestamp,
                 input_tokens, output_tokens, cache_read, cache_write, cost, tool_count, agent_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (session_id, 0, "[claude-mem observer session]", model, ts_now,
              total_in, total_out, total_cr, total_cw, cost, 0, 0))

        conn.commit()

conn.close()
SCAN_FILE.write_text(datetime.now(timezone.utc).isoformat())
