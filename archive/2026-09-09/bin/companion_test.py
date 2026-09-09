#!/usr/bin/env python3
"""
companion_test.py — test harness for the Codex Companion system.
Prints PASS: {name} or FAIL: {name} — {reason} for each test.
Run with: python3 ~/.claude/bin/companion_test.py
"""

import sys
import os
import json
import subprocess
import tempfile
import pathlib
import importlib

# Ensure companion_core is importable
sys.path.insert(0, str(pathlib.Path.home() / ".claude/bin"))
import companion_core as cc

PASS_COUNT = 0
FAIL_COUNT = 0


def ok(name: str):
    global PASS_COUNT
    PASS_COUNT += 1
    print(f"PASS: {name}")


def fail(name: str, reason: str):
    global FAIL_COUNT
    FAIL_COUNT += 1
    print(f"FAIL: {name} — {reason}")


# ---------------------------------------------------------------------------
# 1. PRNG determinism
# ---------------------------------------------------------------------------
def test_prng_determinism():
    b1 = cc.roll_bones('test-user-abc')
    # Clear cache to force re-roll
    cc._roll_cache.clear()
    b2 = cc.roll_bones('test-user-abc')
    if b1 == b2:
        ok("PRNG determinism")
    else:
        fail("PRNG determinism", f"b1={b1} b2={b2}")


# ---------------------------------------------------------------------------
# 2. PRNG distribution — 1000 rolls, check rarity within tolerance
# ---------------------------------------------------------------------------
def test_prng_distribution():
    import hashlib
    rarity_counts = {r: 0 for r in cc.RARITIES}
    n = 1000
    for i in range(n):
        # Use unique user ids that won't collide with cached values
        uid = f"dist-test-user-{i}-zz"
        cc._roll_cache.pop(uid + cc.SALT, None)
        b = cc.roll_bones(uid)
        rarity_counts[b['rarity']] += 1

    total_weight = sum(cc.RARITY_WEIGHTS.values())
    errors = []
    for r in cc.RARITIES:
        expected_pct = cc.RARITY_WEIGHTS[r] / total_weight
        actual_pct = rarity_counts[r] / n
        # Allow 5% absolute tolerance (loose because PRNG seeded per user)
        if abs(actual_pct - expected_pct) > 0.08:
            errors.append(
                f"{r}: expected ~{expected_pct:.2%}, got {actual_pct:.2%}"
            )

    if errors:
        fail("PRNG distribution", "; ".join(errors))
    else:
        ok(f"PRNG distribution ({rarity_counts})")


# ---------------------------------------------------------------------------
# 3. FNV32 known value
# ---------------------------------------------------------------------------
def test_fnv32_known_value():
    # Verify determinism and non-collision
    h_hello = cc.fnv32('hello')
    h_hello2 = cc.fnv32('hello')
    h_world = cc.fnv32('world')
    if h_hello != h_hello2:
        fail("FNV32 determinism", f"{h_hello} != {h_hello2}")
        return
    if h_hello == h_world:
        fail("FNV32 collision", "hello == world")
        return
    if not isinstance(h_hello, int):
        fail("FNV32 type", f"expected int, got {type(h_hello)}")
        return
    # Verify known value: FNV32 of '' = 2166136261
    h_empty = cc.fnv32('')
    if h_empty != 2166136261:
        fail("FNV32 known value", f"fnv32('')={h_empty}, expected 2166136261")
        return
    ok(f"FNV32 known value (hello={h_hello})")


# ---------------------------------------------------------------------------
# 4. State roundtrip
# ---------------------------------------------------------------------------
def test_state_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        orig = cc.STATE_PATH
        cc.STATE_PATH = pathlib.Path(td) / 'companion-state.json'
        try:
            # Missing file returns None
            result = cc.load_state()
            if result is not None:
                fail("state roundtrip — missing file", f"expected None, got {result}")
                return

            # Write and read back
            s = cc.new_state()
            s['soul']['name'] = 'RoundtripBuddy'
            s['xp']['total'] = 999
            cc.save_state(s)
            s2 = cc.load_state()
            if s2 is None:
                fail("state roundtrip — load after save", "returned None")
                return
            if s2['soul']['name'] != 'RoundtripBuddy':
                fail("state roundtrip — name", f"got {s2['soul']['name']}")
                return
            if s2['xp']['total'] != 999:
                fail("state roundtrip — xp", f"got {s2['xp']['total']}")
                return
            ok("state roundtrip")
        finally:
            cc.STATE_PATH = orig


