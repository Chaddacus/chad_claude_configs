#!/usr/bin/env python3
"""web_search.py — Bash-callable web SEARCH for agents that lack the native WebSearch tool.

Why this exists: in experimental-teams mode Claude Code provisions teammates a fixed base
toolset (Read/Write/Edit/Bash/SendMessage) and strips WebSearch/WebFetch regardless of the
agent definition (verified 2026-07-13). Every teammate DOES have Bash, so this helper gives
them search universally:  python3 ~/.claude/bin/web_search.py "<query>"

It queries the engines that actually return scrapeable results to a scripted client
(search.brave.com, startpage.com — lite.duckduckgo/bing/ecosia bot-challenge and were the
round-1 failure), extracts result title+URL pairs, and is guarded by the SAME circuit
breaker as the native tool (web_budget.py): per-session budget + consecutive-failure trip.

Discipline (deep-research skill): this DISCOVERS candidate URLs. VERIFY each by curling it
and reading the real page before citing — a search snippet is not a source.

Usage:
  web_search.py "<query>"            top results as "N. title / url"
  web_search.py -n 15 "<query>"      cap result count
  web_search.py --raw "<query>"      dump raw HTML of the first engine that answered
Exit: 0 results found; 3 blocked by the breaker; 4 all engines failed (recorded as a
failure -> feeds the circuit trip).
"""
import html
import os
import re
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_HOME", os.path.expanduser("~/.claude")), "bin"))
import web_budget  # shared budget/circuit policy (same state as the native-tool breaker)

# Browser-ish UA — a default python-urllib UA gets bot-walled immediately.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

# Engines in preference order; {q} is the URL-encoded query. Ordered by observed
# scriptable-friendliness on 2026-07-13.
ENGINES = [
    ("brave", "https://search.brave.com/search?q={q}"),
    ("startpage", "https://www.startpage.com/sp/search?query={q}"),
]

_ANCHOR = re.compile(r'<a\s[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
# Engine-internal / junk hosts to drop from results.
_JUNK = re.compile(r"(brave\.com|startpage\.com|search\.marginalia|/settings|/preferences"
                   r"|javascript:|duckduckgo\.com|google\.com/search|bing\.com/search)", re.IGNORECASE)


def _fetch(url):
    """GET a URL with a browser UA; return HTML text or None on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status != 200:
                return None
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _extract(page, limit):
    """Pull (title, url) result pairs from a search-results page. Generic anchor scrape +
    junk filtering + dedupe — engine-agnostic so a markup change degrades, not breaks."""
    seen, out = set(), []
    for url, inner in _ANCHOR.findall(page):
        url = html.unescape(url)  # &amp; -> & so the URL curls correctly downstream
        if _JUNK.search(url):
            continue
        title = html.unescape(_TAGS.sub("", inner)).strip()
        if len(title) < 3:
            continue
        # Drop anchors whose visible text is inlined CSS/JS junk, not a real title.
        if "{" in title or "css-" in title or "display:" in title or ";}" in title:
            continue
        key = url.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append((title, url))
        if len(out) >= limit:
            break
    return out


def main():
    args = sys.argv[1:]
    raw = "--raw" in args
    args = [a for a in args if a != "--raw"]
    limit = 10
    if "-n" in args:
        i = args.index("-n")
        try:
            limit = int(args[i + 1]); del args[i:i + 2]
        except Exception:
            pass
    query = " ".join(args).strip()
    if not query:
        print("usage: web_search.py [-n N] [--raw] \"<query>\"", file=sys.stderr)
        sys.exit(2)

    sid = web_budget.resolve_sid()
    allowed, reason = web_budget.check(sid)
    if not allowed:
        print(f"🛑 {reason}", file=sys.stderr)
        sys.exit(3)

    q = urllib.parse.quote_plus(query)
    for name, tmpl in ENGINES:
        page = _fetch(tmpl.format(q=q))
        if not page:
            continue
        if raw:
            web_budget.record(sid, ok=True)
            print(f"[engine: {name}] raw HTML ({len(page)} bytes):\n{page[:20000]}")
            sys.exit(0)
        results = _extract(page, limit)
        if results:
            web_budget.record(sid, ok=True)
            print(f"[web_search via {name}] {len(results)} results for: {query}\n")
            for i, (title, url) in enumerate(results, 1):
                print(f"{i}. {title}\n   {url}")
            print("\n(Discover only — curl each URL and read the real page before citing.)")
            sys.exit(0)

    # Every engine failed or yielded nothing — feed the failure trip.
    web_budget.record(sid, ok=False)
    print("🛑 web_search: no engine returned usable results (bot-wall or markup change). "
          "Try a different query, curl a known URL directly, or escalate — do not loop.",
          file=sys.stderr)
    sys.exit(4)


if __name__ == "__main__":
    main()
