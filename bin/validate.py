#!/usr/bin/env python3
"""
Validate claude-token-usage SQLite data against transcript JSONL sources.

Usage:
  python3 validate.py                    # Full validation report
  python3 validate.py --session <uuid>   # Validate one session
  python3 validate.py --subagents        # Check agent_runs linkage
  python3 validate.py --migration-check  # Compare log totals vs SQLite
  python3 validate.py --pricing          # Show current pricing cache
"""

import json, re, sqlite3, sys, argparse
from pathlib import Path
from datetime import datetime

CTU_DIR     = Path.home() / ".ctu"
DB_FILE     = CTU_DIR / "usage.db"
LOG_FILE    = Path.home() / ".claude" / "token-usage.log"
STATS_CACHE = Path.home() / ".claude" / "stats-cache.json"
PROJECTS    = Path.home() / ".claude" / "projects"
PRICING     = CTU_DIR / "pricing.json"
DEFAULTS    = CTU_DIR / "pricing_defaults.json"


def connect():
    if not DB_FILE.exists():
        print(f"ERROR: Database not found at {DB_FILE}")
        sys.exit(1)
    return sqlite3.connect(DB_FILE)


# ── Transcript helpers ────────────────────────────────────────────────────────

def find_transcript(session_id):
    """Find transcript file for a session UUID."""
    for p in PROJECTS.rglob(f"{session_id}.jsonl"):
        return p
    # Partial match
    for p in PROJECTS.rglob("*.jsonl"):
        if session_id in p.name:
            return p
    return None


def sum_transcript_tokens(path):
    """Sum all assistant turn tokens in a transcript."""
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("type") == "assistant":
                u = entry.get("message", {}).get("usage", {})
                totals["input"]      += u.get("input_tokens", 0)
                totals["output"]     += u.get("output_tokens", 0)
                totals["cache_read"] += u.get("cache_read_input_tokens", 0)
                totals["cache_write"]+= u.get("cache_creation_input_tokens", 0)
    return totals


def count_summary_entries(path):
    """Count summary-type entries (post-compact markers) in transcript."""
    count = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "summary":
                    count += 1
            except Exception:
                pass
    return count


# ── Validation checks ─────────────────────────────────────────────────────────

def check_session(conn, session_id):
    """Validate one session's SQLite totals against transcript."""
    cur = conn.cursor()

    # Get session from DB
    row = cur.execute("SELECT id, total_input_tokens, total_output_tokens, total_cache_read, total_cache_write, total_cost FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        # Try partial match
        rows = cur.execute("SELECT id FROM sessions WHERE id LIKE ?", (f"%{session_id}%",)).fetchall()
        if not rows:
            print(f"  ERROR: Session {session_id} not found in DB")
            return False
        session_id = rows[0][0]
        row = cur.execute("SELECT id, total_input_tokens, total_output_tokens, total_cache_read, total_cache_write, total_cost FROM sessions WHERE id = ?", (session_id,)).fetchone()

    db_in, db_out, db_cr, db_cw, db_cost = row[1], row[2], row[3], row[4], row[5]

    # Find transcript
    transcript = find_transcript(session_id)
    if not transcript:
        print(f"  SKIP: Transcript not found for {session_id[:8]}")
        return True

    t = sum_transcript_tokens(transcript)

    # Compare (allow ±5 rounding tolerance)
    ok = True
    checks = [
        ("input_tokens",  db_in,  t["input"]),
        ("output_tokens", db_out, t["output"]),
        ("cache_read",    db_cr,  t["cache_read"]),
        ("cache_write",   db_cw,  t["cache_write"]),
    ]
    for name, db_val, tx_val in checks:
        diff = abs(db_val - tx_val)
        status = "✓" if diff <= 5 else "✗"
        if diff > 5:
            ok = False
        print(f"  {status} {name}: DB={db_val:,}  transcript={tx_val:,}  diff={diff:,}")

    # Check compact overhead flags
    summary_count = count_summary_entries(transcript)
    compact_rows = cur.execute("SELECT COUNT(*) FROM prompts WHERE session_id=? AND is_compact_overhead=1", (session_id,)).fetchone()[0]
    compact_ok = (summary_count == 0) or (compact_rows > 0)
    print(f"  {'✓' if compact_ok else '⚠'} compact: {summary_count} summary entries, {compact_rows} flagged prompts")

    return ok


def check_internal_prompts(conn):
    """Check for internal/skill prompts that should have been filtered."""
    cur = conn.cursor()
    bad_patterns = [
        "Base directory for this skill",
        "ARGUMENTS:",
        "This skill only loads",
        "# Smart Explore",
        "Contents of /Users/",
        "local-command-stdout",
    ]
    found = 0
    for pat in bad_patterns:
        rows = cur.execute("SELECT COUNT(*) FROM prompts WHERE prompt_text LIKE ?", (f"%{pat}%",)).fetchone()[0]
        if rows:
            found += rows
            print(f"  ✗ Found {rows} prompts containing '{pat[:40]}'")
    if found == 0:
        print("  ✓ No internal prompts in DB")
    return found == 0


def check_subagent_linkage(conn):
    """Check agent_runs are properly linked to prompts."""
    cur = conn.cursor()
    orphans = cur.execute("SELECT COUNT(*) FROM agent_runs WHERE prompt_id IS NULL").fetchone()[0]
    total   = cur.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0]
    linked  = total - orphans
    ok = orphans == 0
    print(f"  {'✓' if ok else '✗'} agent_runs: {linked} linked, {orphans} orphaned (total {total})")
    return ok


