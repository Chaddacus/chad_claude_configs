#!/usr/bin/env python3
"""
companion_post_tool.py — PostToolUse hook for the Codex Companion.
Reads tool context from stdin JSON.
Never blocks the session: all exceptions caught and exit(0).
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".claude/bin"))


def _parse_tool_input():
    """Read and parse stdin JSON, return dict or empty dict on failure."""
    try:
        raw = sys.stdin.read()
        if raw.strip():
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _xp_for_tool(tool_name: str, tool_error, tool_response: dict) -> tuple[int, str]:
    """
    Returns (xp_amount, event_type).
    Edit/Write success: +8
    Bash success (no error, exit 0): +4
    Read: +1
    Any failure: +3
    """
    if tool_error is not None:
        return 3, 'tool_failure'

    name = (tool_name or '').lower()
    if name in ('edit', 'write', 'notebookedit'):
        return 8, 'tool_success'
    elif name == 'bash':
        # Check exit code if available
        if isinstance(tool_response, dict):
            resp_text = str(tool_response.get('text', '') or tool_response.get('content', ''))
            # Claude Code typically appends exit code; heuristic check
            if 'exit code:' in resp_text.lower():
                import re
                m = re.search(r'exit code[:\s]+(\d+)', resp_text, re.IGNORECASE)
                if m and int(m.group(1)) != 0:
                    return 3, 'tool_failure'
        return 4, 'tool_success'
    elif name == 'read':
        return 1, 'tool_success'
    else:
        return 2, 'tool_success'


def main():
    import companion_core as cc

    data = _parse_tool_input()
    tool_name = data.get('tool_name', '')
    tool_error = data.get('tool_error')
    tool_response = data.get('tool_response', {})

    state = cc.load_state()
    if state is None:
        return
    if state['flags']['muted']:
        return
    if state['flags']['hatch_pending']:
        return

    xp_amount, event = _xp_for_tool(tool_name, tool_error, tool_response)
    result = cc.apply_xp(state, xp_amount, source=event)

    user_id = cc.get_user_id()
    bones = cc.roll_bones(user_id)
    evo = state['evolution']['stage']
    archetype = cc.classify_personality(state['soul'].get('personality', ''))
    stats = {k: min(100, bones['stats'][k] + state['stats_bonus'].get(k, 0)) for k in cc.STAT_NAMES}

    output_text = None

    if result['leveled_up']:
        reaction = cc.pick_reaction('level_up', archetype, stats)
        output_text = cc.render_card(state, bones, evo, reaction=reaction)
    elif result.get('evolved_to') is not None:
        reaction = cc.pick_reaction('evolution', archetype, stats)
        output_text = cc.render_card(state, bones, evo, reaction=reaction)
    elif cc.should_react(state, event):
        reaction = cc.pick_reaction(event, archetype, stats)
        output_text = cc.render_minimal(reaction)
        cc.set_cooldown(state, event)

    cc.save_state(state)

    if output_text:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": output_text
            }
        }))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        sys.exit(0)  # Never block the session
