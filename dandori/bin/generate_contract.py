#!/usr/bin/env python3
"""Generate the unified dandori contract from the live route/control surfaces.

The dandori contract is ONE declarative file that today's route logic is spread
across (route_manifest.json route rules + CLAUDE.md route summary + the dispatch
budgets that currently live only in CLAUDE.md prose / CHAD_RUNTIME_INVARIANTS).

This generator EMBEDS the live route_manifest rules verbatim (so the contract is
provably faithful — see validate_contract.py) and ADDS the dandori-native fields:
per-route dispatch budget, prep weight, verification gates, and the internal/
external setup model.

Nothing reads dandori.json yet. It is an additive, reversible artifact until a
deliberate cutover repoints consumers at it.
"""
import json
from pathlib import Path

HOME = Path("/Users/chadsimon/.claude")
MANIFEST = HOME / "state" / "route_manifest.json"
OUT = HOME / "dandori" / "dandori.json"

# dandori-native additions. NOT derivable from the manifest.
# Dispatch budgets: source = CLAUDE.md "Dispatch budgets" + CHAD_RUNTIME_INVARIANTS CR-INV-008.
NATIVE = {
    "R1": {"dispatch_budget_cycles": 6, "prep_budget": "none"},
    "R2": {"dispatch_budget_cycles": 12, "prep_budget": "light"},
    "R3": {"dispatch_budget_cycles": 24, "prep_budget": "heavy"},
    "R4": {"dispatch_budget_cycles": 40, "prep_budget": "heavy"},
    "R5": {"dispatch_budget_cycles": 4, "prep_budget": "clarify_first"},
}


def main():
    m = json.loads(MANIFEST.read_text())
    postflight = m.get("postflight", {})
    pf_routes = set(postflight.get("routes", []))
    gate_chain = postflight.get("gate_chain", [])

    routes = {}
    for rule in m["rules"]:
        rid = rule["id"]
        native = NATIVE.get(rid, {})
        routes[rid] = {
            "dandori": {
                "dispatch_budget_cycles": native.get("dispatch_budget_cycles"),
                "prep_budget": native.get("prep_budget"),
                # verification gates run for this route (empty = no postflight gate chain)
                "verification_gates": list(gate_chain) if rid in pf_routes else [],
            },
            # verbatim embed of the authoritative rule — validate_contract.py asserts
            # this equals the live manifest, so the contract cannot silently drift.
            "_manifest_rule": rule,
        }

    contract = {
        "version": "dandori.v1",
        "generated_from": {
            "route_manifest_version": m.get("version"),
            "source": str(MANIFEST),
        },
        "axis": {
            "principle": "The critical path is sacred. The user waiting is the machine stopped.",
            "headline_metric": "changeover_time_seconds (prompt -> first useful action)",
            "setup_classes": {
                "internal": "runs while the user/turn is blocked (uchi-dandori / 内段取り)",
                "external": "runs off the critical path: pre-staged, speculative, or background (soto-dandori / 外段取り)",
            },
            "goal": "convert internal setup to external until changeover time is single-digit seconds, losing no governance",
        },
        "coordinator": m["coordinator"],
        "profiles": m["profiles"],
        "routes": routes,
        "postflight": {"gate_chain": gate_chain, "routes": sorted(pf_routes), "mode": postflight.get("mode")},
        "envelope_ref": "state/control_plane.json",
    }
    OUT.write_text(json.dumps(contract, indent=2) + "\n")
    print(f"wrote {OUT} ({len(routes)} routes)")


if __name__ == "__main__":
    main()
