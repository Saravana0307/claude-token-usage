#!/usr/bin/env python3
"""
Initialize SQLite schema and migrate existing token-usage.log entries.
Run once on install: python3 migrate.py
"""

import json, re, sqlite3, sys
from pathlib import Path
from datetime import datetime

CTU_DIR  = Path.home() / ".ctu"
DB_FILE  = CTU_DIR / "usage.db"
LOG_FILE = Path.home() / ".claude" / "token-usage.log"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    project         TEXT,
    account_email   TEXT,
    started_at      TIMESTAMP,
    model           TEXT,
    total_input_tokens  INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_cache_read    INTEGER DEFAULT 0,
    total_cache_write   INTEGER DEFAULT 0,
    total_cost          REAL    DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS prompts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT REFERENCES sessions(id),
    prompt_index    INTEGER,
    prompt_text     TEXT,
    model           TEXT,
    timestamp       TIMESTAMP,
    is_compact_overhead BOOLEAN DEFAULT 0,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_read      INTEGER,
    cache_write     INTEGER,
    cost            REAL,
    tool_count      INTEGER,
    agent_count     INTEGER
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id       INTEGER REFERENCES prompts(id),
    transcript_id   TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cache_read      INTEGER,
    cache_write     INTEGER,
    tool_count      INTEGER,
    cost            REAL
);

