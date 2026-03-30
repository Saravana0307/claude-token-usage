#!/usr/bin/env python3
"""Live token usage watcher for Claude Code sessions."""

import os, time, re
from pathlib import Path

STATS_FILE = Path.home() / ".claude" / "last-token-stats.txt"
LOG_FILE   = Path.home() / ".claude" / "token-usage.log"
HISTORY    = 8  # number of past entries to show

try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.live import Live
    from rich.text import Text
    from rich import box
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

console = Console() if HAS_RICH else None

def parse_log_entries(n=HISTORY):
    """Read last N entries from token-usage.log."""
    if not LOG_FILE.exists():
        return []
    text = LOG_FILE.read_text()
    blocks = [b.strip() for b in text.split("\n\n") if b.strip()]
    entries = []
    for block in blocks[-n:]:
        lines = block.splitlines()
        prompt = lines[0].replace('[tokens] ', '').strip('"') if lines else ''
        total_line = next((l for l in lines if 'TOTAL' in l), '')
        m = re.search(r'ctx=([\d,]+).*?total=([\d,]+).*?\$([\d.]+)', total_line)
        if m:
            entries.append({
                'prompt': prompt[:55],
                'ctx':    m.group(1),
                'total':  m.group(2),
                'cost':   m.group(3),
            })
    return entries

def read_current():
    """Read the latest stats from last-token-stats.txt."""
    if not STATS_FILE.exists():
        return None
    return STATS_FILE.read_text().strip()

def build_display():
    if not HAS_RICH:
        current = read_current()
        return current or "Waiting for Claude response…"

    # Current stats panel
    current = read_current()
    if current:
        panel = Panel(
            Text(current, style="bold cyan"),
            title="[bold green]Last Response[/bold green]",
            border_style="green",
        )
    else:
        panel = Panel("Waiting for Claude response…", border_style="dim")

    # History table
    table = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold magenta",
        title="Recent Prompts",
        title_style="bold",
        expand=True,
    )
    table.add_column("Prompt", style="dim", no_wrap=True, max_width=55)
    table.add_column("ctx", justify="right", style="cyan")
    table.add_column("total", justify="right")
    table.add_column("cost", justify="right", style="yellow")

    for e in parse_log_entries():
        table.add_row(e['prompt'], e['ctx'], e['total'], f"${e['cost']}")

    from rich.columns import Columns
    from rich import print as rprint
    return (panel, table)

def main():
    print("Claude Token Watcher — Ctrl+C to exit\n")

    last_mtime = 0

    if HAS_RICH:
        from rich.layout import Layout
        from rich.columns import Columns

        with Live(refresh_per_second=2, screen=False) as live:
            while True:
                try:
                    mtime = STATS_FILE.stat().st_mtime if STATS_FILE.exists() else 0
                    if mtime != last_mtime:
                        last_mtime = mtime
                        result = build_display()
                        if isinstance(result, tuple):
                            panel, table = result
                            from rich.console import Group
                            live.update(Group(panel, table))
                        else:
                            live.update(result)
                    time.sleep(0.5)
                except KeyboardInterrupt:
                    break
    else:
        # Fallback: plain text polling
        while True:
            try:
                mtime = STATS_FILE.stat().st_mtime if STATS_FILE.exists() else 0
                if mtime != last_mtime:
                    last_mtime = mtime
                    os.system("clear")
                    print("=== Claude Token Stats ===\n")
                    current = read_current()
                    if current:
                        print(current)
                    print("\n--- Recent History ---")
                    for e in parse_log_entries():
                        print(f"  {e['prompt'][:50]:<50}  ctx={e['ctx']:>8}  ${e['cost']}")
                time.sleep(0.5)
            except KeyboardInterrupt:
                break

if __name__ == "__main__":
    main()
