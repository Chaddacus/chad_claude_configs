#!/usr/bin/env python3
"""
pet.py — pet your companion (no cooldown).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude/bin"))


def main():
    import companion_core as cc

    state = cc.load_state()
    if state is None:
        print("[ companion ] No companion found. Type /companion hatch to meet yours!")
        return

    if state['flags']['hatch_pending']:
        print("[ companion ] Your companion is waiting to hatch! Type /companion hatch")
        return

    user_id = cc.get_user_id()
    bones = cc.roll_bones(user_id)
    archetype = cc.classify_personality(state['soul'].get('personality', ''))
    stats = {k: min(100, bones['stats'][k] + state['stats_bonus'].get(k, 0)) for k in cc.STAT_NAMES}

    reaction = cc.pick_reaction('pet', archetype, stats)
    print(cc.render_minimal(reaction))
    print(f"\n  {state['soul'].get('name', '???')} appreciated that.")

    # Save (no XP for petting, just record the moment)
    cc.save_state(state)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"[ companion ] Error: {e}")
        sys.exit(0)
