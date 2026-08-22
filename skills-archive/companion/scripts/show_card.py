#!/usr/bin/env python3
"""
show_card.py — display the companion card.
Usage: python3 show_card.py [--stats]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude/bin"))


def main():
    import companion_core as cc
    import random

    show_stats = '--stats' in sys.argv

    state = cc.load_state()
    if state is None:
        print("[ companion ] No companion found. Type /companion hatch to meet yours!")
        return

    if state['flags']['hatch_pending']:
        print("[ companion ] Your companion is waiting to hatch! Type /companion hatch")
        return

    user_id = cc.get_user_id()
    bones = cc.roll_bones(user_id)
    evo = state['evolution']['stage']
    archetype = cc.classify_personality(state['soul'].get('personality', ''))
    stats = {k: min(100, bones['stats'][k] + state['stats_bonus'].get(k, 0)) for k in cc.STAT_NAMES}

    frame = random.randint(0, 2)
    card = cc.render_card(state, bones, evo, frame=frame)
    print(card)

    if show_stats:
        print()
        session_xp = state['session'].get('xp_earned_this_session', 0)
        prompt_xp = state['session'].get('prompt_xp_earned', 0)
        print(f"  Session XP: {session_xp}/250")
        print(f"  Prompt XP:  {prompt_xp}/20 (this session)")
        print(f"  Archetype:  {archetype}")
        print(f"  Rarity:     {bones['rarity']} {cc.RARITY_DISPLAY[bones['rarity']]}")
        print(f"  Species:    {bones['species']}")
        if bones.get('shiny'):
            print("  Shiny:      YES ✦")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"[ companion ] Error: {e}")
        sys.exit(0)
