#!/bin/bash
# Show previous turn's token stats — compact single line
stats="$HOME/.ctu/last-token-stats.txt"
if [ -f "$stats" ]; then
    grep "TOTAL" "$stats" | sed 's/  TOTAL   : //'
fi

# Clear subagent accumulator at the start of each new prompt.
# Prevents stale tokens from a previous prompt's subagents (which may have
# finished AFTER that prompt's Stop hook already cleared the accumulator)
# from leaking into this prompt's cost report.
rm -f "$HOME/.ctu/subagent-accumulator.json"
