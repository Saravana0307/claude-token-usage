#!/usr/bin/env python3
"""Claude Token Usage — web UI server. Reads from ~/.ctu/usage.db."""

import calendar, json, os, re, sqlite3, subprocess, sys, threading, webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT       = int(os.environ.get("TOKEN_UI_PORT", 7123))
CTU_DIR    = Path.home() / ".ctu"
DB_FILE    = CTU_DIR / "usage.db"
CLAUDE_DIR   = Path.home() / ".claude"
PROJECTS_DIR = Path.home() / ".claude" / "projects"


# ── DB helpers ────────────────────────────────────────────────────────────────

def connect():
    if not DB_FILE.exists():
        return None
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def period_where(period, ts_col='timestamp'):
    """Returns 'AND DATE(col) >= ...' clause or '' for all-time."""
    today = datetime.now().date()
    if period == 'month':
        return f"AND DATE({ts_col}) >= '{today.replace(day=1).isoformat()}'"
    elif period == 'week':
        return f"AND DATE({ts_col}) >= '{(today - timedelta(days=6)).isoformat()}'"
    return ""


def get_summary(conn, period='all'):
    s_pw = period_where(period, 'started_at')
    p_pw = period_where(period, 'timestamp')
    s = conn.execute(f"""
        SELECT COALESCE(SUM(total_cost), 0)           AS cost,
               COALESCE(SUM(total_input_tokens), 0)   AS inp,
               COALESCE(SUM(total_output_tokens), 0)  AS out,
               COALESCE(SUM(total_cache_read), 0)     AS cr,
               COALESCE(SUM(total_cache_write), 0)    AS cw,
               COUNT(*)                               AS sessions
        FROM sessions WHERE 1=1 {s_pw}
    """).fetchone()
    p = conn.execute(f"SELECT COUNT(*) AS n FROM prompts WHERE 1=1 {p_pw}").fetchone()
    return {
        "cost":     round(s["cost"], 4),
        "inp":      s["inp"],
        "out":      s["out"],
        "cr":       s["cr"],
        "cw":       s["cw"],
        "tokens":   s["inp"] + s["out"] + s["cr"] + s["cw"],
        "sessions": s["sessions"],
        "prompts":  p["n"],
    }


def get_by_model(conn, period='all'):
    pw = period_where(period, 'timestamp')
    rows = conn.execute(f"""
        SELECT model, COUNT(*) AS prompts,
               SUM(input_tokens+output_tokens+cache_read+cache_write) AS tokens,
               SUM(cost) AS cost, SUM(tool_count) AS tools
        FROM prompts
        WHERE model IS NOT NULL AND model != '' {pw}
        GROUP BY model ORDER BY cost DESC
    """).fetchall()
    return [dict(r) for r in rows]


def get_chart_data(conn, period='all'):
    today = datetime.now().date()
    if period == 'week':
        days  = [today - timedelta(days=i) for i in range(6, -1, -1)]
        start = today - timedelta(days=6)
        title = "Last 7 Days"
    elif period == 'month':
        first     = today.replace(day=1)
        last_day  = calendar.monthrange(today.year, today.month)[1]
        days      = [first + timedelta(days=i) for i in range(last_day)]
        start     = first
        title     = f"This Month ({today.strftime('%B %Y')})"
    else:
        days  = [today - timedelta(days=i) for i in range(29, -1, -1)]
        start = today - timedelta(days=29)
        title = "Last 30 Days"

    day_map = {d.isoformat(): {"tokens": 0, "cost": 0.0} for d in days}
    rows = conn.execute("""
        SELECT DATE(timestamp) AS d,
               SUM(input_tokens+output_tokens+cache_read+cache_write) AS tokens,
               SUM(cost) AS cost
        FROM prompts WHERE timestamp >= ? GROUP BY DATE(timestamp)
    """, (start.isoformat(),)).fetchall()
    for r in rows:
        if r["d"] in day_map:
            day_map[r["d"]]["tokens"] += r["tokens"] or 0
            day_map[r["d"]]["cost"]   += r["cost"] or 0
    return {
        "title":  title,
        "labels": [d.strftime("%b %d") for d in days],
        "tokens": [day_map[d.isoformat()]["tokens"] for d in days],
        "costs":  [round(day_map[d.isoformat()]["cost"], 4) for d in days],
    }


def get_sessions(conn, page=1, page_size=10, period='all'):
    offset = (page - 1) * page_size
    pw = period_where(period, 'started_at')
    rows = conn.execute(f"""
        SELECT s.id, s.project, s.account_email, s.started_at, s.model,
               s.total_cost,
               s.total_input_tokens, s.total_output_tokens,
               s.total_cache_read,   s.total_cache_write,
               s.total_input_tokens + s.total_output_tokens +
               s.total_cache_read   + s.total_cache_write AS total_tokens,
               COUNT(p.id) AS prompt_count
        FROM sessions s LEFT JOIN prompts p ON p.session_id = s.id
        WHERE 1=1 {pw}
        GROUP BY s.id ORDER BY s.started_at DESC
        LIMIT ? OFFSET ?
    """, (page_size, offset)).fetchall()

    total = conn.execute(f"SELECT COUNT(*) AS n FROM sessions WHERE 1=1 {pw}").fetchone()["n"]
    session_list = []
    for r in rows:
        sid     = r["id"]
        prompts = get_prompts_for_session(conn, sid)
        session_list.append({
            "id":           sid,
            "project":      r["project"] or "",
            "account":      r["account_email"] or "unknown",
            "started_at":   r["started_at"] or "",
            "model":        r["model"] or "",
            "total_cost":   round(r["total_cost"] or 0, 4),
            "total_tokens": r["total_tokens"] or 0,
            "total_in":     r["total_input_tokens"] or 0,
            "total_out":    r["total_output_tokens"] or 0,
            "total_cr":     r["total_cache_read"] or 0,
            "total_cw":     r["total_cache_write"] or 0,
            "prompt_count": r["prompt_count"],
            "prompts":      prompts,
        })
    return session_list, total


def get_prompts_for_session(conn, session_id):
    rows = conn.execute("""
        SELECT id, prompt_index, prompt_text, model, timestamp,
               is_compact_overhead, input_tokens, output_tokens,
               cache_read, cache_write, cost, tool_count, agent_count
        FROM prompts WHERE session_id = ? ORDER BY prompt_index ASC
    """, (session_id,)).fetchall()
    prompts = []
    for r in rows:
        agents = get_agents_for_prompt(conn, r["id"])
        prompts.append({
            "id":               r["id"],
            "index":            r["prompt_index"],
            "text":             (r["prompt_text"] or "")[:120],
            "model":            r["model"] or "",
            "timestamp":        r["timestamp"] or "",
            "compact_overhead": bool(r["is_compact_overhead"]),
            "in":               r["input_tokens"] or 0,
            "out":              r["output_tokens"] or 0,
            "cache_read":       r["cache_read"] or 0,
            "cache_write":      r["cache_write"] or 0,
            "tokens":           (r["input_tokens"] or 0) + (r["output_tokens"] or 0) +
                                (r["cache_read"] or 0) + (r["cache_write"] or 0),
            "cost":             round(r["cost"] or 0, 4),
            "tools":            r["tool_count"] or 0,
            "agents":           r["agent_count"] or 0,
            "agent_list":       agents,
        })
    return prompts


