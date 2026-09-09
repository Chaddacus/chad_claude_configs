#!/usr/bin/env python3
"""Audit reader for the stop-gate calibration loop.

Aggregates ~/.claude/state/stop_gate_audit-*.jsonl files and produces a
rollup that lets you decide which L2 rules are safe to enable in
block-mode.

Usage:
    stop_gate_audit.py review                # full rollup (default)
    stop_gate_audit.py count                 # just total entries
    stop_gate_audit.py tail [--limit N]      # last N entries across all sessions
    stop_gate_audit.py rule <name>           # focus on one rule, show samples
    stop_gate_audit.py enable <rule>         # flip a single rule to block in config
    stop_gate_audit.py disable <rule>        # flip a single rule off in config
    stop_gate_audit.py mode <lexical|evidentiary> <block|log|off>
                                             # change overall gate mode
    stop_gate_audit.py clear [--keep-last N] # rotate audit logs (post-review)

Deterministic — no LLM. Counts, groups, samples.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

STATE_DIR = Path(os.path.expanduser("~/.claude/state"))
CONFIG_PATH = STATE_DIR / "stop_gate_config.json"
AUDIT_GLOB = "stop_gate_audit-*.jsonl"
AUDIT_ARCHIVE_DIR = STATE_DIR / "stop_gate_audit_archive"

DEFAULT_CONFIG = {
    "lexical": "block",
    "evidentiary": "log",
    "rules": {
        "verification_claims": True,
        "scope_claims": True,
        "state_claims": False,
        "edit_without_verify": True,
        "slice_reconciliation": True,
        "empty_diff_completion": True,
        "completion_record_required": False,
    },
}


# === IO helpers ============================================================

def audit_files() -> list[Path]:
    return sorted(STATE_DIR.glob(AUDIT_GLOB))


def read_all_entries() -> list[dict]:
    entries: list[dict] = []
    for p in audit_files():
        try:
            with p.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except IOError:
            continue
    return entries


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg["rules"] = dict(DEFAULT_CONFIG["rules"])
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text())
            if isinstance(user, dict):
                for k, v in user.items():
                    if k == "rules" and isinstance(v, dict):
                        cfg["rules"].update(v)
                    else:
                        cfg[k] = v
        except Exception:
            pass
    return cfg


def save_config(cfg: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


# === Commands ==============================================================

def cmd_count(_args) -> int:
    entries = read_all_entries()
    files = audit_files()
    print(f"audit entries: {len(entries)} across {len(files)} session log(s)")
    if entries:
        first = entries[0].get("ts", 0)
        last = entries[-1].get("ts", 0)
        if first and last:
            days = (last - first) / 86400.0
            print(f"timespan: {days:.2f} days")
    return 0


def cmd_tail(args) -> int:
    entries = read_all_entries()
    for e in entries[-args.limit:]:
        ts = e.get("ts", 0)
        mode = e.get("mode", "?")
        blocked = "BLOCK" if e.get("blocked") else "ALLOW"
        findings = e.get("findings", [])
        f_summary = ", ".join(f.get("rule", "?") for f in findings) or "(none)"
        print(f"[{time.strftime('%H:%M:%S', time.localtime(ts))}] {mode:<18} {blocked} rules=[{f_summary}]")
    return 0


def cmd_rule(args) -> int:
    rule = args.rule
    entries = read_all_entries()
    matches: list[dict] = []
    for e in entries:
        for f in e.get("findings", []):
            if f.get("rule") == rule:
                matches.append({"entry": e, "finding": f})
    print(f"rule={rule}: {len(matches)} firings across audit log")
    if matches:
        block_count = sum(1 for m in matches if m["entry"].get("blocked"))
        log_count = len(matches) - block_count
        print(f"  would-block (log-mode): {log_count}")
        print(f"  actually-blocked (block-mode): {block_count}")
        print("\nlast 5 samples:")
        for m in matches[-5:]:
            ts = m["entry"].get("ts", 0)
            f = m["finding"]
            print(f"  [{time.strftime('%m-%d %H:%M', time.localtime(ts))}] "
                  f"claim={f.get('claim','')!r:.60} "
                  f"missing={f.get('missing_evidence','')!r:.60}")
    return 0


def cmd_review(_args) -> int:
    entries = read_all_entries()
    files = audit_files()
    if not entries:
        print("No audit entries found.")
        print(f"Looked in: {STATE_DIR}/{AUDIT_GLOB}")
        return 0

    # Aggregate
    rule_counts: Counter = Counter()
    rule_blocks: Counter = Counter()
    rule_logs: Counter = Counter()
    rule_samples: dict[str, list[dict]] = defaultdict(list)
    mode_counts: Counter = Counter()
    total_blocks = 0
    total_logs = 0

    for e in entries:
        mode_counts[e.get("mode", "?")] += 1
        blocked = bool(e.get("blocked"))
        if blocked:
            total_blocks += 1
        else:
            total_logs += 1
        for f in e.get("findings", []):
            rule = f.get("rule", "unknown")
            rule_counts[rule] += 1
            if blocked:
                rule_blocks[rule] += 1
            else:
                rule_logs[rule] += 1
            if len(rule_samples[rule]) < 3:
                # Lexical findings use `match`; evidentiary use claim/missing_evidence.
                claim = f.get("claim") or f.get("match", "")
                rule_samples[rule].append({
                    "ts": e.get("ts", 0),
                    "claim": claim[:100],
                    "missing_evidence": f.get("missing_evidence", "")[:100],
                })

    # Header
    print("=" * 70)
    print("stop-gate audit review")
    print("=" * 70)
    print(f"sessions:        {len(files)}")
    print(f"audit entries:   {len(entries)}")
    print(f"  blocked:       {total_blocks}")
    print(f"  log-mode:      {total_logs}")
    if entries:
        first = entries[0].get("ts", 0)
        last = entries[-1].get("ts", 0)
        if first and last:
            print(f"timespan:        {(last - first)/86400.0:.2f} days "
                  f"({time.strftime('%Y-%m-%d', time.localtime(first))} "
                  f"→ {time.strftime('%Y-%m-%d', time.localtime(last))})")

    print("\n--- mode breakdown ---")
    for mode, count in sorted(mode_counts.items(), key=lambda x: -x[1]):
        print(f"  {mode:<22} {count}")

    print("\n--- rule firings ---")
    print(f"  {'rule':<32} {'total':<8} {'logged':<8} {'blocked':<8}")
    for rule, count in rule_counts.most_common():
        print(f"  {rule:<32} {count:<8} {rule_logs[rule]:<8} {rule_blocks[rule]:<8}")

    print("\n--- samples per rule (last 3) ---")
    for rule in rule_counts:
        print(f"\n[{rule}] ({rule_counts[rule]} firings)")
        for s in rule_samples[rule]:
            ts_str = time.strftime("%m-%d %H:%M", time.localtime(s["ts"]))
            print(f"  [{ts_str}] claim: {s['claim']!r}")
            print(f"             missing: {s['missing_evidence']!r}")

    # Recommendations
    print("\n" + "=" * 70)
    print("calibration signals")
    print("=" * 70)
    cfg = load_config()
    enabled_rules = {r for r, on in cfg.get("rules", {}).items() if on}
    ev_mode = cfg.get("evidentiary", "log")
    print(f"current: evidentiary={ev_mode}, enabled rules={sorted(enabled_rules)}")

    if len(entries) < 50:
        print(f"\nNot enough data yet (need ~50 entries, have {len(entries)}). Keep collecting.")
    else:
        # Find rules with the lowest firing rate — safest to enable first.
        candidates = []
        for rule, count in rule_counts.items():
            if rule in enabled_rules and ev_mode == "log":
                rate = count / len(entries)
                candidates.append((rule, count, rate))
        candidates.sort(key=lambda x: x[2])
        if candidates:
            print("\nrules ranked by firing rate (lower = safer to enable first):")
            for rule, count, rate in candidates:
                marker = "  safe" if rate < 0.1 else "  review samples first" if rate < 0.3 else "  HIGH — likely false positives"
                print(f"  {rule:<32} {count:>4} firings  {rate*100:>5.1f}% {marker}")
            print(f"\nTo flip overall mode: stop_gate_audit.py mode evidentiary block")
            print(f"To toggle a single rule: stop_gate_audit.py disable <rule>")
    return 0


def cmd_enable(args) -> int:
    cfg = load_config()
    if args.rule not in cfg["rules"]:
        print(f"unknown rule: {args.rule}", file=sys.stderr)
        print(f"valid: {sorted(cfg['rules'].keys())}", file=sys.stderr)
        return 1
    cfg["rules"][args.rule] = True
    save_config(cfg)
    print(f"enabled: {args.rule}")
    return 0


def cmd_disable(args) -> int:
    cfg = load_config()
    if args.rule not in cfg["rules"]:
        print(f"unknown rule: {args.rule}", file=sys.stderr)
        return 1
    cfg["rules"][args.rule] = False
    save_config(cfg)
    print(f"disabled: {args.rule}")
    return 0


def cmd_mode(args) -> int:
    layer = args.layer
    setting = args.setting
    if layer not in ("lexical", "evidentiary"):
        print("layer must be 'lexical' or 'evidentiary'", file=sys.stderr)
        return 1
    if setting not in ("block", "log", "off"):
        print("setting must be 'block', 'log', or 'off'", file=sys.stderr)
        return 1
    cfg = load_config()
    old = cfg.get(layer, "?")
    cfg[layer] = setting
    save_config(cfg)
    print(f"{layer}: {old} → {setting}")
    return 0


def cmd_clear(args) -> int:
    AUDIT_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archived: list[str] = []
    files = audit_files()
    keep = set()
    if args.keep_last and args.keep_last > 0:
        keep = set(p.name for p in files[-args.keep_last:])
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for p in files:
        if p.name in keep:
            continue
        dest = AUDIT_ARCHIVE_DIR / f"{stamp}-{p.name}"
        try:
            p.rename(dest)
            archived.append(p.name)
        except OSError:
            pass
    print(f"archived {len(archived)} audit log(s) to {AUDIT_ARCHIVE_DIR}")
    if keep:
        print(f"kept in place: {sorted(keep)}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("review", help="rollup + calibration signals (default)")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("count")
    sp.set_defaults(func=cmd_count)

    sp = sub.add_parser("tail")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_tail)

    sp = sub.add_parser("rule")
    sp.add_argument("rule")
    sp.set_defaults(func=cmd_rule)

    sp = sub.add_parser("enable")
    sp.add_argument("rule")
    sp.set_defaults(func=cmd_enable)

    sp = sub.add_parser("disable")
    sp.add_argument("rule")
    sp.set_defaults(func=cmd_disable)

    sp = sub.add_parser("mode")
    sp.add_argument("layer", choices=["lexical", "evidentiary"])
    sp.add_argument("setting", choices=["block", "log", "off"])
    sp.set_defaults(func=cmd_mode)

    sp = sub.add_parser("clear")
    sp.add_argument("--keep-last", type=int, default=0,
                    help="keep the N most recent audit logs in place")
    sp.set_defaults(func=cmd_clear)

    args = p.parse_args()
    if not args.cmd:
        return cmd_review(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