def check_migration(conn):
    """Compare log file totals vs SQLite totals."""
    if not LOG_FILE.exists():
        print("  SKIP: No log file to compare")
        return True

    # Parse log file
    text = LOG_FILE.read_text()
    log_cost = 0.0
    log_total = 0
    for line in text.splitlines():
        m = re.search(r'\$([\d.]+)', line)
        if m and 'TOTAL' in line:
            log_cost += float(m.group(1))
        m2 = re.search(r'total=([\d,]+)\s+tokens', line)
        if m2 and 'TOTAL' in line:
            log_total += int(m2.group(1).replace(',', ''))

    # SQLite totals
    row = conn.execute("SELECT COALESCE(SUM(total_cost),0), COALESCE(SUM(total_input_tokens+total_output_tokens+total_cache_read+total_cache_write),0) FROM sessions").fetchone()
    db_cost, db_tokens = row[0], row[1]

    cost_diff = abs(log_cost - db_cost)
    tok_diff  = abs(log_total - db_tokens)

    print(f"  Log cost:    ${log_cost:.4f}")
    print(f"  SQLite cost: ${db_cost:.4f}  diff=${cost_diff:.4f}")
    print(f"  Log tokens:    {log_total:,}")
    print(f"  SQLite tokens: {db_tokens:,}  diff={tok_diff:,}")
    ok = cost_diff < 0.01 and tok_diff < 1000
    print(f"  {'✓' if ok else '⚠'} Migration check {'passed' if ok else 'has discrepancies'}")
    return ok