def get_agents_for_prompt(conn, prompt_id):
    rows = conn.execute("""
        SELECT transcript_id, input_tokens, output_tokens, cache_read, cache_write, tool_count, cost
        FROM agent_runs WHERE prompt_id = ?
    """, (prompt_id,)).fetchall()
    return [{
        "transcript_id": r["transcript_id"] or "",
        "in":            r["input_tokens"] or 0,
        "out":           r["output_tokens"] or 0,
        "cache_read":    r["cache_read"] or 0,
        "cache_write":   r["cache_write"] or 0,
        "tokens":        (r["input_tokens"] or 0) + (r["output_tokens"] or 0) +
                         (r["cache_read"] or 0) + (r["cache_write"] or 0),
        "tools":         r["tool_count"] or 0,
        "cost":          round(r["cost"] or 0, 4),
    } for r in rows]


def get_validation_data():
    """Read stats-cache.json directly + CTU DB cost totals for cross-reference."""
    result = {"sc_models": [], "sc_summary": {}, "ctu_models": [], "pricing": {}}

    # ── stats-cache.json — Claude Code's authoritative token tracking ──
    stats_file = CLAUDE_DIR / "stats-cache.json"
    if stats_file.exists():
        try:
            data = json.loads(stats_file.read_text())
            for model, mu in data.get("modelUsage", {}).items():
                inp = mu.get("inputTokens", 0)
                out = mu.get("outputTokens", 0)
                cr  = mu.get("cacheReadInputTokens", 0)
                cw  = mu.get("cacheCreationInputTokens", 0)
                result["sc_models"].append({
                    "model": model, "inp": inp, "out": out, "cr": cr, "cw": cw,
                    "tokens": inp + out + cr + cw,
                })
            result["sc_models"].sort(key=lambda x: x["tokens"], reverse=True)
            result["sc_summary"] = {
                "last_computed":  data.get("lastComputedDate", ""),
                "total_sessions": data.get("totalSessions", 0),
                "total_messages": data.get("totalMessages", 0),
                "first_date":     data.get("firstSessionDate", ""),
            }
        except Exception as e:
            result["sc_error"] = str(e)
    else:
        result["sc_error"] = "~/.claude/stats-cache.json not found"

    # ── CTU DB — cost data only (tokens tracked since hook installation) ──
    conn = connect()
    if conn:
        try:
            rows = conn.execute("""
                SELECT model,
                       COALESCE(SUM(input_tokens+output_tokens+cache_read+cache_write),0) AS tokens,
                       COALESCE(SUM(cost), 0) AS cost,
                       COUNT(*)               AS prompts
                FROM prompts WHERE model IS NOT NULL AND model != ''
                GROUP BY model ORDER BY cost DESC
            """).fetchall()
            for r in rows:
                result["ctu_models"].append({
                    "model": r["model"], "tokens": r["tokens"],
                    "cost": round(r["cost"], 4), "prompts": r["prompts"],
                })
            row = conn.execute(
                "SELECT MIN(timestamp) AS first FROM prompts WHERE timestamp IS NOT NULL"
            ).fetchone()
            result["ctu_since"] = (row["first"] or "")[:10]
        finally:
            conn.close()

    # ── Pricing rates used for CTU cost calculation ──
    pricing_file = CTU_DIR / "pricing.json"
    if pricing_file.exists():
        try:
            data = json.loads(pricing_file.read_text())
            result["pricing"] = {
                "fetched_at": data.get("fetched_at", "unknown"),
                "models":     data.get("models", {}),
            }
        except Exception:
            pass

    return result


def _is_real_user_entry(entry):
    """True if this is an actual human prompt, not a tool_result or system message."""
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


def _dedup_sum_transcript(transcript_path):
    """Sum tokens from a transcript using per-turn deduplication (mirrors hook logic).

    Claude Code emits one API call per tool use. The transcript records every call,
    sometimes with consecutive duplicate entries (streaming artifacts). We reset
    dedup state on each real user message so each turn is deduped independently.
    """
    t = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    seen = set()
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if _is_real_user_entry(entry):
                    seen = set()  # new turn → reset dedup
                elif entry.get("type") == "assistant":
                    u = entry.get("message", {}).get("usage", {})
                    key = (u.get("input_tokens", 0), u.get("output_tokens", 0),
                           u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))
                    if key not in seen:
                        seen.add(key)
                        t["input"]      += u.get("input_tokens", 0)
                        t["output"]     += u.get("output_tokens", 0)
                        t["cache_read"] += u.get("cache_read_input_tokens", 0)
                        t["cache_write"]+= u.get("cache_creation_input_tokens", 0)
    except Exception as e:
        return None, str(e)
    t["total"] = t["input"] + t["output"] + t["cache_read"] + t["cache_write"]
    return t, None


def _get_turn_tokens(transcript_path, prompt_index):
    """Return deduplicated token sum for the Nth turn (0-indexed) in a transcript."""
    try:
        with open(transcript_path) as f:
            entries = [json.loads(l) for l in f if l.strip()]
    except Exception:
        return None, 0

    turns = []        # list of dicts (summed tokens per turn)
    cur_sum  = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    cur_seen = set()
    cur_last_ts = None
    in_turn = False

    for entry in entries:
        if _is_real_user_entry(entry):
            if in_turn:
                cur_sum["total"] = sum(cur_sum[k] for k in ("input", "output", "cache_read", "cache_write"))
                cur_sum["last_ts"] = cur_last_ts
                turns.append(cur_sum)
            cur_sum  = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
            cur_seen = set()
            cur_last_ts = None
            in_turn = True
        elif entry.get("type") == "assistant" and in_turn:
            u = entry.get("message", {}).get("usage", {})
            key = (u.get("input_tokens", 0), u.get("output_tokens", 0),
                   u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))
            if key not in cur_seen:
                cur_seen.add(key)
                cur_sum["input"]      += u.get("input_tokens", 0)
                cur_sum["output"]     += u.get("output_tokens", 0)
                cur_sum["cache_read"] += u.get("cache_read_input_tokens", 0)
                cur_sum["cache_write"]+= u.get("cache_creation_input_tokens", 0)
                cur_last_ts = entry.get("timestamp", cur_last_ts)

    if in_turn:
        cur_sum["total"] = sum(cur_sum[k] for k in ("input", "output", "cache_read", "cache_write"))
        cur_sum["last_ts"] = cur_last_ts
        turns.append(cur_sum)

    if prompt_index >= len(turns):
        return None, len(turns)
    return turns[prompt_index], len(turns)


