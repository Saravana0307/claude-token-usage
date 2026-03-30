#!/bin/bash
# Claude token usage tracker — Stop + PostToolUse(exit_plan_mode) hook
# Writes to SQLite (~/.ctu/usage.db) and log file (~/.ctu/token-usage.log)

input=$(cat)

HOOK_INPUT="$input" python3 - <<'EOF'
import json, os, sys, sqlite3, re
from datetime import datetime, timezone
from pathlib import Path

CTU_DIR = Path.home() / ".ctu"
DB_FILE = CTU_DIR / "usage.db"

data = json.loads(os.environ["HOOK_INPUT"])
transcript_path = data.get("transcript_path", "")
session_id = data.get("session_id", "") or data.get("sessionId", "")

if not transcript_path or not os.path.exists(transcript_path):
    sys.exit(0)

# ── Derive session_id from transcript path if not provided ──────────────
if not session_id:
    session_id = Path(transcript_path).stem  # UUID from filename

# ── Internal prompt patterns to skip ────────────────────────────────────
SKIP_PATTERNS = [
    "Base directory for this skill:",
    "ARGUMENTS:",
    "This skill only loads",
    "# Smart Explore",
    "# RTK - Rust Token Killer",
    "# currentDate",
    "# claudeMd",
]

def is_internal_prompt(text):
    if not text:
        return True
    t = text.strip()
    for pat in SKIP_PATTERNS:
        if t.startswith(pat) or pat in t[:200]:
            return True
    return False

# ── Read transcript ──────────────────────────────────────────────────────
with open(transcript_path) as f:
    entries = [json.loads(l.strip()) for l in f if l.strip()]

# ── Find last user prompt (human type only, skip internal) ───────────────
last_prompt = ""
for entry in entries:
    if entry.get("type") != "user":
        continue
    content = entry.get("message", {}).get("content", "")
    # Only accept human-originated messages, not local-command-stdout
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text and not is_internal_prompt(text):
                    last_prompt = text
                    break
    elif isinstance(content, str):
        text = content.strip()
        if text and not is_internal_prompt(text):
            last_prompt = text

# ── Find current turn's assistant entries (all API calls since last user prompt) ──
def is_real_user_message(entry):
    """True if this is an actual human prompt, not a tool_result or system message."""
    if entry.get("type") != "user":
        return False
    content = entry.get("message", {}).get("content", "")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return False
        return any(
            isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
            for b in content
        )
    return False

# Bound current turn: everything after the last real user message
last_real_user_idx = -1
for i, entry in enumerate(entries):
    if is_real_user_message(entry):
        last_real_user_idx = i

# Also track overall last assistant index (needed for compact detection)
last_assistant_idx = None
for i, entry in enumerate(entries):
    if entry.get("type") == "assistant":
        last_assistant_idx = i

if last_assistant_idx is None:
    sys.exit(0)

# Collect all assistant entries in the current turn
turn_assistants = [
    e for i, e in enumerate(entries)
    if e.get("type") == "assistant" and i > last_real_user_idx
]

if not turn_assistants:
    sys.exit(0)

# Deduplicate: identical (in, out, cr, cw) = same API call recorded multiple times
seen_keys = set()
unique_assistants = []
for e in turn_assistants:
    u = e.get("message", {}).get("usage", {})
    key = (u.get("input_tokens", 0), u.get("output_tokens", 0),
           u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))
    if key not in seen_keys:
        seen_keys.add(key)
        unique_assistants.append(e)

# Sum tokens across all unique API calls in this turn
m_in = sum(e.get("message", {}).get("usage", {}).get("input_tokens", 0)              for e in unique_assistants)
m_out = sum(e.get("message", {}).get("usage", {}).get("output_tokens", 0)             for e in unique_assistants)
m_cr  = sum(e.get("message", {}).get("usage", {}).get("cache_read_input_tokens", 0)   for e in unique_assistants)
m_cw  = sum(e.get("message", {}).get("usage", {}).get("cache_creation_input_tokens", 0) for e in unique_assistants)

# Tool/agent counts across ALL assistant entries in the turn
main_tool_count  = 0
main_agent_count = 0
for e in turn_assistants:
    for block in e.get("message", {}).get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            main_tool_count += 1
            if block.get("name") == "Agent":
                main_agent_count += 1

# Model from last assistant entry
model = turn_assistants[-1].get("message", {}).get("model", "")

