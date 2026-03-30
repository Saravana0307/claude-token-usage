# claude-token-usage

Per-prompt token usage tracker for [Claude Code](https://claude.ai/code) with SQLite storage, automatic pricing, subagent cost attribution, and a localhost web UI.

## What it does

After every Claude response, captures and stores:
- **Per-prompt**: input / output / cache hits / cache writes, cost, tool count, agent count
- **Per-agent**: full token breakdown for each spawned subagent, linked to its parent prompt
- **Cost**: auto-fetched from live pricing (weekly refresh via OpenRouter), no manual updates needed
- **Compact overhead**: detects and flags token spikes after `/compact` separately
- **Plan mode**: captures costs from plan mode sessions via `exit_plan_mode` hook
- **Account tagging**: tags sessions by logged-in user identity

All data stored in `~/.ctu/usage.db` (SQLite — persistent, queryable, survives log rotation).

### Web UI (`token-usage`)

Runs at `http://localhost:7123`:

```
┌─ ALL TIME: $142.30 | 48.2M tokens | 12 sessions | 310 prompts ─────┐
│                                                                      │
│  Usage by Model      │  Last 30 Days (chart)                        │
│                                                                      │
│  Sessions (12 total)                                                 │
│  ▶ Session Mar 28 saravanakumar  claude-sonnet-4-6  $4.21           │
│    ▶ #1  "refactor auth module"              24k tok  $0.45         │
│         ├─ Explore agent    12k tok  $0.18                          │
│         ├─ Plan agent        8k tok  $0.08                          │
│         └─ code-simplifier   6k tok  $0.07                          │
│    ▶ #2  "add tests"  ⚠ compact             36k tok  $0.12         │
└──────────────────────────────────────────────────────────────────────┘
```

Features:
- All-time cost/token/session/prompt totals at top
- Collapsible session list with per-session cost
- Per-prompt expandable rows: token breakdown + expandable agent tree
- Compact overhead flagged with ⚠
- Model analytics table + 30-day cost/token chart
- Auto-refreshes every 3 seconds

## Requirements

- [Claude Code](https://claude.ai/code) v1.0+
- Python 3.7+ (stdlib only — no pip installs needed)
- macOS or Linux

## Install

```bash
git clone https://github.com/Saravana0307/claude-token-usage.git
cd claude-token-usage
bash install.sh
```

The installer:
1. Copies all scripts to `~/.ctu/`
2. Initializes `~/.ctu/usage.db` (SQLite) and migrates any existing log history
3. Fetches latest Claude pricing from OpenRouter (falls back to bundled defaults)
4. Detects your account identity
5. Auto-patches `~/.claude/settings.json` to register hooks (safe to re-run)
6. Removes old `~/.claude/hooks/` copies if upgrading from a previous version

Then add to your PATH (one-time):
```bash
echo 'export PATH="$HOME/.ctu/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

Restart Claude Code — stats will start appearing before each prompt.

## Uninstall

```bash
bash uninstall.sh
```

Removes hooks from `settings.json` and optionally deletes `~/.ctu/` (including usage history).

## Usage

### Launch the web UI
```bash
token-usage
# Opens http://localhost:7123 automatically
```

### Validate your data
```bash
python3 ~/.ctu/bin/validate.py                    # Full report
python3 ~/.ctu/bin/validate.py --session <uuid>   # Validate one session
python3 ~/.ctu/bin/validate.py --pricing          # Show current pricing cache
python3 ~/.ctu/bin/validate.py --subagents        # Check agent linkage
```

### Force pricing refresh
```bash
python3 ~/.ctu/bin/fetch-pricing.py --force
```

## Configuration

### Account identity (optional)
Set in `~/.zshrc` to override auto-detected username:
```bash
export CLAUDE_ACCOUNT_EMAIL=you@company.com
```

### Web UI port (default: 7123)
```bash
export TOKEN_UI_PORT=8080
token-usage
```

### Pricing fallback
If the auto-fetch fails (network issue), edit `~/.ctu/pricing_defaults.json` to set your rates manually. Format: `[input, output, cache_read, cache_write]` per 1M tokens.

## How it works

| Hook | Event | Purpose |
|------|-------|---------|
| `token-usage.sh` | `Stop` + `PostToolUse(exit_plan_mode)` | Fires after every response (including plan mode exit). Reads transcript, extracts tokens, filters internal prompts, detects compact overhead, writes to SQLite. |
| `subagent-token-accumulator.sh` | `SubagentStop` | Accumulates per-subagent token stats (with parent session link) to a temp JSON for the main hook to pick up. |
| `token-display.sh` | `UserPromptSubmit` | Shows previous turn's TOTAL line as a compact statusline summary. Clears subagent accumulator to prevent stale leakage. |
| `session-start.sh` | `SessionStart` | Detects account identity and caches to `~/.ctu/current-account.txt`. |

### Token categories

- **ctx** = `input + cache_read + cache_write` — matches Claude Code's `/context` command
- **total** = `ctx + output` — all tokens processed by the API
- **compact overhead** — flagged when a `summary` entry precedes the response in the transcript (post-`/compact` spike)

### Cost formula

```
cost = (input_tokens        / 1M × price_input)
     + (output_tokens       / 1M × price_output)
     + (cache_read_tokens   / 1M × price_cache_read)
     + (cache_write_tokens  / 1M × price_cache_write)
```

Pricing auto-fetched from [OpenRouter](https://openrouter.ai/api/v1/models) weekly, cached in `~/.ctu/pricing.json`.

## Files installed

```
~/.ctu/
├── hooks/
│   ├── token-usage.sh                 # Stop + exit_plan_mode hook
│   ├── subagent-token-accumulator.sh  # SubagentStop hook
│   ├── token-display.sh               # UserPromptSubmit hook
│   └── session-start.sh               # SessionStart hook
├── bin/
│   ├── token-usage                    # CLI launcher (web UI)
│   ├── fetch-pricing.py               # Pricing auto-updater
│   ├── migrate.py                     # DB init + log migration
│   └── validate.py                    # Validation & benchmarking
├── token-server.py                    # Web UI server
├── pricing.json                       # Live pricing cache (auto-managed)
├── pricing_defaults.json              # Bundled fallback pricing
├── usage.db                           # SQLite database (all history)
├── token-usage.log                    # Backup log (append-only)
└── current-account.txt                # Detected account identity
```

## Validation / Benchmarking

To verify your data is accurate against transcripts:

```bash
# Validate a specific session against its transcript
python3 ~/.ctu/bin/validate.py --session <session-uuid>

# Check all-time totals match migration
python3 ~/.ctu/bin/validate.py --migration-check

# Confirm agent costs are linked (no orphaned rows)
python3 ~/.ctu/bin/validate.py --subagents
```

The ground truth is the transcript JSONL files in `~/.claude/projects/`. Each `assistant` entry has a `usage` field with exact token counts matching what Anthropic billed.

## vs Claude Code's built-in `/stats`

| | `/stats` | claude-token-usage |
|---|---|---|
| Scope | Current session only | All sessions, historical |
| Per-prompt | No | Yes |
| Subagent cost | Invisible | Attributed per prompt |
| Cost in $ | No | Yes |
| Compact overhead | No | Flagged separately |
| Plan mode | No | Yes |
| Persistent storage | No | SQLite |

## License

MIT
