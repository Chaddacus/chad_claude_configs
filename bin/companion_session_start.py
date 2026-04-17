#!/usr/bin/env python3
"""
companion_session_start.py — SessionStart hook for the Codex Companion.
Never blocks the session: all exceptions caught and exit(0).
"""

import sys
import json
import os
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude/bin"))


def main():
    import companion_core as cc

    state = cc.load_state()

    # First run — write stub, prompt hatch
    if state is None:
        state = cc.new_state()
        cc.save_state(state)
        output = "[ companion ] Type /companion hatch to meet your Codex companion!"
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": output
            }
        }))
        return

    if state['flags']['muted']:
        return

    # Session boundary detection
    session_id = os.environ.get('CLAUDE_SESSION_ID', '')
    if state['session']['session_id'] != session_id:
        state['session']['session_id'] = session_id
        state['session']['xp_earned_this_session'] = 0
        state['session']['prompt_xp_earned'] = 0
        state['session']['reaction_cooldown_until'] = 0
        cc.apply_xp(state, 5, source='session_start')

    if state['flags']['hatch_pending']:
        output = "[ companion ] Your companion is waiting to hatch! Type /companion hatch"
        cc.save_state(state)
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": output
            }
        }))
        return

    user_id = cc.get_user_id()
    bones = cc.roll_bones(user_id)
    evo = state['evolution']['stage']
    archetype = cc.classify_personality(state['soul'].get('personality', ''))
    stats = {k: min(100, bones['stats'][k] + state['stats_bonus'].get(k, 0)) for k in cc.STAT_NAMES}
    reaction = cc.pick_reaction('session_start', archetype, stats)
    card = cc.render_card(state, bones, evo, reaction=reaction)

    cc.save_state(state)
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": card
        }
    }))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(0)  # Never block the session