def get_session_validation(session_id):
    """Compare CTU DB totals for a session against its transcript JSONL (ground truth)."""
    result = {"session_id": session_id, "transcript_found": False}

    # Find transcript JSONL
    transcript = None
    for p in PROJECTS_DIR.rglob(f"{session_id}.jsonl"):
        transcript = p
        break
    if not transcript:
        for p in PROJECTS_DIR.rglob("*.jsonl"):
            if session_id in p.name:
                transcript = p
                break

    result["transcript_found"] = bool(transcript)

    # Sum tokens from transcript using per-turn dedup (ground truth)
    if transcript:
        t, err = _dedup_sum_transcript(transcript)
        if err:
            result["transcript_error"] = err
        if t:
            result["transcript"] = t

    # CTU DB totals for this session
    conn = connect()
    if conn:
        try:
            row = conn.execute("""
                SELECT COALESCE(SUM(input_tokens), 0)  AS inp,
                       COALESCE(SUM(output_tokens), 0) AS out,
                       COALESCE(SUM(cache_read), 0)    AS cr,
                       COALESCE(SUM(cache_write), 0)   AS cw,
                       COUNT(*)                        AS prompts
                FROM prompts WHERE session_id = ?
            """, (session_id,)).fetchone()
            db = {
                "input": row["inp"], "output": row["out"],
                "cache_read": row["cr"], "cache_write": row["cw"],
                "total": row["inp"] + row["out"] + row["cr"] + row["cw"],
                "prompts": row["prompts"],
            }
            result["db"] = db
            if transcript and "transcript" in result:
                diff = db["total"] - result["transcript"]["total"]
                result["diff"]  = diff
                result["match"] = abs(diff) <= 5   # ±5 rounding tolerance
        finally:
            conn.close()

    return result


