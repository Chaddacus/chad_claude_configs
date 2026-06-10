#!/usr/bin/env python3
"""Prove dandori.json is faithful to the live route_manifest.json.

This is the gate that makes the contract safe to cut over to. It asserts the
embedded rules equal the live manifest on every shared field. If the manifest
changes, this FAILS until dandori.json is regenerated — no silent drift.

Exit 0 = faithful. Exit 1 = drift detected.
"""
import json
import sys
from pathlib import Path

HOME = Path("/Users/chadsimon/.claude")
MANIFEST = HOME / "state" / "route_manifest.json"
CONTRACT = HOME / "dandori" / "dandori.json"


def main():
    m = json.loads(MANIFEST.read_text())
    c = json.loads(CONTRACT.read_text())
    fails = []

    # coordinator + profiles must match verbatim
    if c["coordinator"] != m["coordinator"]:
        fails.append("coordinator mismatch")
    if c["profiles"] != m["profiles"]:
        fails.append("profiles mismatch")

    # every manifest rule must be embedded verbatim; no extra/missing routes
    manifest_rules = {r["id"]: r for r in m["rules"]}
    contract_ids = set(c["routes"].keys())
    if contract_ids != set(manifest_rules.keys()):
        fails.append(f"route set mismatch: contract={sorted(contract_ids)} manifest={sorted(manifest_rules)}")
    for rid, rule in manifest_rules.items():
        embedded = c["routes"].get(rid, {}).get("_manifest_rule")
        if embedded != rule:
            fails.append(f"{rid}: embedded rule != live manifest rule")
        # dandori-native fields must be present and sane
        d = c["routes"].get(rid, {}).get("dandori", {})
        if not isinstance(d.get("dispatch_budget_cycles"), int):
            fails.append(f"{rid}: missing dispatch_budget_cycles")
        if not d.get("prep_budget"):
            fails.append(f"{rid}: missing prep_budget")

    # postflight gate chain must match
    if c["postflight"]["gate_chain"] != m.get("postflight", {}).get("gate_chain", []):
        fails.append("postflight gate_chain mismatch")

    if fails:
        print("DRIFT DETECTED:")
        for f in fails:
            print(f"  - {f}")
        print("FAIL — regenerate dandori.json (generate_contract.py) before cutover")
        return 1
    print(f"PASS — dandori.json faithful to route_manifest {m.get('version')} "
          f"({len(manifest_rules)} routes, budgets + prep + gates added)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