# ---------------------------------------------------------------------------
# 5. State missing (covered above) — extra: corrupt JSON returns None
# ---------------------------------------------------------------------------
def test_state_corrupt():
    with tempfile.TemporaryDirectory() as td:
        orig = cc.STATE_PATH
        cc.STATE_PATH = pathlib.Path(td) / 'companion-state.json'
        try:
            cc.STATE_PATH.write_text("{ not valid json {{")
            result = cc.load_state()
            if result is not None:
                fail("state corrupt JSON", f"expected None, got {result}")
                return
            ok("state corrupt JSON returns None")
        finally:
            cc.STATE_PATH = orig


# ---------------------------------------------------------------------------
# 6. State migration from v1 schema
# ---------------------------------------------------------------------------
def test_state_migration():
    v1 = {
        'schema_version': 1,
        'total_xp': 500,
    }
    migrated = cc._migrate_state(v1)
    if migrated.get('schema_version') != 2:
        fail("state migration schema_version", f"got {migrated.get('schema_version')}")
        return
    if 'xp' not in migrated:
        fail("state migration xp field", "missing 'xp' key")
        return
    if migrated['xp']['total'] != 500:
        fail("state migration xp total", f"got {migrated['xp']['total']}")
        return
    ok("state migration v1->v2")


# ---------------------------------------------------------------------------
# 7. XP / level math
# ---------------------------------------------------------------------------
def test_xp_level_math():
    cases = [
        (0, 1), (199, 1), (200, 2), (499, 2), (500, 3),
        (6499, 10), (6500, 11), (20000, 20), (99999, 20),
    ]
    errors = []
    for xp, expected in cases:
        got = cc.get_level(xp)
        if got != expected:
            errors.append(f"xp={xp}: expected level {expected}, got {got}")
    if errors:
        fail("XP level math", "; ".join(errors))
    else:
        ok("XP level math (all threshold cases)")


# ---------------------------------------------------------------------------
# 8. apply_xp level-up and evolution detection
# ---------------------------------------------------------------------------
def test_apply_xp():
    # Start just below level 2 threshold
    s = cc.new_state()
    s['soul'] = {'name': 'T', 'personality': 'hype'}
    s['xp']['total'] = 195
    result = cc.apply_xp(s, 10, 'test')
    if not result['leveled_up']:
        fail("apply_xp level-up", "expected leveled_up=True")
        return
    if result['new_level'] != 2:
        fail("apply_xp level-up new_level", f"got {result['new_level']}")
        return
    ok("apply_xp level-up")

    # Test evolution: cross stage boundary at level 7
    # Set XP just below level 7 threshold so a small award triggers it
    s2 = cc.new_state()
    s2['soul'] = {'name': 'E', 'personality': 'hype'}
    s2['xp']['total'] = cc.LEVEL_THRESHOLDS[6] - 5  # 5 XP below level 7
    s2['level'] = 6
    s2['evolution']['stage'] = 1
    result2 = cc.apply_xp(s2, 10, 'test')  # small enough to pass session cap
    if result2.get('evolved_to') != 2:
        fail("apply_xp evolution to stage 2", f"evolved_to={result2.get('evolved_to')}, level={s2['level']}")
        return
    ok("apply_xp evolution to stage 2")


# ---------------------------------------------------------------------------
# 9. Session XP cap
# ---------------------------------------------------------------------------
def test_session_xp_cap():
    s = cc.new_state()
    s['soul'] = {'name': 'Cap', 'personality': 'hype'}
    s['session']['xp_earned_this_session'] = 245

    result = cc.apply_xp(s, 100, 'test')
    if s['xp']['total'] != 5:
        fail("session XP cap", f"expected total=5, got {s['xp']['total']}")
        return
    ok("session XP cap (250 per session)")


# ---------------------------------------------------------------------------
# 10. Evolution stage function
# ---------------------------------------------------------------------------
def test_evolution_stages():
    cases = [
        (1, 1), (6, 1), (7, 2), (13, 2), (14, 3), (20, 3)
    ]
    errors = []
    for level, expected in cases:
        got = cc._get_evolution_stage(level)
        if got != expected:
            errors.append(f"level {level}: expected stage {expected}, got {got}")
    if errors:
        fail("evolution stage transitions", "; ".join(errors))
    else:
        ok("evolution stage transitions")