def get_prompt_validation(prompt_id):
    """Validate a single DB prompt against its corresponding assistant turn in the JSONL transcript.

    Strategy: the Stop hook reads the last assistant turn and records it with the current
    timestamp. We find the assistant turn in the transcript whose timestamp is closest to
    (and not after) the DB prompt's timestamp.
    """
    conn = connect()
    if not conn:
        return {"error": "DB not available"}

    try:
        row = conn.execute(
            "SELECT session_id, prompt_index, timestamp, input_tokens, output_tokens, cache_read, cache_write, cost, model "
            "FROM prompts WHERE id = ?", (prompt_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return {"error": f"Prompt {prompt_id} not found"}

    session_id = row["session_id"]
    db = {
        "input":       row["input_tokens"] or 0,
        "output":      row["output_tokens"] or 0,
        "cache_read":  row["cache_read"] or 0,
        "cache_write": row["cache_write"] or 0,
    }
    db["total"] = db["input"] + db["output"] + db["cache_read"] + db["cache_write"]
    result = {"prompt_id": prompt_id, "db": db, "model": row["model"]}

    # Find transcript
    transcript = None
    for p in PROJECTS_DIR.rglob(f"{session_id}.jsonl"):
        transcript = p; break
    if not transcript:
        for p in PROJECTS_DIR.rglob("*.jsonl"):
            if session_id in p.name:
                transcript = p; break

    if not transcript:
        result["transcript_found"] = False
        return result

    result["transcript_found"] = True

    # Find the Nth turn (0-indexed by prompt_index) and sum all its unique API calls
    prompt_index = row["prompt_index"]
    turn_tokens, turn_count = _get_turn_tokens(transcript, prompt_index)
    result["turn_count"] = turn_count

    if turn_tokens is None:
        result["error"] = f"Turn {prompt_index} not found (transcript has {turn_count} turns)"
        return result

    tx = {k: turn_tokens[k] for k in ("input", "output", "cache_read", "cache_write", "total")}
    tx["ts"] = turn_tokens.get("last_ts", "")
    result["transcript_turn"] = tx

    diff = db["total"] - tx["total"]
    result["diff"]  = diff
    result["match"] = abs(diff) <= 5

    return result


def get_data(page=1, period='all'):
    conn = connect()
    if not conn:
        return {"error": f"Database not found at {DB_FILE}. Run install first."}
    summary  = get_summary(conn, period)
    by_model = get_by_model(conn, period)
    chart    = get_chart_data(conn, period)
    sessions, total_sessions = get_sessions(conn, page, period=period)
    total_pages = max(1, (total_sessions + 9) // 10)
    conn.close()
    return {
        "summary":        summary,
        "by_model":       by_model,
        "chart":          chart,
        "sessions":       sessions,
        "page":           page,
        "total_pages":    total_pages,
        "total_sessions": total_sessions,
        "period":         period,
    }


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Claude Token Usage</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', monospace; background: #0d1117; color: #e6edf3; padding: 16px; font-size: 13px; }

  /* Top bar */
  .topbar { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
            padding: 12px 18px; margin-bottom: 14px; }
  .period-tabs { display: flex; gap: 4px; margin-bottom: 10px; }
  .period-tab  { background: #21262d; border: 1px solid #30363d; color: #7d8590;
                 padding: 4px 12px; border-radius: 4px; cursor: pointer; font-size: 11px;
                 font-family: inherit; }
  .period-tab:hover  { background: #30363d; color: #e6edf3; }
  .period-tab.active { background: #1f6feb; border-color: #388bfd; color: #e6edf3; }
  .topbar-metrics { display: flex; align-items: center; gap: 24px; flex-wrap: wrap; }
  .topbar-title { font-size: 10px; color: #7d8590; text-transform: uppercase; letter-spacing: 1px; display: block; }
  .topbar-val   { font-size: 18px; font-weight: bold; }
  .topbar-breakdown { font-size: 11px; color: #7d8590; display: flex; gap: 8px;
                      align-items: center; flex-wrap: wrap; }
  .topbar-breakdown .sep { color: #30363d; }

  .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 14px; margin-bottom: 14px; }
  .card-title { font-size: 10px; color: #7d8590; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; }

  .green  { color: #3fb950; } .red   { color: #f78166; } .blue { color: #58a6ff; }
  .yellow { color: #e3b341; } .dim   { color: #7d8590; } .orange { color: #d29922; }

  /* Model table */
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { text-align: left; color: #7d8590; font-weight: normal; padding: 5px 8px; border-bottom: 1px solid #21262d; }
  td { padding: 5px 8px; border-bottom: 1px solid #161b22; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px; }
  tr:hover td { background: #1c2128; }

  .badge { display: inline-block; background: #1f2d3d; border: 1px solid #30363d; color: #58a6ff;
           border-radius: 3px; padding: 1px 5px; font-size: 10px; white-space: nowrap; }
  .badge-compact { background: #2d1f0e; border-color: #d29922; color: #e3b341; }
  .badge-account { background: #1a2a1a; border-color: #3fb950; color: #3fb950; }

  /* Session list */
  .session { border: 1px solid #21262d; border-radius: 6px; margin-bottom: 8px; overflow: hidden; }
  .session-header { display: flex; align-items: center; gap: 10px; padding: 8px 12px;
                    background: #0d1117; cursor: pointer; flex-wrap: wrap; }
  .session-header:hover { background: #1c2128; }
  .session-meta { color: #7d8590; font-size: 11px; }
  .session-cost { font-size: 14px; font-weight: bold; color: #f78166; margin-left: auto; white-space: nowrap; }
  .chevron { color: #7d8590; font-size: 10px; transition: transform 0.2s; }
  .chevron.open { transform: rotate(90deg); }

  /* Always-visible session stats bar */
  .session-stats { display: flex; gap: 6px; padding: 5px 12px;
                   background: #0a0d12; border-top: 1px solid #21262d;
                   font-size: 11px; flex-wrap: wrap; align-items: center; }
  .stat-item { display: flex; gap: 3px; align-items: center; }
  .stat-sep  { color: #30363d; }
  .stat-lbl  { color: #7d8590; font-size: 10px; }

  /* Prompt list */
  .prompts { display: none; padding: 8px; background: #0a0d12; border-top: 1px solid #21262d; }
  .prompts.open { display: block; }
  .prompt-row { border: 1px solid #21262d; border-radius: 4px; margin-bottom: 6px; overflow: hidden; }
  .prompt-header { display: flex; align-items: flex-start; gap: 8px; padding: 7px 10px;
                   cursor: pointer; flex-wrap: wrap; }
  .prompt-header:hover { background: #1c2128; }
  .prompt-num  { color: #7d8590; font-size: 11px; min-width: 28px; padding-top: 1px; }
  .prompt-ts   { color: #7d8590; font-size: 10px; white-space: nowrap; padding-top: 2px; min-width: 36px; }
  .prompt-text { color: #e6edf3; font-size: 12px; flex: 1; min-width: 0; word-break: break-word; white-space: normal; }
  .prompt-stats { display: flex; gap: 8px; align-items: center; margin-left: auto; flex-shrink: 0; flex-wrap: wrap; }
  .prompt-mini  { display: flex; gap: 5px; font-size: 10px; align-items: center; }
  .prompt-mini .ml { color: #7d8590; }
  .prompt-cost  { font-size: 12px; font-weight: bold; color: #f78166; }

  /* Full breakdown bar (inside expandable) */
  .breakdown-bar { display: flex; gap: 12px; padding: 6px 10px; background: #0d1117;
                   border-top: 1px solid #21262d; font-size: 11px; flex-wrap: wrap; }
  .bk-item { display: flex; flex-direction: column; }
  .bk-val  { font-weight: bold; }
  .bk-lbl  { color: #7d8590; font-size: 10px; }

  /* Cost calculation (inside expandable) */
  .cost-calc { padding: 6px 10px 8px; background: #0d1117; border-top: 1px solid #21262d; }
  .cost-calc-title { color: #7d8590; font-size: 10px; text-transform: uppercase;
                     letter-spacing: 1px; display: block; margin-bottom: 4px; }
  .cost-calc table  { width: auto; }
  .cost-calc td     { padding: 2px 10px 2px 0; border-bottom: none; white-space: nowrap; max-width: none; }
  .cost-calc .total-row td { border-top: 1px solid #30363d; padding-top: 5px; }
  .cost-diff { font-size: 10px; color: #7d8590; margin-top: 4px; }

  /* Per-session validate button */
  .val-btn  { background: none; border: 1px solid #30363d; color: #7d8590; padding: 1px 7px;
              border-radius: 3px; cursor: pointer; font-size: 10px; font-family: inherit;
              margin-left: 6px; }
  .val-btn:hover { border-color: #58a6ff; color: #58a6ff; }
  .val-result { font-size: 10px; margin-left: 4px; white-space: nowrap; }
  .val-ok  { color: #3fb950; }
  .val-err { color: #f78166; }
  .val-na  { color: #7d8590; }

  /* Agent tree */
  .agent-tree { display: none; padding: 6px 10px 8px 36px; background: #0a0d12; border-top: 1px solid #21262d; }
  .agent-tree.open { display: block; }
  .agent-row { display: flex; gap: 8px; align-items: center; padding: 4px 0;
               border-bottom: 1px solid #161b22; font-size: 11px; flex-wrap: wrap; }
  .agent-row:last-child { border-bottom: none; }
  .tree-line { color: #30363d; }
  .agent-id   { color: #58a6ff; font-family: monospace; font-size: 10px; min-width: 80px; }
  .agent-stat { color: #7d8590; }
  .agent-cost { color: #f78166; font-weight: bold; }

  /* Grid */
  .grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  canvas { max-height: 200px; }

  /* Pagination */
  .pagination { display: flex; gap: 8px; align-items: center; justify-content: center; margin-top: 12px; }
  .btn { background: #21262d; border: 1px solid #30363d; color: #e6edf3; padding: 4px 12px;
         border-radius: 4px; cursor: pointer; font-size: 12px; font-family: inherit; }
  .btn:hover { background: #30363d; } .btn:disabled { opacity: 0.4; cursor: default; }
  .page-info { color: #7d8590; font-size: 12px; }
  .btn-validate { background: #1a2a1a; border-color: #3fb950; color: #3fb950; margin-left: auto; }
  .btn-validate:hover { background: #203a20; }

  /* Validation modal */
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.7); display: flex;
                   align-items: center; justify-content: center; z-index: 100; }
  .modal { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 20px; max-width: 760px; width: 94%; max-height: 82vh;
           overflow-y: auto; position: relative; }
  .modal-close { position: absolute; top: 12px; right: 14px; background: none;
                 border: none; color: #7d8590; font-size: 16px; cursor: pointer; padding: 0; }
  .modal h3 { color: #58a6ff; margin-bottom: 14px; font-size: 14px; }
  .modal h4 { color: #7d8590; font-size: 10px; text-transform: uppercase; letter-spacing: 1px;
              margin: 14px 0 6px; }
  .validate-note { font-size: 11px; color: #7d8590; line-height: 1.6; margin-top: 12px;
                   padding: 10px; background: #0d1117; border-radius: 4px; border: 1px solid #21262d; }
  .validate-note code { color: #58a6ff; }

  @media (max-width: 700px) {
    .grid2 { grid-template-columns: 1fr; }
    .topbar-metrics { gap: 12px; }
  }
</style>
</head>
<body>

<!-- Top bar: period tabs + stats -->
<div class="topbar">
  <div class="period-tabs">
    <button class="period-tab active" id="tab-all"   onclick="setPeriod('all')">All Time</button>
    <button class="period-tab"        id="tab-month" onclick="setPeriod('month')">This Month</button>
    <button class="period-tab"        id="tab-week"  onclick="setPeriod('week')">Last 7 Days</button>
  </div>
  <div class="topbar-metrics">
    <div>
      <span class="topbar-title">Cost</span>
      <span class="topbar-val red" id="at-cost">—</span>
    </div>
    <div>
      <span class="topbar-title">Total Tokens</span>
      <span class="topbar-val" id="at-tokens">—</span>
    </div>
    <div class="topbar-breakdown" id="at-breakdown">
      <span class="dim">—</span>
    </div>
    <div>
      <span class="topbar-title">Sessions</span>
      <span class="topbar-val blue" id="at-sessions">—</span>
    </div>
    <div>
      <span class="topbar-title">Prompts</span>
      <span class="topbar-val blue" id="at-prompts">—</span>
    </div>
    <button class="btn btn-validate" onclick="showValidation()">Full Stats</button>
  </div>
</div>

<!-- Model analytics + chart -->
<div class="grid2">
  <div class="card">
    <div class="card-title">Usage by Model</div>
    <table>
      <thead><tr><th>Model</th><th>Prompts</th><th>Tokens</th><th>Cost</th><th>Tools</th></tr></thead>
      <tbody id="by-model"></tbody>
    </table>
  </div>
  <div class="card">
    <div class="card-title" id="chart-title">Last 30 Days</div>
    <canvas id="chart-tokens"></canvas>
  </div>
</div>

<!-- Session list -->
<div class="card">
  <div class="card-title" id="session-title">Sessions</div>
  <div id="session-list"></div>
  <div class="pagination">
    <button class="btn" id="btn-prev" onclick="changePage(-1)">← Prev</button>
    <span class="page-info" id="page-info">—</span>
    <button class="btn" id="btn-next" onclick="changePage(1)">Next →</button>
  </div>
</div>

<!-- Validation modal -->
<div class="modal-overlay" id="validate-modal" onclick="hideValidation()" style="display:none">
  <div class="modal" onclick="event.stopPropagation()">
    <button class="modal-close" onclick="hideValidation()">✕</button>
    <div id="validate-content"><div class="dim">Loading…</div></div>
  </div>
</div>

<script>
let currentPage = 1, totalPages = 1, chart = null, currentPeriod = 'all';
let pricingData = {};
const expandedIds = new Set();

async function initPricing() {
  try {
    const r = await fetch('/pricing');
    const d = await r.json();
    pricingData = d.models || {};
  } catch(e) {}
}

async function validatePrompt(promptId, resultElId) {
  const el = document.getElementById(resultElId);
  if (!el) return;
  el.innerHTML = '<span class="val-na">checking…</span>';
  try {
    const r = await fetch('/validate/prompt?id=' + promptId);
    const d = await r.json();
    if (d.error) { el.innerHTML = `<span class="val-err">${escHtml(d.error)}</span>`; return; }
    if (!d.transcript_found) {
      el.innerHTML = '<span class="val-na">— no transcript</span>';
      return;
    }
    const db = d.db || {}, tx = d.transcript_turn || {};
    const txTs = tx.ts ? tx.ts.substring(11,16) : '';
    if (d.match) {
      el.innerHTML = `<span class="val-ok">✓ ${fmtTok(db.total)} tok · in=${fmtTok(db.input)} out=${fmtTok(db.output)} cr=${fmtTok(db.cache_read)} cw=${fmtTok(db.cache_write)} · matched turn@${txTs}</span>`;
    } else {
      const sign = d.diff > 0 ? '+' : '';
      el.innerHTML = `<span class="val-err">✗ DB=${fmtTok(db.total)} · Transcript turn=${fmtTok(tx.total)} · diff=${sign}${fmtTok(d.diff)}</span>
        <span class="val-na" style="display:block;margin-top:2px">DB: in=${fmtTok(db.input)} out=${fmtTok(db.output)} cr=${fmtTok(db.cache_read)} cw=${fmtTok(db.cache_write)}</span>
        <span class="val-na" style="display:block">TX: in=${fmtTok(tx.input)} out=${fmtTok(tx.output)} cr=${fmtTok(tx.cache_read)} cw=${fmtTok(tx.cache_write)} @${txTs}</span>`;
    }
  } catch(e) {
    el.innerHTML = '<span class="val-err">request failed</span>';
  }
}

async function validateSession(sessionId, resultElId) {
  const el = document.getElementById(resultElId);
  if (!el) return;
  el.innerHTML = '<span class="val-na">checking…</span>';
  try {
    const r = await fetch('/validate/session?id=' + encodeURIComponent(sessionId));
    const d = await r.json();
    if (d.error) { el.innerHTML = `<span class="val-err">${escHtml(d.error)}</span>`; return; }
    if (!d.transcript_found) {
      el.innerHTML = '<span class="val-na">— no transcript (API/pre-install session)</span>';
      return;
    }
    const db = d.db || {}, tx = d.transcript || {};
    if (d.match) {
      el.innerHTML = `<span class="val-ok">✓ ${fmtTok(db.total)} tokens match transcript</span>`;
    } else {
      const sign = d.diff > 0 ? '+' : '';
      el.innerHTML = `<span class="val-err">✗ DB=${fmtTok(db.total)} · Transcript=${fmtTok(tx.total)} · diff=${sign}${fmtTok(d.diff)}</span>`;
    }
  } catch(e) {
    el.innerHTML = '<span class="val-err">request failed</span>';
  }
}

function costCalc(p) {
  const rates = pricingData[p.model];
  if (!rates) {
    return `<div class="cost-calc"><span class="cost-calc-title">Cost calculation</span>
      <span class="dim" style="font-size:11px">No pricing data for model "${escHtml(p.model || 'unknown')}"</span></div>`;
  }
  const [rIn, rOut, rCr, rCw] = rates;
  const cIn  = p.in          * rIn  / 1e6;
  const cOut = p.out         * rOut / 1e6;
  const cCr  = p.cache_read  * rCr  / 1e6;
  const cCw  = p.cache_write * rCw  / 1e6;
  const mainCost = cIn + cOut + cCr + cCw;
  const agentTotal = (p.agent_list || []).reduce((sum, a) => sum + (a.cost || 0), 0);
  const calc = mainCost + agentTotal;
  const diff = Math.abs(calc - p.cost);

  const row = (lbl, tok, rate, cost, cls) => tok > 0 ? `<tr>
    <td class="dim">${lbl}</td>
    <td class="${cls}">${fmtTok(tok)}</td>
    <td class="dim">× $${rate.toFixed(2)}/M</td>
    <td class="red">= $${cost.toFixed(6)}</td>
  </tr>` : '';

  const agentRow = agentTotal > 0 ? `<tr>
    <td class="dim">agents (${(p.agent_list||[]).length})</td>
    <td class="orange">${(p.agent_list||[]).length} run${(p.agent_list||[]).length>1?'s':''}</td>
    <td class="dim"></td>
    <td class="red">= $${agentTotal.toFixed(6)}</td>
  </tr>` : '';

  return `<div class="cost-calc">
    <span class="cost-calc-title">Cost calculation — ${escHtml(p.model || '')}</span>
    <table>
      ${row('input',       p.in,          rIn,  cIn,  'blue')}
      ${row('output',      p.out,         rOut, cOut, '')}
      ${row('cache read',  p.cache_read,  rCr,  cCr,  'green')}
      ${row('cache write', p.cache_write, rCw,  cCw,  'yellow')}
      ${agentRow}
      <tr class="total-row">
        <td colspan="3" class="dim">Calculated total</td>
        <td class="red"><strong>$${calc.toFixed(6)}</strong></td>
      </tr>
    </table>
    <div class="cost-diff">Stored: $${p.cost.toFixed(6)} &nbsp;·&nbsp; Diff: $${diff.toFixed(6)} ${diff < 0.000001 ? '✓' : '⚠'}
      &nbsp;<button class="val-btn" onclick="validatePrompt(${p.id},'pvr-${p.id}')">Validate tokens</button>
    </div>
    <div id="pvr-${p.id}" class="val-result" style="padding:4px 0 0 0"></div>
  </div>`;
}

function fmt(n)    { return n ? Number(n).toLocaleString() : '—'; }
function fmtTok(n) {
  if (!n) return '—';
  if (n >= 1e6) return (n/1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(1) + 'k';
  return n.toString();
}
function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Period tabs ───────────────────────────────────────────────────────────────
function setPeriod(p) {
  currentPeriod = p;
  currentPage   = 1;
  ['all','month','week'].forEach(id => {
    const el = document.getElementById('tab-' + id);
    if (el) el.classList.toggle('active', id === p);
  });
  fetchData();
}

// ── Chart ─────────────────────────────────────────────────────────────────────
function initChart(title, labels, tokens, costs) {
  document.getElementById('chart-title').textContent = title;
  const ctx = document.getElementById('chart-tokens').getContext('2d');
  if (chart) chart.destroy();
  chart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Tokens', data: tokens, backgroundColor: '#1f6feb88', borderColor: '#388bfd', borderWidth: 1, yAxisID: 'y' },
        { label: 'Cost ($)', data: costs, backgroundColor: '#f7816688', borderColor: '#f78166', borderWidth: 1, type: 'line', yAxisID: 'y2', tension: 0.3 }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: true,
      plugins: { legend: { labels: { color: '#7d8590', font: { size: 10 } } } },
      scales: {
        x:  { ticks: { color: '#7d8590', font: { size: 9 }, maxRotation: 45 }, grid: { color: '#21262d' } },
        y:  { ticks: { color: '#3fb950', font: { size: 9 } }, grid: { color: '#21262d' }, position: 'left' },
        y2: { ticks: { color: '#f78166', font: { size: 9 } }, grid: { display: false }, position: 'right' }
      }
    }
  });
}

// ── Expand/collapse ───────────────────────────────────────────────────────────
function toggleSection(el, contentId) {
  const content = document.getElementById(contentId);
  if (!content) return;
  const isOpen = content.style.display === 'block' || content.classList.contains('open');
  const opening = !isOpen;
  content.style.display = opening ? 'block' : 'none';
  content.classList.toggle('open', opening);
  if (el) {
    const chev = el.querySelector(':scope > .chevron, .chevron');
    if (chev) chev.classList.toggle('open', opening);
  }
  if (opening) expandedIds.add(contentId);
  else expandedIds.delete(contentId);
}

function toggleAgent(contentId) {
  const content = document.getElementById(contentId);
  if (!content) return;
  const opening = content.style.display !== 'block';
  content.style.display = opening ? 'block' : 'none';
  if (opening) expandedIds.add(contentId);
  else expandedIds.delete(contentId);
}

function restoreExpanded() {
  expandedIds.forEach(id => {
    const content = document.getElementById(id);
    if (!content) return;
    content.style.display = 'block';
    content.classList.add('open');
    const header = content.previousElementSibling;
    if (header) {
      const chev = header.querySelector('.chevron');
      if (chev) chev.classList.add('open');
    }
  });
}

// ── Render helpers ────────────────────────────────────────────────────────────
function renderAgentTree(agents) {
  if (!agents || agents.length === 0) return '';
  return agents.map((a, i) => {
    const line = (i === agents.length - 1) ? '└─' : '├─';
    return `<div class="agent-row">
      <span class="tree-line">${line}</span>
      <span class="agent-id">${a.transcript_id ? a.transcript_id.substring(0,8) : 'agent'}</span>
      <span class="agent-stat">in=${fmtTok(a.in)}</span>
      <span class="agent-stat">out=${fmtTok(a.out)}</span>
      <span class="agent-stat">${fmtTok(a.tokens)} tok</span>
      <span class="agent-stat">🔧${a.tools}</span>
      <span class="agent-cost">$${a.cost.toFixed(4)}</span>
    </div>`;
  }).join('');
}

function renderPrompt(p) {
  const compact   = p.compact_overhead ? '<span class="badge badge-compact">⚠ compact</span> ' : '';
  const hasAgents = p.agent_list && p.agent_list.length > 0;
  const agentId   = `agents-${p.id}`;
  const agentToggle = hasAgents
    ? `<span class="badge" onclick="event.stopPropagation();toggleAgent('${agentId}')" style="cursor:pointer">▶ ${p.agent_list.length} agent${p.agent_list.length > 1 ? 's' : ''}</span>`
    : '';

  // Timestamp HH:MM
  const ts = p.timestamp ? p.timestamp.substring(11, 16) : '';

  // Mini breakdown inline: in / out / cr
  const mini = `<span class="prompt-mini">
    <span class="ml">in</span><span class="blue">${fmtTok(p.in)}</span>
    <span class="ml">out</span><span>${fmtTok(p.out)}</span>
    ${p.cache_read ? `<span class="ml">cr</span><span class="green">${fmtTok(p.cache_read)}</span>` : ''}
  </span>`;

  // Full breakdown (expandable)
  const breakdown = `<div class="breakdown-bar">
    <div class="bk-item"><span class="bk-val blue">${fmtTok(p.in)}</span><span class="bk-lbl">input</span></div>
    <div class="bk-item"><span class="bk-val">${fmtTok(p.out)}</span><span class="bk-lbl">output</span></div>
    <div class="bk-item"><span class="bk-val green">${fmtTok(p.cache_read)}</span><span class="bk-lbl">cache hit</span></div>
    <div class="bk-item"><span class="bk-val yellow">${fmtTok(p.cache_write)}</span><span class="bk-lbl">cache write</span></div>
    <div class="bk-item"><span class="bk-val yellow">${p.tools}</span><span class="bk-lbl">tools</span></div>
    ${hasAgents ? `<div class="bk-item"><span class="bk-val orange">${p.agents}</span><span class="bk-lbl">agents</span></div>` : ''}
  </div>`;

  const agentTree = hasAgents
    ? `<div class="agent-tree" id="${agentId}">${renderAgentTree(p.agent_list)}</div>`
    : '';

  const detailId = `prompt-detail-${p.id}`;
  return `<div class="prompt-row">
    <div class="prompt-header" onclick="toggleSection(this,'${detailId}')">
      <span class="prompt-num dim">#${(p.index || 0) + 1}</span>
      ${ts ? `<span class="prompt-ts">${ts}</span>` : ''}
      <span class="prompt-text">${compact}${escHtml(p.text || '(empty)')}</span>
      <div class="prompt-stats">
        ${mini}
        ${agentToggle}
        <span class="prompt-cost">$${p.cost.toFixed(4)}</span>
        <span class="chevron">›</span>
      </div>
    </div>
    <div id="${detailId}" style="display:none">
      ${breakdown}
      ${costCalc(p)}
      ${agentTree}
    </div>
  </div>`;
}

function renderSession(s) {
  const sid       = s.id.replace(/[^a-z0-9]/gi, '-');
  const promptsId = `prompts-${sid}`;
  const ts        = s.started_at ? s.started_at.substring(0, 16).replace('T', ' ') : '';
  const promptsHtml = (s.prompts || []).map(p => renderPrompt(p)).join('');

  return `<div class="session">
    <div class="session-header" onclick="toggleSection(this,'${promptsId}')">
      <span class="chevron">›</span>
      <span class="badge">${escHtml(s.model || 'unknown')}</span>
      <span class="badge badge-account">${escHtml(s.account)}</span>
      ${s.project ? `<span class="dim" style="font-size:11px">${escHtml(s.project)}</span>` : ''}
      <span class="session-meta">${ts}</span>
      <span class="session-meta">${s.prompt_count} prompts</span>
      <span class="session-cost">$${s.total_cost.toFixed(4)}</span>
    </div>
    <div class="session-stats">
      <span class="stat-item"><span class="stat-lbl">in</span> <span class="blue">${fmtTok(s.total_in)}</span></span>
      <span class="stat-sep">·</span>
      <span class="stat-item"><span class="stat-lbl">out</span> <span>${fmtTok(s.total_out)}</span></span>
      <span class="stat-sep">·</span>
      <span class="stat-item"><span class="stat-lbl">cache↓</span> <span class="green">${fmtTok(s.total_cr)}</span></span>
      <span class="stat-sep">·</span>
      <span class="stat-item"><span class="stat-lbl">cache↑</span> <span class="yellow">${fmtTok(s.total_cw)}</span></span>
      <span class="stat-sep">·</span>
      <span class="stat-item"><span class="stat-lbl">total</span> <span>${fmtTok(s.total_tokens)}</span></span>
      <button class="val-btn" onclick="event.stopPropagation();validateSession('${s.id}','val-${sid}')">Validate</button>
      <span id="val-${sid}" class="val-result"></span>
    </div>
    <div class="prompts" id="${promptsId}" style="display:none">${promptsHtml}</div>
  </div>`;
}

// ── Validation modal ──────────────────────────────────────────────────────────
function showValidation() {
  const overlay = document.getElementById('validate-modal');
  overlay.style.display = 'flex';
  const content = document.getElementById('validate-content');
  content.innerHTML = '<div class="dim">Loading…</div>';
  fetch('/validate').then(r => r.json()).then(d => renderValidation(d, content))
    .catch(e => { content.innerHTML = `<div class="red">Error: ${escHtml(e.message)}</div>`; });
}

function hideValidation() {
  document.getElementById('validate-modal').style.display = 'none';
}

function renderValidation(d, el) {
  if (d.error) { el.innerHTML = `<div class="red">${escHtml(d.error)}</div>`; return; }

  // ── Section 1: stats-cache.json — all-time token usage ──
  const sc   = d.sc_summary || {};
  const scErr = d.sc_error ? `<div class="red" style="font-size:11px;margin:6px 0">${escHtml(d.sc_error)}</div>` : '';

  let scRows = '';
  let scTotalInp = 0, scTotalOut = 0, scTotalCr = 0, scTotalCw = 0;
  (d.sc_models || []).forEach(m => {
    scTotalInp += m.inp; scTotalOut += m.out; scTotalCr += m.cr; scTotalCw += m.cw;
    scRows += `<tr>
      <td><span class="badge">${escHtml(m.model)}</span></td>
      <td class="blue">${fmtTok(m.inp)}</td>
      <td>${fmtTok(m.out)}</td>
      <td class="green">${fmtTok(m.cr)}</td>
      <td class="yellow">${fmtTok(m.cw)}</td>
      <td><strong>${fmtTok(m.tokens)}</strong></td>
    </tr>`;
  });
  const scTotal = scTotalInp + scTotalOut + scTotalCr + scTotalCw;
  if (scRows) {
    scRows += `<tr style="border-top:1px solid #30363d">
      <td class="dim">Total</td>
      <td class="blue"><strong>${fmtTok(scTotalInp)}</strong></td>
      <td><strong>${fmtTok(scTotalOut)}</strong></td>
      <td class="green"><strong>${fmtTok(scTotalCr)}</strong></td>
      <td class="yellow"><strong>${fmtTok(scTotalCw)}</strong></td>
      <td><strong>${fmtTok(scTotal)}</strong></td>
    </tr>`;
  }

  const scMeta = [
    sc.first_date     ? `Since ${escHtml(sc.first_date)}`        : '',
    sc.last_computed  ? `Updated ${escHtml(sc.last_computed)}`   : '',
    sc.total_sessions ? `${fmt(sc.total_sessions)} sessions`     : '',
    sc.total_messages ? `${fmt(sc.total_messages)} messages`     : '',
  ].filter(Boolean).join('  ·  ');

  // ── Section 2: CTU cost data ──
  let ctuRows = '';
  let ctuTotalCost = 0;
  (d.ctu_models || []).forEach(m => {
    ctuTotalCost += m.cost;
    ctuRows += `<tr>
      <td><span class="badge">${escHtml(m.model)}</span></td>
      <td>${fmtTok(m.tokens)}</td>
      <td class="red">$${m.cost.toFixed(4)}</td>
      <td class="dim">${fmt(m.prompts)}</td>
    </tr>`;
  });
  if (ctuRows) {
    ctuRows += `<tr style="border-top:1px solid #30363d">
      <td class="dim">Total</td><td></td>
      <td class="red"><strong>$${ctuTotalCost.toFixed(4)}</strong></td><td></td>
    </tr>`;
  }
  const ctuSince = d.ctu_since ? `Tracking since ${escHtml(d.ctu_since)}` : '';

  // ── Section 3: Pricing rates ──
  let pricingHtml = '';
  const pr = d.pricing || {};
  if (pr.models && Object.keys(pr.models).length > 0) {
    let pRows = Object.entries(pr.models).map(([m, p]) => `<tr>
      <td><span class="badge">${escHtml(m)}</span></td>
      <td class="blue">$${(p[0]||0).toFixed(2)}</td>
      <td>$${(p[1]||0).toFixed(2)}</td>
      <td class="green">$${(p[2]||0).toFixed(3)}</td>
      <td class="yellow">$${(p[3]||0).toFixed(2)}</td>
    </tr>`).join('');
    pricingHtml = `
      <h4>Pricing used for cost calculation &nbsp;<span class="dim" style="font-weight:normal;text-transform:none">fetched ${escHtml(pr.fetched_at || '?')} from OpenRouter</span></h4>
      <table><thead><tr><th>Model</th><th>$/M input</th><th>$/M output</th><th>$/M cache read</th><th>$/M cache write</th></tr></thead>
      <tbody>${pRows}</tbody></table>`;
  }

  el.innerHTML = `
    <h3>Usage Stats</h3>

    <h4>All-time token usage &nbsp;<span class="dim" style="font-weight:normal;text-transform:none">${scMeta}</span></h4>
    <p class="dim" style="font-size:10px;margin-bottom:6px">Source: ~/.claude/stats-cache.json — Claude Code's own tracking, includes every session</p>
    ${scErr}
    ${scRows ? `<table><thead><tr><th>Model</th><th>Input</th><th>Output</th><th>Cache read</th><th>Cache write</th><th>Total</th></tr></thead><tbody>${scRows}</tbody></table>` : ''}

    <h4 style="margin-top:16px">Cost by model &nbsp;<span class="dim" style="font-weight:normal;text-transform:none">${ctuSince}</span></h4>
    <p class="dim" style="font-size:10px;margin-bottom:6px">Source: CTU database — sessions tracked since hook installation. Cost calculated using pricing below.</p>
    ${ctuRows ? `<table><thead><tr><th>Model</th><th>Tokens</th><th>Cost</th><th>Prompts</th></tr></thead><tbody>${ctuRows}</tbody></table>` : '<div class="dim" style="font-size:11px">No CTU data yet</div>'}

    ${pricingHtml}
    <div class="validate-note" style="margin-top:14px">
      Cost = (input × $/M) + (output × $/M) + (cache_read × $/M) + (cache_write × $/M)<br>
      For full token-level validation: <code>python3 ~/.ctu/bin/validate.py --stats-cache</code>
    </div>`;
}

// ── Main update ───────────────────────────────────────────────────────────────
function update(d) {
  if (d.error) {
    document.getElementById('session-list').innerHTML = `<div style="color:#f78166;padding:20px">${escHtml(d.error)}</div>`;
    return;
  }

  const s = d.summary || {};
  document.getElementById('at-cost').textContent     = '$' + (s.cost || 0).toFixed(4);
  document.getElementById('at-tokens').textContent   = fmtTok(s.tokens);
  document.getElementById('at-sessions').textContent = fmt(s.sessions);
  document.getElementById('at-prompts').textContent  = fmt(s.prompts);

  // Topbar token breakdown
  document.getElementById('at-breakdown').innerHTML = `
    <span class="stat-lbl">in</span> <span class="blue">${fmtTok(s.inp)}</span>
    <span class="sep">·</span>
    <span class="stat-lbl">out</span> <span>${fmtTok(s.out)}</span>
    <span class="sep">·</span>
    <span class="stat-lbl">cache↓</span> <span class="green">${fmtTok(s.cr)}</span>
    <span class="sep">·</span>
    <span class="stat-lbl">cache↑</span> <span class="yellow">${fmtTok(s.cw)}</span>`;

  // By model
  const bmBody = document.getElementById('by-model');
  bmBody.innerHTML = '';
  (d.by_model || []).forEach(m => {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td><span class="badge">${escHtml(m.model)}</span></td>
      <td class="blue">${fmt(m.prompts)}</td>
      <td class="green">${fmtTok(m.tokens)}</td>
      <td class="red">$${(m.cost||0).toFixed(4)}</td>
      <td class="yellow">${fmt(m.tools)}</td>`;
    bmBody.appendChild(tr);
  });

  if (d.chart) initChart(d.chart.title, d.chart.labels, d.chart.tokens, d.chart.costs);

  totalPages = d.total_pages;
  document.getElementById('session-list').innerHTML = (d.sessions || []).map(renderSession).join('');
  restoreExpanded();

  const label = currentPeriod === 'all' ? 'All Time'
              : currentPeriod === 'month' ? 'This Month'
              : 'Last 7 Days';
  document.getElementById('session-title').textContent =
    `Sessions — ${label} (${d.total_sessions} total)`;
  document.getElementById('page-info').textContent =
    `Page ${d.page} of ${d.total_pages}`;
  document.getElementById('btn-prev').disabled = d.page <= 1;
  document.getElementById('btn-next').disabled = d.page >= d.total_pages;
}

function changePage(dir) {
  currentPage = Math.max(1, Math.min(totalPages, currentPage + dir));
  fetchData();
}

async function fetchData() {
  try {
    const r = await fetch(`/data?page=${currentPage}&period=${currentPeriod}`);
    const d = await r.json();
    update(d);
  } catch(e) {}
}

setInterval(fetchData, 5000);
initPricing();
fetchData();
</script>
</body>
</html>"""


# ── Server ────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        if self.path == '/' or self.path.startswith('/?'):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path.startswith('/data'):
            page   = 1
            period = 'all'
            m = re.search(r'page=(\d+)', self.path)
            if m: page = int(m.group(1))
            pm = re.search(r'period=(\w+)', self.path)
            if pm: period = pm.group(1)
            data = json.dumps(get_data(page, period))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data.encode())
        elif self.path.startswith('/pricing'):
            pricing_file = CTU_DIR / "pricing.json"
            try:
                raw = json.loads(pricing_file.read_text()) if pricing_file.exists() else {}
                payload = {"models": raw.get("models", {}), "fetched_at": raw.get("fetched_at", "")}
            except Exception:
                payload = {"models": {}}
            data = json.dumps(payload)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data.encode())
            return
        elif self.path.startswith('/validate/prompt'):
            m = re.search(r'[?&]id=([^&]+)', self.path)
            if m:
                data = json.dumps(get_prompt_validation(int(m.group(1))))
            else:
                data = json.dumps({"error": "missing id param"})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data.encode())
            return
        elif self.path.startswith('/validate/session'):
            m = re.search(r'[?&]id=([^&]+)', self.path)
            if m:
                import urllib.parse
                sid  = urllib.parse.unquote(m.group(1))
                data = json.dumps(get_session_validation(sid))
            else:
                data = json.dumps({"error": "missing id param"})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data.encode())
            return
        elif self.path.startswith('/validate'):
            data = json.dumps(get_validation_data())
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data.encode())
        else:
            self.send_response(404)
            self.end_headers()


def main():
    # Background pricing refresh if stale
    fetch_script = CTU_DIR / "bin" / "fetch-pricing.py"
    if fetch_script.exists():
        pricing_file = CTU_DIR / "pricing.json"
        needs_refresh = not pricing_file.exists()
        if not needs_refresh:
            try:
                data = json.loads(pricing_file.read_text())
                fetched = data.get("fetched_at")
                if fetched:
                    age = (datetime.now() - datetime.fromisoformat(
                        fetched.replace('Z', '+00:00').replace('+00:00', ''))).days
                    needs_refresh = age >= 7
            except Exception:
                needs_refresh = True
        if needs_refresh:
            subprocess.Popen(
                ["python3", str(fetch_script)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True
            )

    # Kill any existing process on the port before binding
    try:
        import signal
        result = subprocess.run(['lsof', '-ti', f':{PORT}'], capture_output=True, text=True)
        for pid in result.stdout.strip().split('\n'):
            if pid:
                os.kill(int(pid), signal.SIGKILL)
    except Exception:
        pass

    server = HTTPServer(('127.0.0.1', PORT), Handler)
    print(f'Claude Token UI → http://localhost:{PORT}')
    threading.Timer(0.5, lambda: webbrowser.open(f'http://localhost:{PORT}')).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')


if __name__ == '__main__':
    main()
