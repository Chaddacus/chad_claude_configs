#!/usr/bin/env python3
"""
companion_prompt.py — UserPromptSubmit hook for the Codex Companion.
Checks cooldown for 'prompt_received'. Awards +2 XP (20 XP/session cap from prompts).
Never blocks the session: all exceptions caught and exit(0).
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude/bin"))


def main():
    import companion_core as cc

    # Consume stdin (required by hook contract)
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

    # Per-session prompt XP cap: 20 XP
    prompt_xp = state['session'].get('prompt_xp_earned', 0)
    prompt_xp_cap = 20
    prompt_xp_to_award = min(2, max(0, prompt_xp_cap - prompt_xp))
    if prompt_xp_to_award > 0:
        cc.apply_xp(state, prompt_xp_to_award, source='prompt')
        state['session']['prompt_xp_earned'] = prompt_xp + prompt_xp_to_award

    output_text = None

    if cc.should_react(state, 'prompt_received'):
        user_id = cc.get_user_id()
        bones = cc.roll_bones(user_id)
        archetype = cc.classify_personality(state['soul'].get('personality', ''))
        stats = {k: min(100, bones['stats'][k] + state['stats_bonus'].get(k, 0)) for k in cc.STAT_NAMES}
        reaction = cc.pick_reaction('prompt_received', archetype, stats)
        output_text = cc.render_minimal(reaction)
        cc.set_cooldown(state, 'prompt_received')

    cc.save_state(state)

    if output_text:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": output_text
            }
        }))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(0)  # Never block the session
