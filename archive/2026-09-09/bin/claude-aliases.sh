#!/usr/bin/env bash
# Source this file to get mode-specific Claude aliases:
#   source ~/.claude/bin/claude-aliases.sh

alias c-dev='claude --system-prompt "$(cat ~/.claude/contexts/dev.md)"'
alias c-review='claude --system-prompt "$(cat ~/.claude/contexts/review.md)"'
alias c-research='claude --system-prompt "$(cat ~/.claude/contexts/research.md)"'