CREATE INDEX IF NOT EXISTS idx_prompts_session ON prompts(session_id);
CREATE INDEX IF NOT EXISTS idx_prompts_ts      ON prompts(timestamp);
CREATE INDEX IF NOT EXISTS idx_agents_prompt   ON agent_runs(prompt_id);
"""


def init_db():
    CTU_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def parse_log_blocks(text):
    """Parse token-usage.log into structured records."""
    records = []
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]

    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        header = lines[0]

        # Parse header: [tokens] [model] [timestamp] "prompt"
        model = ts = prompt = ""
        m = re.match(r'\[tokens\](?:\s+\[([^\]]+)\])?(?:\s+\[([^\]]+)\])?\s+"?(.*?)"?\s*$', header)
        if m:
            g1, g2, g3 = m.group(1) or "", m.group(2) or "", m.group(3) or ""
            if g1 and re.match(r'\d{4}-\d{2}-\d{2}', g1):
                ts, model = g1, ""
            elif g1:
                model = g1
                if g2 and re.match(r'\d{4}-\d{2}-\d{2}', g2):
                    ts = g2
            prompt = g3.strip().strip('"')
        else:
            # Old format: tokens: in=X | ...
            prompt = header.replace('[tokens]', '').strip().strip('"')

        # Parse main line
        main_line  = next((l for l in lines if '  main' in l), '')
        total_line = next((l for l in lines if 'TOTAL' in l), '')
        agent_lines = [l for l in lines if '  agent' in l]

        m_in = m_out = m_cr = m_cw = m_tools = 0
        mm = re.search(r'in=([\d,]+).*?out=([\d,]+)', main_line)
        if mm:
            m_in  = int(mm.group(1).replace(',',''))
            m_out = int(mm.group(2).replace(',',''))
        cr = re.search(r'cache_hit=([\d,]+)', main_line)
        cw = re.search(r'cache_write=([\d,]+)', main_line)
        if cr: m_cr = int(cr.group(1).replace(',',''))
        if cw: m_cw = int(cw.group(1).replace(',',''))
        mt = re.search(r'tools=(\d+)', main_line)
        if mt: m_tools = int(mt.group(1))

        cost = agents_count = tools_total = 0
        ct = re.search(r'\$([\d.]+)', total_line)
        ag = re.search(r'(\d+)\s*agent', total_line)
        tl = re.search(r'(\d+)\s*tool', total_line)
        if ct: cost         = float(ct.group(1))
        if ag: agents_count = int(ag.group(1))
        if tl: tools_total  = int(tl.group(1))

        # Parse agent sub-lines
        agents = []
        for al in agent_lines:
            a_in = a_out = a_cr = a_cw = a_tools = 0
            a_tid = ""
            am = re.search(r'in=([\d,]+).*?out=([\d,]+)', al)
            if am:
                a_in  = int(am.group(1).replace(',',''))
                a_out = int(am.group(2).replace(',',''))
            acr = re.search(r'cache_hit=([\d,]+)', al)
            acw = re.search(r'cache_write=([\d,]+)', al)
            if acr: a_cr = int(acr.group(1).replace(',',''))
            if acw: a_cw = int(acw.group(1).replace(',',''))
            atl = re.search(r'tools=(\d+)', al)
            if atl: a_tools = int(atl.group(1))
            tid = re.search(r'\[([0-9a-f]{8})\]', al)
            if tid: a_tid = tid.group(1)
            agents.append({
                "transcript_id": a_tid,
                "input_tokens":  a_in,
                "output_tokens": a_out,
                "cache_read":    a_cr,
                "cache_write":   a_cw,
                "tool_count":    a_tools,
                "cost":          0.0,  # Not stored per-agent in log format
            })

        if m_in + m_out + m_cr + m_cw + cost > 0:
            records.append({
                "prompt":    prompt,
                "model":     model,
                "ts":        ts,
                "in":        m_in,
                "out":       m_out,
                "cache_read":  m_cr,
                "cache_write": m_cw,
                "cost":      cost,
                "tools":     m_tools,
                "agents":    agents_count,
                "agent_list": agents,
            })

    return records


def migrate_log(conn):
    if not LOG_FILE.exists():
        print("No log file to migrate.")
        return 0

    text = LOG_FILE.read_text()
    records = parse_log_blocks(text)
    if not records:
        print("No records found in log file.")
        return 0

    # Group records into a synthetic "log-migration" session
    session_id = "log-migration-0000-0000-0000-000000000000"
    account    = "migrated"

    # Check if already migrated
    existing = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if existing:
        print(f"Log already migrated ({len(records)} records exist). Skipping.")
        return 0

    started_at = records[0]["ts"] if records else datetime.now().isoformat()

    # Insert session
    total_cost = sum(r["cost"] for r in records)
    total_in   = sum(r["in"]   for r in records)
    total_out  = sum(r["out"]  for r in records)
    total_cr   = sum(r["cache_read"]  for r in records)
    total_cw   = sum(r["cache_write"] for r in records)

    conn.execute("""
        INSERT OR IGNORE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (session_id, None, account, started_at,
          records[0]["model"] if records else None,
          total_in, total_out, total_cr, total_cw, total_cost))

    # Insert prompts + agent_runs
    for i, r in enumerate(records):
        cur = conn.execute("""
            INSERT INTO prompts (session_id, prompt_index, prompt_text, model, timestamp,
                                 is_compact_overhead, input_tokens, output_tokens,
                                 cache_read, cache_write, cost, tool_count, agent_count)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (session_id, i, r["prompt"], r["model"], r["ts"],
              0, r["in"], r["out"], r["cache_read"], r["cache_write"],
              r["cost"], r["tools"], r["agents"]))
        prompt_id = cur.lastrowid

        for ag in r["agent_list"]:
            conn.execute("""
                INSERT INTO agent_runs (prompt_id, transcript_id, input_tokens, output_tokens,
                                        cache_read, cache_write, tool_count, cost)
                VALUES (?,?,?,?,?,?,?,?)
            """, (prompt_id, ag["transcript_id"], ag["input_tokens"], ag["output_tokens"],
                  ag["cache_read"], ag["cache_write"], ag["tool_count"], ag["cost"]))

    conn.commit()
    print(f"Migrated {len(records)} records from {LOG_FILE}")
    return len(records)


def main():
    print(f"Initializing database at {DB_FILE}...")
    conn = init_db()
    print("Schema created.")

    print("Migrating existing log data...")
    count = migrate_log(conn)
    if count:
        print(f"Migration complete: {count} prompts imported.")
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
