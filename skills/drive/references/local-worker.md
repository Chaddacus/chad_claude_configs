# /drive --local-worker — delegate slices to goose/qwen

Loaded on demand by `skills/drive/SKILL.md` Phase 2. Read this ONLY when
`--local-worker` is set; it is inert for every other invocation.

When `--local-worker` is set, execution slices run on the local model instead of Claude. Claude remains supervisor: plan slices, write acceptance scripts, interpret results.

**Preflight (fail fast):**
```bash
curl -s --max-time 3 http://localhost:1234/v1/models | python3 -c "import sys,json; d=json.load(sys.stdin); assert any(m['id']=='daily-heavy' for m in d['data'])"
test -x ~/.claude/bin/goose_dispatch.py
```
If either fails, stop and tell the user the local worker is down.

**Slice discipline:**
- Each slice touches ≤3 files, has a written spec, has a deterministic `verify_cmd`, has an explicit files-in-scope list.
- **Write the acceptance script BEFORE dispatching.** Store at `<workspace>/.claude-gates/verify_slice_N.sh` outside the slice's `--allowed-paths`. No `|| true`, no silent catches.
- Reuse presets from `~/.claude/bin/presets/` where applicable (python-strict, frontend-visual, mcp-stdio).
- Gate calibration: test the visible output (HTTP roundtrip, CLI invocation, pixel diff), not just file contents. Ask: "would this gate pass on a broken artifact? reject a correct one?"

**Dispatch:**
```bash
python3 ~/.claude/bin/goose_dispatch.py \
  --slice-id "<track_id>-slice-N" \
  --workspace "<abs path>" \
  --spec "<spec text>" \
  --brief "<scoped brief ≤1500 tokens>" \
  --acceptance-script "<workspace>/.claude-gates/verify_slice_N.sh" \
  --files "<comma-sep file paths>" \
  --allowed-paths "<comma-sep write-allowed prefixes>" \
  --max-retries 3 --max-turns 25
```

**Outcome handling:**
- `pass` → mark slice accepted in auto_runtime, next slice.
- `fail` → Claude takes the slice in-session (supervisor is stronger than worker).
- `escalate` → sandbox violation; stop, read evidence, report to user.
- `infra_down` (exit 4) → do not count against goose; pause and re-dispatch when upstream is back.
- `gate_cheat_suspected` (exit 5) → tests contain `except: pass` / `assert True`; rewrite tests or ask user. Never accept as pass without review.

**Escalation budget:** 3 supervisor-taken slices per track. If exceeded, pause and ask the user whether to switch remaining slices back to Claude-native execution.

**Before track complete:** run the Phase 3.5 user-journey smoke — `page.goto("/") + click + screenshot` for UI, CLI happy-path for tools, `curl` sequence for APIs. Green unit tests ≠ working product.

Files this flag uses: `~/.claude/bin/goose_dispatch.py`, `~/.goosehints`, `~/.config/goose/skills/*.md`, `~/.claude/state/goose_dispatch/<slice_id>.log`.