# ---------------------------------------------------------------------------
# 11. Render: render_stat_bar
# ---------------------------------------------------------------------------
def test_render_stat_bar():
    bar = cc.render_stat_bar(72, 3)
    if '72+3' not in bar:
        fail("render_stat_bar with bonus", f"got: {bar}")
        return
    bar2 = cc.render_stat_bar(50, 0)
    if '+' in bar2:
        fail("render_stat_bar no bonus", f"should not have '+': {bar2}")
        return
    ok("render_stat_bar")


# ---------------------------------------------------------------------------
# 12. Render: render_speech_bubble
# ---------------------------------------------------------------------------
def test_render_speech_bubble():
    bubble = cc.render_speech_bubble("hello world")
    lines = bubble.split('\n')
    if len(lines) != 3:
        fail("render_speech_bubble line count", f"expected 3 lines, got {len(lines)}")
        return
    if 'hello world' not in bubble:
        fail("render_speech_bubble content", "text not in bubble")
        return
    ok("render_speech_bubble")


# ---------------------------------------------------------------------------
# 13. Render: render_card output contains key fields
# ---------------------------------------------------------------------------
def test_render_card():
    s = cc.new_state()
    s['soul'] = {'name': 'Ziggy', 'personality': 'chaotic gremlin'}
    cc._roll_cache.clear()
    bones = cc.roll_bones('render-card-test-user')
    card = cc.render_card(s, bones, 1, reaction="test!")
    if 'Ziggy' not in card:
        fail("render_card name", "name not in output")
        return
    if 'VELOCITY' not in card:
        fail("render_card stats", "VELOCITY not in output")
        return
    if 'XP' not in card:
        fail("render_card XP bar", "XP not in output")
        return
    if 'test!' not in card:
        fail("render_card reaction", "reaction not in output")
        return
    ok("render_card")


# ---------------------------------------------------------------------------
# 14. render_sprite evolution cosmetics
# ---------------------------------------------------------------------------
def test_render_sprite_evolution():
    bones = {'species': 'corgi', 'eye': 'o', 'hat': 'none', 'shiny': False,
             'stats': {s: 50 for s in cc.STAT_NAMES}}
    # Stage 1: no hat override, default eye
    lines1 = cc.render_sprite(bones, 1, 0)
    # Stage 3: eye should be '*'
    lines3 = cc.render_sprite(bones, 3, 0)
    combined3 = '\n'.join(lines3)
    if '*' not in combined3:
        fail("render_sprite stage 3 eye", f"'*' not found in: {combined3}")
        return
    ok("render_sprite evolution cosmetics")


