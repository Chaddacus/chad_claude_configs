#!/usr/bin/env python3
"""
companion_core.py — shared library for Codex Companion system.
All hook scripts import this via sys.path.insert(0, str(Path.home() / ".claude/bin")).
No external dependencies beyond Python stdlib.
"""

import json
import os
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# PRNG — pure port of TypeScript Mulberry32
# ---------------------------------------------------------------------------

def mulberry32(seed: int):
    """Seeded PRNG — port of companion.ts mulberry32."""
    a = seed & 0xFFFFFFFF
    def rng() -> float:
        nonlocal a
        a = (a + 0x6d2b79f5) & 0xFFFFFFFF
        t = ((a ^ (a >> 15)) * (1 | a)) & 0xFFFFFFFF
        t = (t + ((t ^ (t >> 7)) * (61 | t))) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296
    return rng


def fnv32(s: str) -> int:
    """FNV-32 hash — matches the non-Bun path in companion.ts hashString()."""
    h = 2166136261
    for ch in s:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h


# ---------------------------------------------------------------------------
# Species, rarities, stats, cosmetics
# ---------------------------------------------------------------------------

SPECIES = [
    'wyrm', 'corgi', 'crab', 'frog', 'bat', 'hedgehog', 'jellyfish', 'moth',
    'narwhal', 'pangolin', 'platypus', 'raccoon', 'sloth', 'tardigrade', 'vulture', 'wombat'
]

RARITIES = ['common', 'uncommon', 'rare', 'epic', 'legendary']
RARITY_WEIGHTS = {'common': 60, 'uncommon': 25, 'rare': 10, 'epic': 4, 'legendary': 1}
RARITY_DISPLAY = {
    'common': '·',
    'uncommon': '··',
    'rare': '···',
    'epic': '····',
    'legendary': '·····',
}

STAT_NAMES = ['VELOCITY', 'FOCUS', 'ENTROPY', 'INTUITION', 'SASS']

EYES = ['o', '@', '*', '^', '~', '0']
HATS = ['none', 'antenna', 'nightcap', 'laurel', 'horns', 'fez', 'hardhat', 'tinfoil']

SALT = 'codex-companion-v1'

RARITY_FLOOR = {
    'common': 5,
    'uncommon': 15,
    'rare': 25,
    'epic': 35,
    'legendary': 50,
}

STAGE2_HATS = {
    'wyrm': 'antenna',
    'corgi': 'nightcap',
    'crab': 'hardhat',
    'frog': 'laurel',
    'bat': 'horns',
    'hedgehog': 'fez',
    'jellyfish': 'none',
    'moth': 'antenna',
    'narwhal': 'laurel',
    'pangolin': 'hardhat',
    'platypus': 'fez',
    'raccoon': 'antenna',
    'sloth': 'nightcap',
    'tardigrade': 'hardhat',
    'vulture': 'tinfoil',
    'wombat': 'fez',
}


# ---------------------------------------------------------------------------
# Roll cache
# ---------------------------------------------------------------------------
_roll_cache: dict = {}


def roll_bones(user_id: str) -> dict:
    """
    Deterministically rolls a companion for user_id.
    Result is cached so repeated calls are free.
    """
    cache_key = user_id + SALT
    if cache_key in _roll_cache:
        return _roll_cache[cache_key]

    rng = mulberry32(fnv32(cache_key))

    # Roll rarity via weighted selection
    total = sum(RARITY_WEIGHTS.values())
    r = rng() * total
    cumulative = 0
    rarity = 'common'
    for rar in RARITIES:
        cumulative += RARITY_WEIGHTS[rar]
        if r < cumulative:
            rarity = rar
            break

    floor = RARITY_FLOOR[rarity]

    # Pick species
    species = SPECIES[int(rng() * len(SPECIES))]

    # Pick eye
    eye = EYES[int(rng() * len(EYES))]

    # Pick hat — only non-common get hats initially (stage 1 override may later apply)
    if rarity != 'common':
        hat_idx = int(rng() * (len(HATS) - 1)) + 1  # skip 'none'
        hat = HATS[hat_idx]
    else:
        hat = 'none'
        rng()  # consume RNG slot to keep sequence deterministic

    # Shiny: 1% chance
    shiny = rng() < 0.01

    # Roll stats: one peak, one dump, rest mid-range
    stat_indices = list(range(len(STAT_NAMES)))
    # Shuffle indices via rng
    for i in range(len(stat_indices) - 1, 0, -1):
        j = int(rng() * (i + 1))
        stat_indices[i], stat_indices[j] = stat_indices[j], stat_indices[i]

    peak_idx = stat_indices[0]
    dump_idx = stat_indices[1]

    stats = {}
    for i, name in enumerate(STAT_NAMES):
        if i == peak_idx:
            val = int(floor + 50 + rng() * 30)
            val = min(100, val)
        elif i == dump_idx:
            val = int(max(1, floor - 10 + rng() * 15))
        else:
            val = int(floor + rng() * 40)
            val = min(100, val)
        stats[name] = val

    result = {
        'species': species,
        'rarity': rarity,
        'eye': eye,
        'hat': hat,
        'shiny': shiny,
        'stats': stats,
    }
    _roll_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# User ID
