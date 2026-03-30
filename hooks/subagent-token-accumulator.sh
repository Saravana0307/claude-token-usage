#!/bin/bash
# Accumulates subagent token usage into a temp file, linking to parent session.
# Fires on SubagentStop — runs before the main Stop hook.

input=$(cat)

HOOK_INPUT="$input" python3 - <<'EOF'
import json, os

data = json.loads(os.environ["HOOK_INPUT"])
transcript_path = data.get("transcript_path", "")
parent_session  = data.get("parent_session_id", "") or data.get("sessionId", "")

if not transcript_path or not os.path.exists(transcript_path):
    import sys; sys.exit(0)

# Sum all token usage across subagent turns
in_tok = out_tok = cache_read = cache_write = tool_count = 0

with open(transcript_path) as f:
    for line in f:
        line = line.strip()
        if not line: continue
        try:
            entry = json.loads(line)
        except: continue
        if entry.get("type") == "assistant":
            msg = entry.get("message", {})
            u = msg.get("usage", {})
            in_tok     += u.get("input_tokens", 0)
            out_tok    += u.get("output_tokens", 0)
            cache_read += u.get("cache_read_input_tokens", 0)
            cache_write+= u.get("cache_creation_input_tokens", 0)
            for block in msg.get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_count += 1

accumulator = os.path.expanduser("~/.ctu/subagent-accumulator.json")

existing = []
if os.path.exists(accumulator):
    try:
        with open(accumulator) as f:
            existing = json.load(f)
    except: pass

existing.append({
    "transcript":     os.path.basename(transcript_path).replace(".jsonl", ""),
    "parent_session": parent_session,
    "input_tokens":   in_tok,
    "output_tokens":  out_tok,
    "cache_read":     cache_read,
    "cache_write":    cache_write,
    "tool_count":     tool_count,
})

with open(accumulator, "w") as f:
    json.dump(existing, f)
EOF
