#!/bin/bash
# Uninstall claude-token-usage — removes ~/.ctu/ and cleans settings.json hooks

CTU_DIR="$HOME/.ctu"
CLAUDE_DIR="$HOME/.claude"
SETTINGS="$CLAUDE_DIR/settings.json"

echo "Uninstalling claude-token-usage..."

# ── Remove hooks from settings.json ─────────────────────────────────────────
if [ -f "$SETTINGS" ]; then
    python3 - "$SETTINGS" "$CTU_DIR" <<'PYEOF'
import json, sys

settings_path = sys.argv[1]
ctu_dir       = sys.argv[2]

with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.get("hooks", {})

events = ["Stop", "SubagentStop", "UserPromptSubmit", "SessionStart"]
for event in events:
    for group in hooks.get(event, []):
        before = len(group.get("hooks", []))
        group["hooks"] = [
            h for h in group.get("hooks", [])
            if ctu_dir not in h.get("command", "")
        ]
        if len(group["hooks"]) < before:
            print(f"  removed hook: {event}")

# Remove exit_plan_mode PostToolUse group added by CTU
pt = hooks.get("PostToolUse", [])
before = len(pt)
hooks["PostToolUse"] = [g for g in pt if g.get("matcher") != "exit_plan_mode" or
                         not any(ctu_dir in h.get("command","") for h in g.get("hooks",[]))]
if len(hooks["PostToolUse"]) < before:
    print("  removed hook: PostToolUse(exit_plan_mode)")

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")
print(f"  saved: {settings_path}")
PYEOF
    echo "✅ Hooks removed from settings.json"
fi

# ── Remove ~/.ctu directory ──────────────────────────────────────────────────
if [ -d "$CTU_DIR" ]; then
    read -p "Remove $CTU_DIR (including usage.db and all token history)? [y/N] " confirm
    if [[ "$confirm" =~ ^[Yy]$ ]]; then
        rm -rf "$CTU_DIR"
        echo "✅ Removed $CTU_DIR"
    else
        echo "  Skipped: $CTU_DIR preserved"
    fi
fi

# ── Remove PATH entry reminder ───────────────────────────────────────────────
echo ""
echo "Done. If you added ~/.ctu/bin to PATH, remove it from ~/.zshrc manually."
