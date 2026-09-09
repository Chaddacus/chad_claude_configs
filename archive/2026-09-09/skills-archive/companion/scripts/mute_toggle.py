#!/usr/bin/env python3
"""
mute_toggle.py — mute or unmute the companion.
Usage: python3 mute_toggle.py mute|unmute
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude/bin"))


def main():
    import companion_core as cc

    if len(sys.argv) < 2 or sys.argv[1] not in ('mute', 'unmute'):
        print("Usage: mute_toggle.py mute|unmute")
        sys.exit(1)

    action = sys.argv[1]

    state = cc.load_state()
    if state is None:
        state = cc.new_state()

    state['flags']['muted'] = (action == 'mute')
    cc.save_state(state)

    if action == 'mute':
        print("[ companion ] Companion muted. Use /companion unmute to bring them back.")
    else:
        name = state['soul'].get('name', 'your companion')
        print(f"[ companion ] {name} is back! They missed you.")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"[ companion ] Error: {e}")
        sys.exit(0)
