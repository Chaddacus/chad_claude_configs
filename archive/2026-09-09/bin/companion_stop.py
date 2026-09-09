#!/usr/bin/env python3
"""
companion_stop.py — Stop hook for the Codex Companion.
Awards +5 XP, picks 'stop' reaction, renders minimal bubble.
IMPORTANT: Does NOT output stopReason — that conflicts with what_would_chad_do.py.
Never blocks the session: all exceptions caught and exit(0).
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude/bin"))


def main():
    import companion_core as cc

    # Consume stdin
    try:
        sys.stdin.read()
    except Exception:
        pass

    state = cc.load_state()
    if state is None:
        return
    if state['flags']['muted']:
        return
    if state['flags']['hatch_pending']:
        return

    cc.apply_xp(state, 5, source='stop')

    user_id = cc.get_user_id()
    bones = cc.roll_bones(user_id)
    archetype = cc.classify_personality(state['soul'].get('personality', ''))
    stats = {k: min(100, bones['stats'][k] + state['stats_bonus'].get(k, 0)) for k in cc.STAT_NAMES}
    reaction = cc.pick_reaction('stop', archetype, stats)
    output_text = cc.render_minimal(reaction)

    cc.save_state(state)

    # Output hookSpecificOutput ONLY — never stopReason
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": output_text
        }
    }))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(0)  # Never block the session