def check_stats_cache(conn):
    """Compare daily token counts in SQLite against ~/.claude/stats-cache.json.

    stats-cache.json is the ground truth maintained by Claude Code itself.
    It stores total tokens per model per day across all sessions.
    Our DB should be a subset of (or equal to) those totals for dates we have data.
    """
    if not STATS_CACHE.exists():
        print("  SKIP: ~/.claude/stats-cache.json not found")
        return True

    cache = json.loads(STATS_CACHE.read_text())
    daily = {entry["date"]: entry["tokensByModel"] for entry in cache.get("dailyModelTokens", [])}

    # Get our daily totals from SQLite (sum all token types)
    rows = conn.execute("""
        SELECT DATE(timestamp) AS d,
               model,
               SUM(input_tokens + output_tokens + cache_read + cache_write) AS tokens
        FROM prompts
        WHERE timestamp IS NOT NULL AND model IS NOT NULL AND model != ''
        GROUP BY DATE(timestamp), model
        ORDER BY d DESC
    """).fetchall()

    if not rows:
        print("  SKIP: No dated prompts in DB yet")
        return True

    print(f"  {'Date':<12} {'Model':<32} {'CTU DB':>12} {'stats-cache':>12} {'Diff':>10} {'Status'}")
    print(f"  {'-'*11} {'-'*31} {'-'*12} {'-'*12} {'-'*10} {'-'*6}")

    all_ok = True
    for row in rows:
        date, model, db_tokens = row[0], row[1], row[2] or 0
        sc_tokens = daily.get(date, {}).get(model, None)

        if sc_tokens is None:
            # stats-cache may aggregate under a shorter model alias
            # Try prefix match
            for sc_model, sc_tok in daily.get(date, {}).items():
                if model.startswith(sc_model) or sc_model.startswith(model.split("-2")[0]):
                    sc_tokens = sc_tok
                    break

        if sc_tokens is None:
            print(f"  {date:<12} {model:<32} {db_tokens:>12,} {'(no entry)':>12} {'':>10} {'?'}")
            continue

        diff = db_tokens - sc_tokens
        # CTU can be <= stats-cache (stats-cache includes ALL sessions, CTU only those
        # that fired the Stop hook). Allow CTU to be lower but not much higher.
        if diff > sc_tokens * 0.05:   # CTU > 5% above stats-cache: suspicious
            status = "✗ HIGH"
            all_ok = False
        elif db_tokens < sc_tokens * 0.5:  # CTU < 50% of stats-cache: possible missed prompts
            status = "⚠ LOW"
        else:
            status = "✓"
        print(f"  {date:<12} {model:<32} {db_tokens:>12,} {sc_tokens:>12,} {diff:>+10,} {status}")

    print()
    # Summary: stats-cache totals vs our totals
    sc_total = sum(sum(m.values()) for m in daily.values())
    db_row = conn.execute("SELECT COALESCE(SUM(input_tokens+output_tokens+cache_read+cache_write),0) FROM prompts").fetchone()
    db_total = db_row[0] or 0
    print(f"  Stats-cache all-time total: {sc_total:,} tokens")
    print(f"  CTU DB all-time total:      {db_total:,} tokens")
    print(f"  Coverage: {db_total/sc_total*100:.1f}% of Claude Code's recorded usage")
    print()
    print("  Note: CTU only tracks sessions where the Stop hook fired.")
    print("  API sessions, IDE sessions, or pre-install sessions won't appear.")
    return all_ok


def check_pricing():
    """Show current pricing cache status."""
    for path in [PRICING, DEFAULTS]:
        if path.exists():
            data = json.loads(path.read_text())
            fetched = data.get("fetched_at", "never")
            models  = data.get("models", {})
            print(f"  {path.name}: {len(models)} models, fetched={fetched}")
            for m, prices in sorted(models.items()):
                print(f"    {m:<40} in=${prices[0]:.2f} out=${prices[1]:.2f} cr=${prices[2]:.3f} cw=${prices[3]:.2f}")
        else:
            print(f"  MISSING: {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", help="Validate a specific session UUID")
    parser.add_argument("--subagents", action="store_true")
    parser.add_argument("--migration-check", action="store_true")
    parser.add_argument("--pricing", action="store_true")
    parser.add_argument("--stats-cache", action="store_true", help="Compare daily tokens against ~/.claude/stats-cache.json")
    args = parser.parse_args()

    if args.pricing:
        print("=== Pricing Cache ===")
        check_pricing()
        return

    conn = connect()

    if args.session:
        print(f"=== Session Validation: {args.session} ===")
        check_session(conn, args.session)
        return

    if args.subagents:
        print("=== Subagent Linkage ===")
        check_subagent_linkage(conn)
        return

    if args.migration_check:
        print("=== Migration Check ===")
        check_migration(conn)
        return

    if args.stats_cache:
        print("=== Stats-Cache Validation ===")
        check_stats_cache(conn)
        return

    # Full validation
    print("=== Full Validation Report ===")
    print(f"DB: {DB_FILE}")
    row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()
    print(f"Sessions in DB: {row[0]}\n")

    print("--- Internal Prompt Filter ---")
    check_internal_prompts(conn)
    print()

    print("--- Subagent Linkage ---")
    check_subagent_linkage(conn)
    print()

    print("--- Migration Check ---")
    check_migration(conn)
    print()

    print("--- Stats-Cache Validation ---")
    check_stats_cache(conn)
    print()

    print("--- Session Sample (last 3) ---")
    sessions = conn.execute("SELECT id FROM sessions ORDER BY started_at DESC LIMIT 3").fetchall()
    for (sid,) in sessions:
        print(f"\nSession {sid[:8]}...")
        check_session(conn, sid)

    print()
    print("--- Pricing ---")
    check_pricing()

    conn.close()


if __name__ == "__main__":
    main()