# ── Detect compact overhead ──────────────────────────────────────────────
# Look for 'summary' type entry between second-to-last and last assistant
is_compact_overhead = False
found_prev_assistant = False
for entry in reversed(entries[:last_assistant_idx]):
    if entry.get("type") == "summary":
        is_compact_overhead = True
        break
    elif entry.get("type") == "assistant":
        break  # Only check between last two assistant turns

# ── Subagent accumulator ─────────────────────────────────────────────────
accumulator_path = CTU_DIR / "subagent-accumulator.json"
subagents = []
if accumulator_path.exists():
    try:
        with open(accumulator_path) as f:
            subagents = json.load(f)
    except: pass
    accumulator_path.unlink(missing_ok=True)

s_in = s_out = s_cr = s_cw = s_tools = 0
for s in subagents:
    s_in    += s.get("input_tokens", 0)
    s_out   += s.get("output_tokens", 0)
    s_cr    += s.get("cache_read", 0)
    s_cw    += s.get("cache_write", 0)
    s_tools += s.get("tool_count", 0)

# ── Pricing: load from cache, fallback to defaults ───────────────────────
def load_pricing():
    for path in [CTU_DIR / "pricing.json", CTU_DIR / "pricing_defaults.json"]:
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return data.get("models", {})
            except: pass
    return {}

PRICING = load_pricing()

# Check if pricing is stale (>7 days), trigger background refresh
pricing_file = CTU_DIR / "pricing.json"
needs_refresh = True
if pricing_file.exists():
    try:
        d = json.loads(pricing_file.read_text())
        fetched = d.get("fetched_at")
        if fetched:
            from datetime import timedelta
            age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched)
            needs_refresh = age.days >= 7
    except: pass