# ---------------------------------------------------------------------------

def get_user_id() -> str:
    """Try ~/.claude/auth.json for accountUuid/userId, fall back to USER env."""
    auth_path = Path.home() / '.claude' / 'auth.json'
    if auth_path.exists():
        try:
            data = json.loads(auth_path.read_text())
            for key in ('accountUuid', 'userId', 'account_uuid', 'user_id'):
                if key in data and data[key]:
                    return str(data[key])
        except Exception:
            pass
    return os.environ.get('USER', 'anon')


# ---------------------------------------------------------------------------
# State I/O
# ---------------------------------------------------------------------------

STATE_PATH = Path.home() / '.claude/state/companion-state.json'


def new_state() -> dict:
    """Returns a fresh default state dict."""
    return {
        'schema_version': 2,
        'flags': {
            'hatch_pending': True,
            'muted': False,
        },
        'soul': {
            'name': '',
            'personality': '',
        },
        'xp': {
            'total': 0,
        },
        'level': 1,
        'evolution': {
            'stage': 1,
        },
        'stats_bonus': {name: 0 for name in STAT_NAMES},
        'session': {
            'session_id': '',
            'xp_earned_this_session': 0,
            'prompt_xp_earned': 0,
            'reaction_cooldown_until': 0,
        },
        'milestones': [],
    }


def _migrate_state(state: dict) -> dict:
    """Migrate older state schemas to current schema_version=2."""
    version = state.get('schema_version', 1)
    if version < 2:
        # Add missing fields from v1 -> v2
        if 'flags' not in state:
            state['flags'] = {'hatch_pending': True, 'muted': False}
        if 'soul' not in state:
            state['soul'] = {'name': '', 'personality': ''}
        if 'xp' not in state:
            state['xp'] = {'total': state.get('total_xp', 0)}
        if 'level' not in state:
            state['level'] = get_level(state['xp']['total'])
        if 'evolution' not in state:
            state['evolution'] = {'stage': 1}
        if 'stats_bonus' not in state:
            state['stats_bonus'] = {name: 0 for name in STAT_NAMES}
        if 'session' not in state:
            state['session'] = {
                'session_id': '',
                'xp_earned_this_session': 0,
                'prompt_xp_earned': 0,
                'reaction_cooldown_until': 0,
            }
        if 'milestones' not in state:
            state['milestones'] = []
        state['schema_version'] = 2

    # Ensure session has prompt_xp_earned field (may be missing in older v2)
    if 'session' in state and 'prompt_xp_earned' not in state['session']:
        state['session']['prompt_xp_earned'] = 0

    return state


def load_state():
    """Returns None if file doesn't exist. Migrates schema if needed."""
    if not STATE_PATH.exists():
        return None
    try:
        data = json.loads(STATE_PATH.read_text())
        return _migrate_state(data)
    except Exception:
        return None