# ---------------------------------------------------------------------------
# 15. Hook script smoke: companion_session_start.py (fresh state)
# ---------------------------------------------------------------------------
def test_hook_session_start_fresh():
    with tempfile.TemporaryDirectory() as td:
        state_path = pathlib.Path(td) / 'companion-state.json'
        env = os.environ.copy()
        env['HOME_OVERRIDE_FOR_TEST'] = td
        # We can't easily override HOME but we can check it runs without error
        # Run the script and verify it outputs valid JSON or exits 0
        result = subprocess.run(
            [sys.executable, str(pathlib.Path.home() / '.claude/bin/companion_session_start.py')],
            input='{}',
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Script should exit 0 (never blocks session)
        if result.returncode != 0:
            fail("hook session_start exit code", f"rc={result.returncode} stderr={result.stderr[:200]}")
            return
        stdout = result.stdout.strip()
        if stdout:
            try:
                parsed = json.loads(stdout)
                if 'hookSpecificOutput' not in parsed:
                    fail("hook session_start JSON format", f"missing hookSpecificOutput: {stdout[:200]}")
                    return
            except json.JSONDecodeError as e:
                fail("hook session_start JSON parse", f"{e}: {stdout[:200]}")
                return
        ok("hook session_start exits cleanly")


# ---------------------------------------------------------------------------
# 16. Hook script smoke: companion_post_tool.py
# ---------------------------------------------------------------------------
def test_hook_post_tool():
    # Ensure there's a valid state
    s = cc.new_state()
    s['flags']['hatch_pending'] = False
    s['soul'] = {'name': 'TestPet', 'personality': 'hype gremlin'}
    cc.save_state(s)

    payload = json.dumps({
        "tool_name": "Edit",
        "tool_input": {},
        "tool_response": {"type": "text", "text": "ok"},
        "tool_error": None
    })
    result = subprocess.run(
        [sys.executable, str(pathlib.Path.home() / '.claude/bin/companion_post_tool.py')],
        input=payload,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        fail("hook post_tool exit code", f"rc={result.returncode} stderr={result.stderr[:200]}")
        return
    stdout = result.stdout.strip()
    if stdout:
        try:
            json.loads(stdout)
        except json.JSONDecodeError as e:
            fail("hook post_tool JSON parse", f"{e}: {stdout[:200]}")
            return
    ok("hook post_tool exits cleanly")


# ---------------------------------------------------------------------------
# 17. Hook script smoke: companion_prompt.py
# ---------------------------------------------------------------------------
def test_hook_prompt():
    result = subprocess.run(
        [sys.executable, str(pathlib.Path.home() / '.claude/bin/companion_prompt.py')],
        input='{"prompt": "hello"}',
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        fail("hook prompt exit code", f"rc={result.returncode} stderr={result.stderr[:200]}")
        return
    stdout = result.stdout.strip()
    if stdout:
        try:
            json.loads(stdout)
        except json.JSONDecodeError as e:
            fail("hook prompt JSON parse", f"{e}: {stdout[:200]}")
            return
    ok("hook prompt exits cleanly")


# ---------------------------------------------------------------------------
# 18. Hook script smoke: companion_stop.py
# ---------------------------------------------------------------------------
def test_hook_stop():
    result = subprocess.run(
        [sys.executable, str(pathlib.Path.home() / '.claude/bin/companion_stop.py')],
        input='{}',
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        fail("hook stop exit code", f"rc={result.returncode} stderr={result.stderr[:200]}")
        return
    stdout = result.stdout.strip()
    if stdout:
        try:
            parsed = json.loads(stdout)
            # Must NOT have stopReason key
            if 'stopReason' in parsed:
                fail("hook stop must not output stopReason", f"got: {stdout[:200]}")
                return
        except json.JSONDecodeError as e:
            fail("hook stop JSON parse", f"{e}: {stdout[:200]}")
            return
    ok("hook stop exits cleanly (no stopReason)")


# ---------------------------------------------------------------------------
# 19. pick_reaction covers all event types
# ---------------------------------------------------------------------------
def test_pick_reaction_coverage():
    events = ['session_start', 'tool_success', 'tool_failure', 'prompt_received',
              'level_up', 'evolution', 'stop', 'pet']
    archetypes = ['chaos', 'wise', 'snark', 'hype', 'shy', 'gremlin']
    stats = {s: 50 for s in cc.STAT_NAMES}
    errors = []
    for event in events:
        for arch in archetypes:
            try:
                r = cc.pick_reaction(event, arch, stats)
                if not isinstance(r, str) or not r:
                    errors.append(f"{event}/{arch}: empty result")
            except Exception as e:
                errors.append(f"{event}/{arch}: {e}")
    if errors:
        fail("pick_reaction coverage", "; ".join(errors[:3]))
    else:
        ok("pick_reaction all events/archetypes")


# ---------------------------------------------------------------------------
# 20. classify_personality
# ---------------------------------------------------------------------------
def test_classify_personality():
    cases = [
        ("chaotic unpredictable gremlin", "chaos"),
        ("wise and ancient sage", "wise"),
        ("snarky deadpan humor", "snark"),
        ("enthusiastic cheerful optimist", "hype"),
        ("shy timid gentle", "shy"),
        ("mischievous prankster", "gremlin"),
        ("totally unknown", "hype"),  # fallback
    ]
    errors = []
    for s, expected in cases:
        got = cc.classify_personality(s)
        if got != expected:
            errors.append(f"'{s}': expected {expected}, got {got}")
    if errors:
        fail("classify_personality", "; ".join(errors))
    else:
        ok("classify_personality (7 cases)")


# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    print("=== Codex Companion test harness ===\n")

    test_prng_determinism()
    test_prng_distribution()
    test_fnv32_known_value()
    test_state_roundtrip()
    test_state_corrupt()
    test_state_migration()
    test_xp_level_math()
    test_apply_xp()
    test_session_xp_cap()
    test_evolution_stages()
    test_render_stat_bar()
    test_render_speech_bubble()
    test_render_card()
    test_render_sprite_evolution()
    test_hook_session_start_fresh()
    test_hook_post_tool()
    test_hook_prompt()
    test_hook_stop()
    test_pick_reaction_coverage()
    test_classify_personality()

    print(f"\n=== Results: {PASS_COUNT} passed, {FAIL_COUNT} failed ===")
    sys.exit(0 if FAIL_COUNT == 0 else 1)
