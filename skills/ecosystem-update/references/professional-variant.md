# /ecosystem-update --professional — public / chadacus.dev variant

Loaded on demand by `skills/ecosystem-update/SKILL.md`. Read this ONLY when
the user asks for the professional, public, chadacus, resume, or "run it for
the site" variant. Inert for a standard run.

When the user asks for the **professional**, **public**, or **chadacus** variant — or says "run it for the site" / "the resume version" — they mean: generate the standard report, then run it through the chadacus.dev scrubber/renderer to produce the public-facing version.

The professional variant strips Chad-internal language ("this is how it would apply to chad", named agents, internal paths, slash-commands, harness-specific notes) and outputs a neutral "here's today's updates" digest suitable for the public site, resume material, or a Zoom-channel summary.

### Pipeline

1. **Generate the standard report** — run Steps 1–7 above to produce `~/.claude/reports/ecosystem/{YYYY-MM-DD}.md`. Skip Step 8 (auto-implement) on professional runs unless explicitly asked — public output is the goal, not local config changes.
2. **Render the public version** — invoke the chadacus.dev renderer:
   ```bash
   python3 /Users/chadsimon/chad_personal/chadacus.dev/scripts/render_ecosystem.py
   ```
   This reads every dated md report under `~/.claude/reports/ecosystem/`, scrubs Chad-perspective phrases via `scripts/parse_findings.py` (STRONG markers drop the whole clause, WEAK markers strip phrases inline), and writes:
   - `chadacus.dev/public/ecosystem-update/YYYY-MM-DD/index.html` (public)
   - `chadacus.dev/public/ecosystem-update/YYYY-MM-DD/internal/index.html` (Chad-POV mirror)
   - `chadacus.dev/public/ecosystem-update/latest/` (symlink-style)
   - `chadacus.dev/public/ecosystem-update/index.html` (rolled-up index)
3. **Deploy (optional)** — `bash /Users/chadsimon/chad_personal/chadacus.dev/scripts/deploy.sh` rsyncs to the Linode VPS. Only run if the user asks to publish.
4. **Summary output** — when the user wants a Zoom-channel summary, derive it from the structured findings in the public render (TL;DR + Quick Wins/Build Queue titles + source links). No Chad-internal markers.

### Scrubber surface

The scrubbing logic lives in `chadacus.dev/scripts/parse_findings.py`. STRONG markers (`CHAD_STRONG_MARKERS`) drop entire sentences; WEAK markers neutralize phrases. When the user adds a new internal-only term, update that file — not this skill.

### Daily cron

`chadacus.dev/scripts/daily_runner.sh` is the cron-installed wrapper that runs the skill via `claude --print` and then calls `deploy.sh`. It is the production loop for the public site. Manual `--professional` invocations should match its behavior: run skill → render → (optionally) deploy.
