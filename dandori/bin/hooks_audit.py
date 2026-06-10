#!/usr/bin/env python3
"""Audit the live settings.json hooks against the dandori classification.

Two jobs:
  1. DRIFT GATE — every live hook must have a classification. An unclassified
     live hook fails the audit (exit 1). This guarantees the registry stays a
     complete, faithful view of the wiring before any cutover.
  2. REPORT — the internal/external split and, crucially, the convertible
     internal worklist: the critical-path time we could move off-path (SMED).

Read-only. Does not modify settings.json.
"""
import json
import sys
from pathlib import Path

HOME = Path("/Users/chadsimon/.claude")
SETTINGS = HOME / "settings.json"
REGISTRY = HOME / "dandori" / "hooks.json"


def live_hooks():
    s = json.loads(SETTINGS.read_text())
    out = []
    for event, groups in s.get("hooks", {}).items():
        for g in groups:
            matcher = g.get("matcher")
            for h in g.get("hooks", []):
                out.append({"event": event, "matcher": matcher,
                            "command": h.get("command", ""), "timeout": h.get("timeout", 0)})
    return out


def classify(command, rules):
    for r in rules:
        if r["key"] in command:
            return r
    return None


def main():
    rules = json.loads(REGISTRY.read_text())["classification"]
    hooks = live_hooks()

    unclassified = []
    internal, external = [], []
    for h in hooks:
        r = classify(h["command"], rules)
        if r is None:
            unclassified.append(h)
            continue
        h["setup"] = r["setup"]
        h["convertible"] = r["convertible"]
        h["note"] = r["note"]
        (internal if r["setup"] == "internal" else external).append(h)

    print(f"live hooks: {len(hooks)}  |  internal: {len(internal)}  external: {len(external)}")

    conv = [h for h in internal if h["convertible"]]
    irr = [h for h in internal if not h["convertible"]]
    conv_time = sum(h["timeout"] for h in conv)
    print("\nINTERNAL — convertible (the SMED worklist, off-loadable critical-path time):")
    for h in sorted(conv, key=lambda x: -x["timeout"]):
        print(f"  [{h['timeout']:>2}s] {h['event']:<16} {h['command'].split('/')[-1][:48]:<48} -> {h['note']}")
    print(f"  convertible internal timeout budget: {conv_time}s")
    print("\nINTERNAL — irreducible (must stay synchronous):")
    for h in irr:
        print(f"  [{h['timeout']:>2}s] {h['event']:<16} {h['command'].split('/')[-1][:48]}")

    if unclassified:
        print(f"\nDRIFT: {len(unclassified)} live hook(s) with no classification:")
        for h in unclassified:
            print(f"  - {h['event']} :: {h['command']}")
        print("FAIL — add them to hooks.json")
        return 1
    print(f"\nPASS — all {len(hooks)} live hooks classified; "
          f"{conv_time}s of internal setup is convertible to external")
    return 0


if __name__ == "__main__":
    sys.exit(main())
