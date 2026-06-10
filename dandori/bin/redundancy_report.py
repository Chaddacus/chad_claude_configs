#!/usr/bin/env python3
"""5S 'sort' for the policy surface: find concerns expressed in more than one
file, and name the single canonical home each should collapse to.

Read-only. Produces the evidence that drives the collapse; it does not edit any
policy file (that is the deliberate cutover step).
"""
import re
from pathlib import Path

HOME = Path("/Users/chadsimon/.claude")

FILES = {
    "CLAUDE.md": HOME / "CLAUDE.md",
    "route_manifest.json": HOME / "state" / "route_manifest.json",
    "control_plane.json": HOME / "state" / "control_plane.json",
    "CHAD_RUNTIME_INVARIANTS.md": HOME / "standards" / "CHAD_RUNTIME_INVARIANTS.md",
    "classify_prompt.py": HOME / "skills" / "govern" / "scripts" / "classify_prompt.py",
    "planner.md": HOME / "agents" / "planner.md",
    "dandori.json": HOME / "dandori" / "dandori.json",
}

CONCERNS = {
    "dispatch_budgets": {
        "patterns": [r"R2\s*=?\s*12", r"R4\s*=?\s*40", r"dispatch[_ ]budget"],
        "canonical": "dandori.json (machine-readable; was CLAUDE.md prose + invariants only)",
    },
    "route_classes_R1_R5": {
        "patterns": [r"quick_factual", r"low_risk_small_impl", r"non_trivial_impl",
                     r"high_risk_impl", r"\bR5\b.*ambiguous"],
        "canonical": "route_manifest.json / dandori.json (machine); CLAUDE.md should only reference",
    },
    "false_completion": {
        "patterns": [r"false[_ ]completion", r"unsupported closure", r"false closure"],
        "canonical": "control_plane.json (envelope); CLAUDE.md + invariants reference",
    },
    "overengineering_gate": {
        "patterns": [r"500\s*LOC", r">?\s*3\s*files", r"overengineering", r"anti-overengineering"],
        "canonical": "control_plane.json + dandori.json; CLAUDE.md references",
    },
}


def main():
    text = {name: (p.read_text() if p.exists() else "") for name, p in FILES.items()}

    print("REDUNDANCY MATRIX (regex hits per concern per file)\n")
    header = "concern".ljust(22) + "".join(n[:14].ljust(15) for n in FILES)
    print(header)
    print("-" * len(header))

    redundant = []
    for concern, spec in CONCERNS.items():
        counts = {}
        for name, body in text.items():
            c = sum(len(re.findall(p, body, re.IGNORECASE)) for p in spec["patterns"])
            counts[name] = c
        files_with = [n for n, c in counts.items() if c > 0]
        row = concern.ljust(22) + "".join((str(counts[n]) if counts[n] else ".").ljust(15) for n in FILES)
        print(row)
        if len(files_with) > 1:
            redundant.append((concern, files_with, spec["canonical"]))

    print("\nCOLLAPSE PLAN (concerns expressed in >1 file):\n")
    for concern, files_with, canonical in redundant:
        print(f"  {concern}")
        print(f"    expressed in : {', '.join(files_with)}")
        print(f"    canonical    : {canonical}")
        print(f"    action       : keep canonical authoritative; others -> one-line reference\n")

    print(f"redundancy score: {len(redundant)}/{len(CONCERNS)} concerns duplicated across files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
