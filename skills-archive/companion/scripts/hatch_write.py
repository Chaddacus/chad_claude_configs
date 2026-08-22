#!/usr/bin/env python3
"""
hatch_write.py — write name/personality to companion state.
Usage:
    hatch_write.py <name> <personality>
    hatch_write.py --rename-only <name>
"""

import sys
import re
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude/bin"))

_BAD_CHARS = re.compile(r'["\\\x00-\x1f\x7f]')


def validate_name(name: str):
    """Returns error string or None if valid."""
    if not name:
        return "Name cannot be empty"
    if len(name) > 20:
        return f"Name too long ({len(name)} chars, max 20)"
    if _BAD_CHARS.search(name):
        return "Name contains invalid characters"
    return None


def validate_personality(personality: str):
    """Returns error string or None if valid."""
    if not personality:
        return "Personality cannot be empty"
    if len(personality) > 100:
        return f"Personality too long ({len(personality)} chars, max 100)"
    if _BAD_CHARS.search(personality):
        return "Personality contains invalid characters"
    return None


def main():
    import companion_core as cc

    args = sys.argv[1:]

    rename_only = False
    if args and args[0] == '--rename-only':
        rename_only = True
        args = args[1:]

    if rename_only:
        if len(args) < 1:
            print("Usage: hatch_write.py --rename-only <name>")
            sys.exit(1)
        name = args[0]
        err = validate_name(name)
        if err:
            print(f"[ companion ] Invalid name: {err}")
            sys.exit(1)

        state = cc.load_state()
        if state is None:
            state = cc.new_state()

        old_name = state['soul'].get('name', '???')
        state['soul']['name'] = name
        cc.save_state(state)
        print(f"[ companion ] Renamed from '{old_name}' to '{name}'.")
        return

    # Full hatch
    if len(args) < 2:
        print("Usage: hatch_write.py <name> <personality>")
        sys.exit(1)

    name = args[0]
    personality = args[1]

    name_err = validate_name(name)
    if name_err:
        print(f"[ companion ] Invalid name: {name_err}")
        sys.exit(1)

    personality_err = validate_personality(personality)
    if personality_err:
        print(f"[ companion ] Invalid personality: {personality_err}")
        sys.exit(1)

    state = cc.load_state()
    if state is None:
        state = cc.new_state()

    state['soul']['name'] = name
    state['soul']['personality'] = personality
    state['flags']['hatch_pending'] = False

    # Roll bones to show species
    user_id = cc.get_user_id()
    bones = cc.roll_bones(user_id)
    evo = state['evolution']['stage']

    cc.save_state(state)

    print(f"[ companion ] Hatched! Meet {name} the {bones['rarity']} {bones['species']}!")
    print()
    card = cc.render_card(state, bones, evo, reaction=f"hi!! I'm {name}!")
    print(card)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"[ companion ] Error: {e}")
        sys.exit(0)