if needs_refresh:
    fetch_script = CTU_DIR / "bin" / "fetch-pricing.py"
    if fetch_script.exists():
        import subprocess
        subprocess.Popen(
            ["python3", str(fetch_script)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True
        )

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

prices = lookup_pricing(model)
if prices:
    p_in, p_out, p_cr, p_cw = prices
else:
    p_in  = float(os.environ.get("PRICE_INPUT",       "3.00"))
    p_out = float(os.environ.get("PRICE_OUTPUT",      "15.00"))
    p_cr  = float(os.environ.get("PRICE_CACHE_READ",  "0.30"))
    p_cw  = float(os.environ.get("PRICE_CACHE_WRITE", "3.75"))

# ── Cost calculation ─────────────────────────────────────────────────────
t_in  = m_in  + s_in
t_out = m_out + s_out
t_cr  = m_cr  + s_cr
t_cw  = m_cw  + s_cw
total = t_in + t_out + t_cr + t_cw

cost = (
    (t_in  / 1_000_000 * p_in)  +
    (t_out / 1_000_000 * p_out) +
    (t_cr  / 1_000_000 * p_cr)  +
    (t_cw  / 1_000_000 * p_cw)
)

# ── SQLite write ─────────────────────────────────────────────────────────
CTU_DIR.mkdir(parents=True, exist_ok=True)
conn = sqlite3.connect(DB_FILE)

# Ensure schema exists
conn.executescript("""
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, project TEXT, account_email TEXT,
    started_at TIMESTAMP, model TEXT,
    total_input_tokens INTEGER DEFAULT 0, total_output_tokens INTEGER DEFAULT 0,
    total_cache_read INTEGER DEFAULT 0, total_cache_write INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0
);
CREATE TABLE IF NOT EXISTS prompts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, prompt_index INTEGER,
    prompt_text TEXT, model TEXT, timestamp TIMESTAMP,
    is_compact_overhead BOOLEAN DEFAULT 0,
    input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
    cost REAL, tool_count INTEGER, agent_count INTEGER
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, prompt_id INTEGER,
    transcript_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cache_read INTEGER, cache_write INTEGER, tool_count INTEGER, cost REAL
);
CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id);
CREATE INDEX IF NOT EXISTS idx_prompts_ts ON prompts(timestamp);
CREATE INDEX IF NOT EXISTS idx_agents_prompt ON agent_runs(prompt_id);
""")

# Account email
account_file = CTU_DIR / "current-account.txt"
account_email = account_file.read_text().strip() if account_file.exists() else "unknown"

# Project from cwd
project = data.get("cwd", "")
if project:
    project = str(Path(project).name)

# Insert/update session
ts_now = datetime.now(timezone.utc).isoformat()
conn.execute("""
    INSERT INTO sessions (id, project, account_email, started_at, model,
        total_input_tokens, total_output_tokens, total_cache_read, total_cache_write, total_cost)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(id) DO UPDATE SET
        total_input_tokens  = total_input_tokens  + excluded.total_input_tokens,
        total_output_tokens = total_output_tokens + excluded.total_output_tokens,
        total_cache_read    = total_cache_read    + excluded.total_cache_read,
        total_cache_write   = total_cache_write   + excluded.total_cache_write,
        total_cost          = total_cost          + excluded.total_cost,
        model = COALESCE(excluded.model, model)
""", (session_id, project, account_email, ts_now, model,
      t_in, t_out, t_cr, t_cw, cost))

# Prompt index
prompt_count = conn.execute("SELECT COUNT(*) FROM prompts WHERE session_id=?", (session_id,)).fetchone()[0]

# Insert prompt
cur = conn.execute("""
    INSERT INTO prompts (session_id, prompt_index, prompt_text, model, timestamp,
                         is_compact_overhead, input_tokens, output_tokens,
                         cache_read, cache_write, cost, tool_count, agent_count)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
""", (session_id, prompt_count, last_prompt[:500], model, ts_now,
      1 if is_compact_overhead else 0,
      m_in, m_out, m_cr, m_cw, cost, main_tool_count, main_agent_count))
prompt_id = cur.lastrowid

# Insert agent_runs
for s in subagents:
    # Per-agent cost (proportional to token share)
    if total > 0:
        agent_total = s.get("input_tokens", 0) + s.get("output_tokens", 0) + s.get("cache_read", 0) + s.get("cache_write", 0)
        agent_cost = (
            (s.get("input_tokens", 0)  / 1_000_000 * p_in)  +
            (s.get("output_tokens", 0) / 1_000_000 * p_out) +
            (s.get("cache_read", 0)    / 1_000_000 * p_cr)  +
            (s.get("cache_write", 0)   / 1_000_000 * p_cw)
        )
    else:
        agent_cost = 0.0
    conn.execute("""
        INSERT INTO agent_runs (prompt_id, transcript_id, input_tokens, output_tokens,
                                 cache_read, cache_write, tool_count, cost)
        VALUES (?,?,?,?,?,?,?,?)
    """, (prompt_id, s.get("transcript", ""), s.get("input_tokens", 0),
          s.get("output_tokens", 0), s.get("cache_read", 0), s.get("cache_write", 0),
          s.get("tool_count", 0), agent_cost))

conn.commit()
conn.close()

# ── Format display output ────────────────────────────────────────────────
preview = (last_prompt[:60] + "…") if len(last_prompt) > 60 else last_prompt
preview = preview.replace("\n", " ")

main_ctx = m_in + m_cr + m_cw
t_ctx    = t_in + t_cr + t_cw

lines = []
model_label = f" [{model}]" if model else ""
ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
compact_flag = " [compact-overhead]" if is_compact_overhead else ""
lines.append(f'[tokens]{model_label} [{ts}]{compact_flag} "{preview}"')

# Main session row
main_total = m_in + m_out + m_cr + m_cw
main_parts = [f"in={m_in:,}", f"out={m_out:,}"]
if m_cr: main_parts.append(f"cache_hit={m_cr:,}")
if m_cw: main_parts.append(f"cache_write={m_cw:,}")
main_parts.append(f"ctx={main_ctx:,}")
main_parts.append(f"total={main_total:,}")
main_parts.append(f"tools={main_tool_count}")
lines.append(f"  main    : {' | '.join(main_parts)}")

# Subagent rows
for s in subagents:
    s_total = s['input_tokens'] + s['output_tokens'] + s['cache_read'] + s['cache_write']
    s_ctx   = s['input_tokens'] + s['cache_read'] + s['cache_write']
    name = s['transcript'][:8]
    lines.append(f"  agent   : in={s['input_tokens']:,} | out={s['output_tokens']:,} | ctx={s_ctx:,} | total={s_total:,} | tools={s['tool_count']} [{name}]")

total_line = f"  TOTAL   : ctx={t_ctx:,} | total={total:,} tokens | ${cost:.4f} | {len(subagents)} agent(s) | {main_tool_count + s_tools} tool(s)"
lines.append(total_line)

output = "\n".join(lines)

# Log to file (backup)
log_file = CTU_DIR / "token-usage.log"
with open(log_file, "a") as f:
    f.write(output + "\n\n")

# Save for display hook
stats_file = CTU_DIR / "last-token-stats.txt"
with open(stats_file, "w") as f:
    f.write(output)
EOF
