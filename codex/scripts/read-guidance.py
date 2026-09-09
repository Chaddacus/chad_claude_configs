"""Emit exact, source-addressable guidance sections for compact agent handoffs."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re

GUIDANCE = Path(__file__).resolve().parents[1] / "guidance"


def sections(text: str) -> tuple[list[str], list[tuple[str, int, int]]]:
    """Locate level-two sections while ignoring headings inside fenced code."""
    lines = text.splitlines(keepends=True)
    starts = []
    fence = None
    for index, line in enumerate(lines):
        match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif (marker[0] == fence[0] and len(marker) >= len(fence)
                  and not line[match.end():].strip()):
                fence = None
            continue
        if fence is None and line.startswith("## "):
            starts.append((line[3:].strip(), index))
    return lines, [(name, start, starts[i + 1][1] if i + 1 < len(starts) else len(lines))
                   for i, (name, start) in enumerate(starts)]


def render(selectors: list[str], root: Path = GUIDANCE) -> str:
    """Resolve every requested section before emitting any text; fail on ambiguity."""
    grouped: dict[str, list[str]] = {}
    for selector in selectors:
        guide, separator, heading = selector.partition(":")
        if not separator or not heading or not re.fullmatch(r"[a-z][a-z-]*", guide):
            raise ValueError(f"Use guide:Exact section heading, received {selector!r}")
        grouped.setdefault(guide, []).append(heading)
    output = []
    for guide, wanted in grouped.items():
        source = root / f"{guide}.md"
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Unknown or non-regular guide: {guide}")
        raw = source.read_bytes()
        lines, spans = sections(raw.decode("utf-8"))
        selected = []
        for heading in dict.fromkeys(wanted):
            matches = [(start, end) for name, start, end in spans if name == heading]
            if len(matches) != 1:
                raise ValueError(f"Expected one section {heading!r} in {guide}; found {len(matches)}")
            selected.extend(matches)
        # Keep the document introduction so section selection cannot drop its general rules.
        if spans and spans[0][1]:
            selected.append((0, spans[0][1]))
        output.append(f"Source: {source.resolve()}\nSHA-256: {hashlib.sha256(raw).hexdigest()}\n")
        for start, end in sorted(set(selected)):
            output.append(f"Lines {start + 1}-{end}:\n" + "".join(lines[start:end]))
    return "\n".join(output)


def main() -> None:
    """Print a compact heading index or requested excerpts for the local guide tree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selectors", nargs="*", help="guide:Exact section heading")
    parser.add_argument("--list", action="store_true", help="List headings without guide bodies")
    args = parser.parse_args()
    if args.list:
        if args.selectors:
            parser.error("--list does not accept section selectors")
        for source in sorted(GUIDANCE.glob("*.md")):
            _, spans = sections(source.read_text())
            for heading, _, _ in spans:
                print(f"{source.stem}:{heading}")
        return
    if not args.selectors:
        parser.error("Supply at least one section selector, or use --list")
    try:
        result = render(args.selectors)
    except (ValueError, OSError, UnicodeError) as error:
        parser.error(str(error))
    print(result, end="" if result.endswith("\n") else "\n")


if __name__ == "__main__":
    main()
