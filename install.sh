#!/bin/bash
set -e

CTU_DIR="$HOME/.ctu"
CLAUDE_DIR="$HOME/.claude"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing claude-token-usage to $CTU_DIR..."

# ── Create directory structure ──────────────────────────────────────────────
mkdir -p "$CTU_DIR/hooks" "$CTU_DIR/bin"

# ── Copy files ───────────────────────────────────────────────────────────────
cp "$SCRIPT_DIR/hooks/token-usage.sh"                "$CTU_DIR/hooks/"
cp "$SCRIPT_DIR/hooks/subagent-token-accumulator.sh" "$CTU_DIR/hooks/"
cp "$SCRIPT_DIR/hooks/token-display.sh"              "$CTU_DIR/hooks/"
cp "$SCRIPT_DIR/hooks/session-start.sh"              "$CTU_DIR/hooks/"
cp "$SCRIPT_DIR/token-server.py"                     "$CTU_DIR/"
cp "$SCRIPT_DIR/bin/fetch-pricing.py"                "$CTU_DIR/bin/"
cp "$SCRIPT_DIR/bin/migrate.py"                      "$CTU_DIR/bin/"
cp "$SCRIPT_DIR/bin/validate.py"                     "$CTU_DIR/bin/"
cp "$SCRIPT_DIR/pricing_defaults.json"               "$CTU_DIR/"
cp "$SCRIPT_DIR/bin/token-usage"                     "$CTU_DIR/bin/"

chmod +x \
  "$CTU_DIR/hooks/token-usage.sh" \
  "$CTU_DIR/hooks/subagent-token-accumulator.sh" \
  "$CTU_DIR/hooks/token-display.sh" \
  "$CTU_DIR/hooks/session-start.sh" \
  "$CTU_DIR/bin/token-usage" \
  "$CTU_DIR/token-server.py"

echo "✅ Files installed to $CTU_DIR"

# ── Initialize SQLite DB and migrate any existing log data ───────────────────
echo ""
echo "Initializing database..."
python3 "$CTU_DIR/bin/migrate.py"

# ── Detect account identity ──────────────────────────────────────────────────
echo ""
echo "Detecting account..."
bash "$CTU_DIR/hooks/session-start.sh"
ACCOUNT=$(cat "$CTU_DIR/current-account.txt" 2>/dev/null || echo "unknown")
echo "  Account: $ACCOUNT"
echo "  (Set CLAUDE_ACCOUNT_EMAIL in ~/.zshrc to override)"

# ── Initialize pricing cache ─────────────────────────────────────────────────
echo ""
echo "Fetching latest pricing..."
python3 "$CTU_DIR/bin/fetch-pricing.py" --force 2>&1 || echo "  (Using bundled defaults — will retry automatically)"

# ── Patch settings.json ──────────────────────────────────────────────────────
SETTINGS="$CLAUDE_DIR/settings.json"

patch_settings() {
    python3 - "$SETTINGS" "$CTU_DIR" "$CLAUDE_DIR" <<'PYEOF'
import json, sys, os

settings_path = sys.argv[1]
ctu_dir       = sys.argv[2]
claude_dir    = sys.argv[3]

if os.path.exists(settings_path):
    with open(settings_path) as f:
        settings = json.load(f)
else:
    settings = {}

hooks = settings.setdefault("hooks", {})

# New hooks to register (event -> command)
additions = {
    "Stop":             f"{ctu_dir}/hooks/token-usage.sh",
    "SubagentStop":     f"{ctu_dir}/hooks/subagent-token-accumulator.sh",
    "UserPromptSubmit": f"{ctu_dir}/hooks/token-display.sh",
    "SessionStart":     f"{ctu_dir}/hooks/session-start.sh",
}

# Old paths to remove (from previous ~/.claude-based install)
old_paths = {
    "Stop":             f"{claude_dir}/hooks/token-usage.sh",
    "SubagentStop":     f"{claude_dir}/hooks/subagent-token-accumulator.sh",
    "UserPromptSubmit": f"{claude_dir}/hooks/token-display.sh",
}

# Remove old hooks
for event, old_cmd in old_paths.items():
    groups = hooks.get(event, [])
    for group in groups:
        inner = group.get("hooks", [])
        before = len(inner)
        group["hooks"] = [h for h in inner if h.get("command") != old_cmd]
        if len(group["hooks"]) < before:
            print(f"  removed old: {event}")

# Add new hooks (catch-all matcher group)
for event, cmd in additions.items():
    groups = hooks.setdefault(event, [])
    target = next((g for g in groups if g.get("matcher") == ""), None)
    if target is None:
        target = {"matcher": "", "hooks": []}
        groups.append(target)
    inner = target.setdefault("hooks", [])
    if any(h.get("command") == cmd for h in inner):
        print(f"  already configured: {event}")
        continue
    inner.append({"type": "command", "command": cmd})
    print(f"  added: {event}")

# Add PostToolUse hook for exit_plan_mode (plan mode cost capture)
plan_cmd = f"{ctu_dir}/hooks/token-usage.sh"
pt_groups = hooks.setdefault("PostToolUse", [])
plan_group = next((g for g in pt_groups if g.get("matcher") == "exit_plan_mode"), None)
if plan_group is None:
    pt_groups.append({"matcher": "exit_plan_mode", "hooks": [{"type": "command", "command": plan_cmd}]})
    print("  added: PostToolUse(exit_plan_mode)")
elif not any(h.get("command") == plan_cmd for h in plan_group.get("hooks", [])):
    plan_group.setdefault("hooks", []).append({"type": "command", "command": plan_cmd})
    print("  added: PostToolUse(exit_plan_mode)")
else:
    print("  already configured: PostToolUse(exit_plan_mode)")

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"  saved: {settings_path}")
PYEOF
}

echo ""
echo "Patching $SETTINGS..."
if patch_settings; then
    echo "✅ Hooks registered in settings.json"
else
    echo "⚠️  Could not auto-patch settings.json. Add hooks manually (see README)."
fi

# ── Remove old ~/.claude copies if present ───────────────────────────────────
OLD_FILES=(
    "$CLAUDE_DIR/hooks/token-usage.sh"
    "$CLAUDE_DIR/hooks/subagent-token-accumulator.sh"
    "$CLAUDE_DIR/hooks/token-display.sh"
    "$CLAUDE_DIR/last-token-stats.txt"
    "$CLAUDE_DIR/subagent-tokens-accumulator.json"
)
removed=0
for f in "${OLD_FILES[@]}"; do
    if [ -f "$f" ]; then
        rm "$f"
        removed=$((removed + 1))
    fi
done
[ $removed -gt 0 ] && echo "✅ Removed $removed old files from $CLAUDE_DIR"

echo ""
echo "━━━ Next steps ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "1. Add ~/.ctu/bin to your PATH (if not already):"
echo "   echo 'export PATH=\"\$HOME/.ctu/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
echo ""
echo "2. Launch the web UI:"
echo "   token-usage"
echo "   # or: python3 ~/.ctu/token-server.py"
echo ""
echo "3. Validate your setup:"
echo "   python3 ~/.ctu/bin/validate.py"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