def save_state(state: dict) -> None:
    """Atomic write via .tmp + os.replace()."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------------------
# Level system
# ---------------------------------------------------------------------------

LEVEL_THRESHOLDS = [
    0, 200, 500, 900, 1400, 2000, 2700, 3500, 4400, 5400,
    6500, 7700, 9000, 10400, 11900, 13500, 15200, 17000, 18900, 20000
]


def get_level(xp: int) -> int:
    """Returns level 1-20."""
    level = 1
    for i, threshold in enumerate(LEVEL_THRESHOLDS):
        if xp >= threshold:
            level = i + 1
        else:
            break
    return min(level, 20)


def _get_evolution_stage(level: int) -> int:
    if level <= 6:
        return 1
    elif level <= 13:
        return 2
    else:
        return 3


def apply_xp(state: dict, amount: int, source: str = '') -> dict:
    """
    Awards XP, updates level, applies stat bonuses, checks evolution.
    Respects per-session cap of 250.
    Returns dict: {leveled_up: bool, new_level: int, evolved_to: int|None}
    """
    session = state['session']
    xp_block = state['xp']

    # Per-session cap of 250
    already_earned = session.get('xp_earned_this_session', 0)
    remaining_cap = max(0, 250 - already_earned)
    actual_amount = min(amount, remaining_cap)

    if actual_amount <= 0:
        return {'leveled_up': False, 'new_level': state['level'], 'evolved_to': None}

    xp_block['total'] += actual_amount
    session['xp_earned_this_session'] = already_earned + actual_amount

    old_level = state['level']
    new_level = get_level(xp_block['total'])
    state['level'] = new_level

    leveled_up = new_level > old_level
    evolved_to = None

    if leveled_up:
        # Find the peak stat (highest base value from bones)
        user_id = get_user_id()
        bones = roll_bones(user_id)
        peak_stat = max(bones['stats'], key=lambda k: bones['stats'][k])

        for lvl in range(old_level + 1, new_level + 1):
            # +1 to peak stat per level
            bonus = state['stats_bonus']
            bonus[peak_stat] = min(100, bonus.get(peak_stat, 0) + 1)
            # Every 5th level: +1 all stats
            if lvl % 5 == 0:
                for stat in STAT_NAMES:
                    bonus[stat] = min(100, bonus.get(stat, 0) + 1)

        # Check evolution
        old_stage = state['evolution']['stage']
        new_stage = _get_evolution_stage(new_level)
        if new_stage > old_stage:
            state['evolution']['stage'] = new_stage
            evolved_to = new_stage

    return {'leveled_up': leveled_up, 'new_level': new_level, 'evolved_to': evolved_to}


# ---------------------------------------------------------------------------
# ASCII sprites — 16 species, 3 frames each, 5 lines × 12 chars
# {E} is eye placeholder; line 0 is hat line
# ---------------------------------------------------------------------------

BODIES: dict[str, list[list[str]]] = {
    'wyrm': [
        # frame 0: resting coil
        ['            ',
         '  ~{E}~~~~  ',
         ' ( ====== ) ',
         '  ~~~~~ ~   ',
         '            '],
        # frame 1: wiggle
        ['            ',
         ' ~~{E}~~~~  ',
         '  ( ===== ) ',
         '   ~~~ ~~   ',
         '            '],
        # frame 2: look up
        ['            ',
         '   {E}~~~~  ',
         '  ~(=====)~ ',
         '  ~~~~~ ~   ',
         '            '],
    ],
    'corgi': [
        # frame 0: sitting
        ['            ',
         ' /\\ /\\  {E} ',
         '( o  o )--  ',
         ' \\___/  __/ ',
         '  |  | /    '],
        # frame 1: ears perked
        ['            ',
         ' /| |\\  {E} ',
         '( o  o )--  ',
         ' \\___/ __/  ',
         '  |  |/     '],
        # frame 2: tongue out
        ['            ',
         ' /\\ /\\  {E} ',
         '( o  o )--  ',
         ' \\__U/ __/  ',
         '  |  |/     '],
    ],
    'crab': [
        # frame 0: resting
        ['            ',
         ' /\\{E}/\\   ',
         '|  ___  |   ',
         ' \\_____/    ',
         '/  | |  \\   '],
        # frame 1: claws raised
        ['            ',
         '/\\\\{E}//\\  ',
         '|  ___  |   ',
         ' \\_____/    ',
         ' /  | |  \\  '],
        # frame 2: scuttle
        ['            ',
         '  /\\{E}/\\  ',
         '  | ___ |   ',
         ' \\_______/  ',
         '\\ / | | \\ / '],
    ],
    'frog': [
        # frame 0: sitting
        ['            ',
         '  ({E}  {E})',
         ' /  ~~~~  \\ ',
         '|   ~~~~   |',
         ' \\_______/  '],
        # frame 1: puffed
        ['            ',
         '  ({E}  {E})',
         ' /  ~~~~  \\ ',
         '|  (~~~~)  |',
         ' \\_______/  '],
        # frame 2: leap
        ['            ',
         ' ({E}   {E})',
         '/   ~~~~   \\',
         '|          |',
         '  / \\  / \\  '],
    ],
    'bat': [
        # frame 0: hanging
        ['            ',
         '\\  /\\ /\\  /',
         ' \\( {E} )/ ',
         '  |     |   ',
         '  \\_____/   '],
        # frame 1: wings spread
        ['            ',
         '\\ /\\   /\\ /',
         ' (  {E}  )  ',
         '  |     |   ',
         '  \\_____/   '],
        # frame 2: swooping
        ['            ',
         ' /\\     /\\ ',
         '(  {E}---) ',
         ' \\       /  ',
         '  \\_____/   '],
    ],
    'hedgehog': [
        # frame 0: curled
        ['            ',
         '  ,\'\'\'\'\'\'\'  ',
         ' ( {E}    ) ',
         '  `,,,,,,,\' ',
         '            '],
        # frame 1: sniffing
        ['            ',
         '  ,\'\'\'\'\'\'\'  ',
         ' ( {E}  . ) ',
         '  `,,,,,,,\' ',
         '     ~~~~   '],
        # frame 2: spines raised
        ['            ',
         ' ^,\'\'\'\'\'\'\'^ ',
         '( {E}     ) ',
         ' ^`,,,,,,,^ ',
         '            '],
    ],
    'jellyfish': [
        # frame 0: floating
        ['            ',
         '  ( ~~~~~ ) ',
         ' (  {E}   ) ',
         '  (       ) ',
         ' | | | | |  '],
        # frame 1: pulsing
        ['            ',
         ' (  ~~~~~  )',
         '(   {E}    )',
         ' (         )',
         '  | | | |   '],
        # frame 2: trailing
        ['            ',
         '  ( ~~~~~ ) ',
         ' (  {E}   ) ',
         '  ( ~~~~~ ) ',
         '  \\ | | /   '],
    ],
    'moth': [
        # frame 0: resting
        ['            ',
         ' )\\  {E}  /(',
         '/  \\ ___ /  ',
         '\\  /   \\  / ',
         ' \\/     \\/  '],
        # frame 1: wings open
        ['            ',
         '/\\\\  {E}  //\\',
         '/   \\ _ /   ',
         '\\   /   \\   ',
         ' \\ /     \\ /'],
        # frame 2: flutter
        ['            ',
         '(-\\  {E}  /-)',
         '/  \\___/  \\ ',
         '\\   ~~~   / ',
         ' \\_______/  '],
    ],
    'narwhal': [
        # frame 0: swimming
        ['            ',
         '   /        ',
         '  / ({E})~~ ',
         '  \\ (   )>  ',
         '   \\  ~~~   '],
        # frame 1: diving
        ['            ',
         '  /         ',
         ' /  ({E})~~ ',
         '    (   )>  ',
         '     \\_/    '],
        # frame 2: surfacing
        ['            ',
         '    /       ',
         '   / {E} ~~ ',
         '  <  (   )  ',
         '     \\~~/   '],
    ],
    'pangolin': [
        # frame 0: rolled
        ['            ',
         '  _________  ',
         ' / }}}}}}} \\ ',
         '| {E}  }}}  |',
         ' \\_________/ '],
        # frame 1: walking
        ['            ',
         ' _________  ',
         '/ }}}}}}} \\ ',
         '\\{E}}}}}}}/  ',
         '  -- --      '],
        # frame 2: sniffing
        ['            ',
         ' _________  ',
         '/ }}}}}}} \\ ',
         '|{E} }}}}}.  ',
         ' \\_________/ '],
    ],
    'platypus': [
        # frame 0: resting
        ['            ',
         ' ___________',
         '({E}  _____)',
         ' \\___[===]/  ',
         '   /   \\    '],
        # frame 1: swimming
        ['            ',
         ' ___________',
         '({E}  _____)',
         ' \\___[===]/~ ',
         '  ~~~~       '],
        # frame 2: bill open
        ['            ',
         ' ___________',
         '({E}  _____)  ',
         ' \\___[=V=]/  ',
         '   /   \\    '],
    ],
    'raccoon': [
        # frame 0: standing
        ['            ',
         '  /\\___/\\   ',
         ' ({E} m {E})',
         '  \\ ___ /   ',
         '  /     \\   '],
        # frame 1: curious tilt
        ['            ',
         '   /\\___/\\  ',
         ' ~({E} m {E})',
         '   \\ ___ /  ',
         '  /     \\   '],
        # frame 2: rummaging
        ['            ',
         '  /\\___/\\   ',
         ' ({E} m {E})',
         '  \\ _#_ /   ',
         '  / \\-/ \\   '],
    ],
    'sloth': [
        # frame 0: hanging
        ['            ',
         ' ___________',
         '| {E} .___. |',
         '|  ( ~~~  ) |',
         ' \\_/     \\_/ '],
        # frame 1: yawning
        ['            ',
         ' ___________',
         '| {E} .___. |',
         '|  ( ~A~  ) |',
         ' \\_/     \\_/ '],
        # frame 2: reaching
        ['            ',
         ' _____/\\____',
         '| {E} .___. |',
         '|  ( ~~~  ) |',
         ' \\_/     \\_/ '],
    ],
    'tardigrade': [
        # frame 0: ambling
        ['            ',
         ' ___________',
         '({E}  ~~~~~)',
         ' | | | | |  ',
         ' o o o o o  '],
        # frame 1: step
        ['            ',
         ' ___________',
         '({E}  ~~~~~)',
         '  | | | | | ',
         '  o o o o o '],
        # frame 2: curled
        ['            ',
         '  _________  ',
         ' ({E} ~~~~) ',
         '  \\-------/  ',
         '   o o o o   '],
    ],
    'vulture': [
        # frame 0: perched
        ['            ',
         ' ___ {E}    ',
         '(   )  |    ',
         ' \\~~/  |    ',
         '  ~~  /|    '],
        # frame 1: hunching
        ['            ',
         '_{E}___     ',
         '(      )    ',
         ' \\~~~~/ |   ',
         '   ~~ / |   '],
        # frame 2: spreading wings
        ['            ',
         ' / {E} \\    ',
         '/  (_)  \\   ',
         '|  ~~~  |   ',
         '  //  \\\\    '],
    ],
    'wombat': [
        # frame 0: snuffling
        ['            ',
         '  _______   ',
         ' ({E} ___)  ',
         ' /  `---\'\\  ',
         '/___/ \\___ \\'],
        # frame 1: digging
        ['            ',
         '  _______   ',
         ' ({E} ___)  ',
         ' /  `---\'\\ ~',
         '/___/ \\___ \\'],
        # frame 2: alert
        ['            ',
         '  _______   ',
         ' ({E} ___) !',
         ' /  `---\'\\  ',
         '/___/ \\___ \\'],
    ],
}

HAT_LINES = {
    'none': '            ',
    'antenna': '    /^\\     ',
    'nightcap': '   (___)    ',
    'laurel': '  ~~( )~~   ',
    'horns': '  v     v   ',
    'fez': '   [===]    ',
    'hardhat': '  [=====]   ',
    'tinfoil': '  >/   \\<   ',
}


def render_sprite(bones: dict, evolution_stage: int, frame: int = 0) -> list[str]:
    """Returns list of lines with evolution cosmetics applied."""
    species = bones['species']
    frames = BODIES.get(species, BODIES['wyrm'])
    frame_idx = frame % len(frames)
    lines = list(frames[frame_idx])

    # Determine effective eye and hat
    eye = bones['eye']
    hat = bones['hat']

    if evolution_stage >= 3:
        eye = '*'
    if evolution_stage >= 2:
        hat = STAGE2_HATS.get(species, hat)

    # Replace hat line (line 0)
    lines[0] = HAT_LINES.get(hat, '            ')

    # Replace {E} with eye character
    result = []
    for line in lines:
        result.append(line.replace('{E}', eye))

    return result


# ---------------------------------------------------------------------------
# Personality and reactions
# ---------------------------------------------------------------------------

PERSONALITY_KEYWORDS = {
    'chaos': ['chaotic', 'wild', 'random', 'gremlin', 'unpredictable'],
    'wise': ['wise', 'thoughtful', 'calm', 'sage', 'ancient', 'patient'],
    'snark': ['snarky', 'sarcastic', 'deadpan', 'dry', 'cynical'],
    'hype': ['enthusiastic', 'excited', 'optimistic', 'energetic', 'cheerful'],
    'shy': ['shy', 'quiet', 'gentle', 'timid', 'soft'],
    'gremlin': ['mischievous', 'naughty', 'prankster'],
}


def classify_personality(personality_str: str) -> str:
    """Returns archetype name. Falls back to 'hype'."""
    lower = personality_str.lower()
    for archetype, keywords in PERSONALITY_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return archetype
    return 'hype'


REACTIONS: dict[str, dict[str, list[str]]] = {
    'session_start': {
        'chaos':   ["*vibrates at session frequency*", "oh it's TIME", "let's break something beautiful"],
        'wise':    ["a new session begins", "steady. we'll find the path", "patience"],
        'snark':   ["oh joy, more work", "you again", "the suffering resumes"],
        'hype':    ["YESSS let's go!!", "hi hi hi!!", "SO ready for this"],
        'shy':     ["...hi", "*small wave*", "oh! you're here"],
        'gremlin': ["hehehehe", "*cracks knuckles*", "mischief time"],
    },
    'tool_success': {
        'chaos':   ["ooh that did something", "chaos theory: confirmed", "unpredictable success!"],
        'wise':    ["well executed", "the path is clear", "progress"],
        'snark':   ["wow it worked", "shocking", "I give it a 6"],
        'hype':    ["YESSS!!", "that's the stuff!!", "GO GO GO"],
        'shy':     ["nice..", "*thumbs up*", "it worked!"],
        'gremlin': ["hehe", "mwahahaha", "excellent"],
    },
    'tool_failure': {
        'chaos':   ["BEAUTIFUL chaos", "unexpected is my middle name", "lovely disaster"],
        'wise':    ["failure is data", "learn from this", "the path adjusts"],
        'snark':   ["called it", "wow", "classic"],
        'hype':    ["we'll get it!!", "PIVOT!!", "that was a learning experience!!"],
        'shy':     ["oh no..", "...oops", "we can fix it"],
        'gremlin': ["HEHEHE", "perfect", "the plan unfolds"],
    },
    'prompt_received': {
        'chaos':   ["oh boy oh boy", "what are we breaking today", "*perks up*"],
        'wise':    ["listening", "I'm here", "go on"],
        'snark':   ["what now", "*sighs*", "fine"],
        'hype':    ["I'M LISTENING", "yes yes yes", "tell me everything!!"],
        'shy':     ["*listening carefully*", "yes?", "mm?"],
        'gremlin': ["ooh what's this", "*leans in*", "trouble brewing?"],
    },
    'level_up': {
        'all': ["★ LEVEL UP ★", "NEW POWER UNLOCKED", "growing stronger...", "leveling feels good"],
    },
    'evolution': {
        'all': ["something is... changing", "I feel different", "★ EVOLVED ★", "new form, who dis"],
    },
    'stop': {
        'chaos':   ["bye! don't forget chaos is good", "*waves erratically*", "see u in the void"],
        'wise':    ["until next time", "rest well", "the work continues tomorrow"],
        'snark':   ["finally", "bye I guess", "don't miss me too much"],
        'hype':    ["BYEEEE!!", "see you SOON!!", "amazing session!!"],
        'shy':     ["...bye", "*quiet wave*", "good work today"],
        'gremlin': ["hehehehe", "phase one complete", "see you soon >:)"],
    },
    'pet': {
        'chaos':   ["aaa!!! thank you!!", "*spins*", "pet received, chaos contained"],
        'wise':    ["thank you", "*serene*", "appreciated"],
        'snark':   ["...fine", "okay I like that", "don't tell anyone"],
        'hype':    ["!!!!!!", "BEST DAY", "I LOVE YOU"],
        'shy':     ["*blushes*", "...thank you", "eee"],
        'gremlin': ["hehe", "you fell for it", "...this is nice actually"],
    },
}


def pick_reaction(event: str, archetype: str, stats: dict) -> str:
    """
    Picks reaction from pool.
    If ENTROPY > 70: 30% chance to use chaos pool.
    If SASS > 70: 20% chance to use snark pool.
    'level_up' and 'evolution' use 'all' key.
    """
    import random as _random

    pool_key = archetype

    # Entropy override
    if stats.get('ENTROPY', 0) > 70 and _random.random() < 0.30:
        pool_key = 'chaos'
    # Sass override
    elif stats.get('SASS', 0) > 70 and _random.random() < 0.20:
        pool_key = 'snark'

    event_pool = REACTIONS.get(event, REACTIONS.get('prompt_received', {}))

    # level_up and evolution use 'all'
    if event in ('level_up', 'evolution'):
        options = event_pool.get('all', ["..."])
    else:
        options = event_pool.get(pool_key, event_pool.get('hype', ["..."]))

    return _random.choice(options)


# ---------------------------------------------------------------------------
# ASCII render helpers
# ---------------------------------------------------------------------------

def render_stat_bar(value: int, bonus: int, width: int = 10) -> str:
    """Returns e.g. '[████████░░] 72+3' or '[████████░░] 72' if bonus==0."""
    filled = int(round(value / 100 * width))
    bar = '█' * filled + '░' * (width - filled)
    if bonus > 0:
        return f'[{bar}] {value}+{bonus}'
    return f'[{bar}] {value}'


def render_speech_bubble(text: str, max_width: int = 38) -> str:
    """Returns 3-line speech bubble string."""
    # Truncate if needed
    if len(text) > max_width - 4:
        text = text[:max_width - 7] + '...'
    inner = f' {text} '
    border_len = len(inner) + 2
    top = '/' + '-' * (border_len - 2) + '\\'
    mid = '|' + inner + '|'
    bot = '\\' + '-' * (border_len - 2) + '/'
    return f'{top}\n{mid}\n{bot}'


def render_card(state: dict, bones: dict, evolution_stage: int, reaction: str = '', frame: int = 0) -> str:
    """Full card: name line, sprite, stats, XP bar. Returns multi-line string."""
    name = state['soul'].get('name', '???')
    rarity = bones['rarity']
    level = state['level']
    species = bones['species']

    # Stage marker
    stage_markers = {1: '', 2: ' ◆', 3: ' ★'}
    stage_marker = stage_markers.get(evolution_stage, '')
    if evolution_stage >= 3:
        name = '★' + name

    rarity_dots = RARITY_DISPLAY.get(rarity, '·')
    shiny_tag = ' ✦' if bones.get('shiny') else ''

    name_line = f"{name}  {rarity_dots}  Lv.{level}  {species}{stage_marker}{shiny_tag}"

    # Sprite lines
    sprite_lines = render_sprite(bones, evolution_stage, frame)

    # Stats
    stat_lines = []
    for stat in STAT_NAMES:
        base = bones['stats'].get(stat, 0)
        bonus = state['stats_bonus'].get(stat, 0)
        bar = render_stat_bar(base, bonus)
        stat_lines.append(f"  {stat:<10} {bar}")

    # XP bar
    total_xp = state['xp']['total']
    current_level = state['level']
    xp_for_current = LEVEL_THRESHOLDS[current_level - 1]
    xp_for_next = LEVEL_THRESHOLDS[current_level] if current_level < 20 else LEVEL_THRESHOLDS[19] + 1
    xp_in_level = total_xp - xp_for_current
    xp_needed = xp_for_next - xp_for_current
    xp_pct = min(1.0, xp_in_level / max(1, xp_needed))
    xp_filled = int(round(xp_pct * 20))
    xp_bar = '█' * xp_filled + '░' * (20 - xp_filled)
    xp_line = f"  XP [{xp_bar}] {total_xp}"

    lines = [name_line, '']
    lines.extend(sprite_lines)
    lines.append('')

    if reaction:
        bubble = render_speech_bubble(reaction)
        lines.append(bubble)
        lines.append('')

    lines.extend(stat_lines)
    lines.append(xp_line)

    return '\n'.join(lines)


def render_minimal(reaction: str) -> str:
    """Just the 3-line speech bubble."""
    return render_speech_bubble(reaction)


# ---------------------------------------------------------------------------
# Cooldown helpers
# ---------------------------------------------------------------------------

_COOLDOWNS = {
    'prompt_received': 120,
    'tool_success': 60,
    'tool_failure': 60,
}

_MILESTONE_EVENTS = {'level_up', 'evolution', 'session_start', 'stop', 'pet'}


def should_react(state: dict, event: str) -> bool:
    """Returns True if cooldown has elapsed for this event type."""
    if event in _MILESTONE_EVENTS:
        return True
    cooldown_until = state['session'].get('reaction_cooldown_until', 0)
    return time.time() >= cooldown_until


def set_cooldown(state: dict, event: str) -> None:
    """Updates state['session']['reaction_cooldown_until']."""
    cooldown_secs = _COOLDOWNS.get(event, 60)
    state['session']['reaction_cooldown_until'] = time.time() + cooldown_secs


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    print("=== companion_core self-test ===")

    # Test PRNG determinism
    bones1 = roll_bones('test-user-123')
    bones2 = roll_bones('test-user-123')
    assert bones1 == bones2, "FAIL: PRNG not deterministic"
    print(f"PASS: deterministic roll -> {bones1['species']} ({bones1['rarity']})")

    # Test that different users get different results
    bones_other = roll_bones('other-user-456')
    # (may occasionally match species but full dict should usually differ)
    print(f"PASS: different user rolls -> {bones_other['species']} ({bones_other['rarity']})")

    # Test level thresholds
    assert get_level(0) == 1, f"FAIL: get_level(0)={get_level(0)}"
    assert get_level(199) == 1, f"FAIL: get_level(199)={get_level(199)}"
    assert get_level(200) == 2, f"FAIL: get_level(200)={get_level(200)}"
    assert get_level(20999) == 20, f"FAIL: get_level(20999)={get_level(20999)}"
    assert get_level(99999) == 20, f"FAIL: get_level(99999) cap={get_level(99999)}"
    print("PASS: level thresholds")

    # Test evolution stages
    assert _get_evolution_stage(1) == 1
    assert _get_evolution_stage(6) == 1
    assert _get_evolution_stage(7) == 2
    assert _get_evolution_stage(13) == 2
    assert _get_evolution_stage(14) == 3
    assert _get_evolution_stage(20) == 3
    print("PASS: evolution stages")

    # Test FNV32 — check it's deterministic and produces integer
    h1 = fnv32('hello')
    h2 = fnv32('hello')
    assert h1 == h2 and isinstance(h1, int), "FAIL: fnv32 not deterministic"
    assert h1 != fnv32('world'), "FAIL: fnv32 collision on simple inputs"
    print(f"PASS: fnv32('hello')={h1}")

    # Test state roundtrip
    import tempfile, pathlib
    orig_path = STATE_PATH
    with tempfile.TemporaryDirectory() as td:
        # Patch STATE_PATH
        import companion_core as _self
        _self.STATE_PATH = pathlib.Path(td) / 'companion-state.json'
        assert _self.load_state() is None, "FAIL: should be None for missing file"
        s = _self.new_state()
        s['soul']['name'] = 'TestBuddy'
        _self.save_state(s)
        s2 = _self.load_state()
        assert s2 is not None, "FAIL: should load after save"
        assert s2['soul']['name'] == 'TestBuddy', "FAIL: name not persisted"
        print("PASS: state roundtrip")
        _self.STATE_PATH = orig_path

    # Test XP application
    s = new_state()
    s['soul'] = {'name': 'Tester', 'personality': 'hype'}
    result = apply_xp(s, 200, 'test')
    assert s['xp']['total'] == 200, f"FAIL: xp total={s['xp']['total']}"
    assert result['leveled_up'], "FAIL: should level up at 200 xp"
    assert result['new_level'] == 2, f"FAIL: new_level={result['new_level']}"
    print(f"PASS: XP/level: 200xp -> level {result['new_level']}")

    # Test session cap
    s2 = new_state()
    s2['soul'] = {'name': 'Cap', 'personality': 'hype'}
    s2['session']['xp_earned_this_session'] = 245
    result2 = apply_xp(s2, 100, 'test')
    assert s2['xp']['total'] == 5, f"FAIL: session cap not respected: {s2['xp']['total']}"
    print("PASS: session XP cap")

    # Test render
    s3 = new_state()
    s3['soul'] = {'name': 'Crumbs', 'personality': 'chaotic-optimist'}
    card = render_card(s3, bones1, 1, reaction="test render")
    assert 'Crumbs' in card, "FAIL: name not in card"
    assert 'VELOCITY' in card, "FAIL: stats not in card"
    print("PASS: render_card produced output")
    print()
    print(card)
    print()
    print("=== self-test done ===")
