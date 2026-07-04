---
policy_doc_kind: best_practices_reference
classification: reference
canonical_owner: self
authority_level: informational
in_verifier_scope: false
---

# Claude Code Best Practices (External, Cited)

This is a **neutral, externally-sourced facts base** on Claude Code best practices,
compiled from Anthropic's official docs and engineering posts, plus the creator's
own published workflow notes. It does not editorialize about Chad's setup and does
not fill the conformance column — that is a separate audit's job. Every claim below
carries a verification tier per the `deep-research` skill discipline:

- `(curl)` — the full page was fetched with `curl` and the supporting text was read.
- `[snippet]` — only a search-result snippet confirmed it; weaker, flagged inline.
- `[UNVERIFIED]` — could not be confirmed against a fetched source; either dropped or explicitly flagged.

**Update mechanism**: this doc is meant to be *updated*, not re-researched from scratch.
Before adding new claims, check the Update Log below for what's already been checked
and when. Re-fetch a URL only if the "date checked" is stale relative to your need, or
add a new row for a new source.

## Update Log

| Date checked | Source URL | Version / last-modified indicator | What changed |
|---|---|---|---|
| 2026-07-04 | https://code.claude.com/docs/en/best-practices | live docs, no version string exposed; content references Claude Code v2.1.x features (auto mode, `/goal`, Ultraplan) | Initial fetch. Confirmed this is the current canonical home of Boris Cherny's "Claude Code best practices" post — `anthropic.com/engineering/claude-code-best-practices` 301s here. |
| 2026-07-04 | https://code.claude.com/docs/en/memory | live docs, references v2.1.59+ (auto memory), v2.1.198+ (symlink path matching) | Initial fetch. CLAUDE.md vs auto memory model, load order, project rules directory. |
| 2026-07-04 | https://code.claude.com/docs/en/settings | live docs, references up to v2.1.200+ | Initial fetch. Scopes, precedence, permission/sandbox/plugin settings tables (very large reference; only load-bearing rows pulled below — re-fetch and re-read in full if a specific settings key is needed). |
| 2026-07-04 | https://code.claude.com/docs/en/permission-modes | live docs, references up to v2.1.200 | Initial fetch. Full mode table, auto-mode classifier behavior, protected paths. |
| 2026-07-04 | https://code.claude.com/docs/en/sandboxing | live docs, references up to v2.1.199+ | Initial fetch. Sandbox mechanics, credential masking, limitations. |
| 2026-07-04 | https://code.claude.com/docs/en/hooks-guide | live docs | Initial fetch. Practical hook setup + limitations section. |
| 2026-07-04 | https://code.claude.com/docs/en/hooks | live docs (reference page; very large — only lifecycle overview + security section read in full) | Initial fetch. Event list, security best practices for hook scripts. |
| 2026-07-04 | https://code.claude.com/docs/en/sub-agents | live docs, references up to v2.1.199+ | Initial fetch. Built-in subagents, scope/priority, common patterns, forks. |
| 2026-07-04 | https://code.claude.com/docs/en/worktrees | live docs, references up to v2.1.200 | Initial fetch. `--worktree` flag, worktree-include file, subagent isolation, cleanup rules. |
| 2026-07-04 | https://code.claude.com/docs/en/mcp | live docs (very large reference; scopes + output-limits sections read in full) | Initial fetch. Installation scopes, precedence, output-token limits. |
| 2026-07-04 | https://code.claude.com/docs/en/skills | live docs (first ~200 lines read) | Initial fetch. Skill vs CLAUDE.md vs subagent framing, skill-file structure. |
| 2026-07-04 | https://code.claude.com/docs/en/plugins | live docs (first ~180 lines read) | Initial fetch. Standalone-vs-plugin decision table. |
| 2026-07-04 | https://code.claude.com/docs/en/headless | live docs, full page read | Initial fetch. `-p`/`--bare` flags, structured output, background-task behavior. |
| 2026-07-04 | https://code.claude.com/docs/en/checkpointing | live docs, full page read | Initial fetch. Rewind command, restore vs. summarize, limitations. |
| 2026-07-04 | https://code.claude.com/docs/en/context-window | live docs, full page read | Initial fetch. What survives compaction, context-fill mitigation. |
| 2026-07-04 | https://code.claude.com/docs/en/common-workflows | live docs (only "Work with tests" section read in full; **pending next update**: full recipe set — bug-fix and PR-creation recipes) | Initial partial fetch. |
| 2026-07-04 | https://code.claude.com/docs/en/costs | fetched to disk, **not yet read/incorporated — pending next update** | Fetched only; no claims drawn from it in this revision. |
| 2026-07-04 | https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk (redirects to https://claude.com/blog/building-agents-with-the-claude-agent-sdk) | dated Sep 29, 2025 on page | Initial fetch, full read. |
| 2026-07-04 | https://www.anthropic.com/engineering/writing-tools-for-agents | dated Sep 11, 2025 on page | Initial fetch, full read. |
| 2026-07-04 | https://www.anthropic.com/engineering/multi-agent-research-system | dated Jun 13, 2025 on page | Initial fetch, full read. |
| 2026-07-04 | https://claude.com/blog/how-anthropic-teams-use-claude-code | dated Jul 24, 2025 on page | Initial fetch, full read. |
| 2026-07-04 | https://twitter-thread.com/t/2007179832300581177 (mirror of a Boris Cherny thread, posted "Jan 2") | third-party Thread Reader mirror, not X.com itself | Initial fetch, full read. Boris Cherny's personal setup (15 tweets). |
| 2026-07-04 | https://twitter-thread.com/t/2017742741636321619 (mirror of a Boris Cherny thread, posted "Jan 31") | third-party Thread Reader mirror, not X.com itself | Initial fetch, full read. Team tips sourced from the Claude Code team (12 tweets). |

---

## 1. CLAUDE.md and memory

- CLAUDE.md is a persistent-instructions file Claude reads at the start of every session; run `/init` to generate a starter from the codebase (build system, test framework, conventions detected automatically). If a CLAUDE.md already exists, `/init` suggests improvements rather than overwriting it (curl — memory, best-practices).
- **Two complementary memory systems**, both loaded every session: CLAUDE.md (you write it — instructions/rules) and **auto memory** (Claude writes it — learnings/patterns it discovers from your corrections). Both are context, not enforced config — to hard-block an action regardless of what Claude decides, use a `PreToolUse` hook instead (curl — memory).
- Auto memory requires Claude Code v2.1.59+, is on by default (toggle via `/memory` or an `autoMemoryEnabled: false` setting), and stores in a per-project memory directory under Claude Code's config home, keyed by an index file named `MEMORY.md`. Only the **first 200 lines or 25KB** of that index (whichever comes first) load at session start; topic files load on demand. Auto memory is shared across all worktrees/subdirectories of the same git repo but is machine-local (curl — memory).
- **Where CLAUDE.md files live**, in load order (broadest → narrowest, so project instructions land in context *after* user instructions): managed policy (org-wide, IT-deployed) → the user's personal Claude Code config home (all projects) → `./CLAUDE.md` or `./.claude/CLAUDE.md` (project, shared via git) → `./CLAUDE.local.md` (personal, gitignored). Claude Code walks up the directory tree from cwd loading every CLAUDE.md/CLAUDE.local.md it finds; nested (child-directory) CLAUDE.md files load lazily only when Claude reads a file in that subdirectory (curl — memory).
- **Size discipline**: target **under 200 lines** per CLAUDE.md file — longer files consume more context *and measurably reduce instruction adherence*. "For each line, ask: would removing this cause Claude to make a mistake? If not, cut it." Bloated CLAUDE.md causes Claude to ignore real instructions (curl — best-practices, memory).
- Include: bash commands Claude can't guess, code-style rules that differ from language defaults, testing/test-runner preferences, repo etiquette (branch naming, PR conventions), project-specific architectural decisions, env-var quirks, non-obvious gotchas. Exclude: anything Claude can discover by reading code, standard language conventions, detailed API docs (link instead), frequently-changing info, tutorials/long explanations, file-by-file descriptions, "write clean code"-style platitudes (curl — best-practices).
- For domain knowledge or multi-step procedures that are only *sometimes* relevant, use **skills** instead of CLAUDE.md — skills load on demand and cost near-nothing until invoked, vs. CLAUDE.md which is loaded in full every session regardless of length (curl — memory, best-practices, skills).
- For large projects, split into a per-project rules directory (`.claude/rules/*.md`), each covering one topic; rules can carry a `paths:` frontmatter glob so they load only when Claude touches matching files (reduces context noise vs. always-loaded CLAUDE.md content). Rules support symlinks (share across projects) and a user-level rules directory tier for personal, cross-project preferences (curl — memory).
- **CLAUDE.md imports**: an `@path/to/file` syntax expands and loads the target inline at launch (max 4 hops of recursive imports); wrap a path in backticks to *mention* it without importing. A repository's `AGENTS.md` is not read natively by Claude Code — import it via an `@AGENTS.md` reference or a symlink from `CLAUDE.md` to `AGENTS.md` so both tools share one file (curl — memory).
- **What survives compaction**: project-root CLAUDE.md and unscoped rules are re-injected from disk; auto memory is re-injected from disk; path-scoped rules and nested (subdirectory) CLAUDE.md are **lost** until the triggering file is read again; invoked skill bodies are re-injected but capped (5,000 tokens/skill, 25,000 total, oldest dropped first, truncation keeps the *start* of the file — so put critical instructions near the top of a skill file) (curl — context-window).
- If Claude keeps making a mistake despite a rule against it, the file is probably too long and the rule is getting lost — prune ruthlessly rather than adding more rules. Emphasis markers ("IMPORTANT", "YOU MUST") can improve adherence. Review CLAUDE.md like code: prune when things go wrong, verify behavior actually shifts (curl — best-practices, memory).
- **Debug loading**: run `/memory` to see exactly which CLAUDE.md/CLAUDE.local.md/rules files are loaded in the current session; if a file isn't listed, Claude can't see it. An `InstructionsLoaded` hook can log exactly which instruction files load, when, and why — useful for path-scoped/lazy-load debugging (curl — memory).

## 2. Settings, scopes, and permissions

- Four settings scopes, in precedence order (highest → lowest): **Managed** (server/MDM-deployed, cannot be overridden by anything including CLI args) → **CLI args** (session-only) → **Local** (personal, gitignored project-local settings file) → **Project** (shared via git) → **User** (applies to you across all projects). Scalars from higher scopes override; arrays generally concatenate/merge across scopes — permission rules specifically *merge* rather than override (curl — settings).
- Settings files are watched and hot-reloaded (permissions, hooks, credential helpers apply without restart); `model` and `outputStyle` require `/model` or a restart/`/clear` respectively (curl — settings).
- **Managed settings parse tolerantly**: an invalid entry is stripped with a warning rather than invalidating the whole policy (a typo can't disable your org's entire security config) — this tolerance is unique to managed settings; user/project/local settings files are rejected wholesale on a validation failure (curl — settings).
- Use a `permissions.deny` rule set to exclude sensitive files from all access (reads, file discovery, search): e.g. denying reads on `.env`, `.env.*`, and a `secrets/` directory (curl — settings).
- **Permission rule evaluation order is deny → ask → allow**, first match wins regardless of specificity (curl — settings).

### Permission modes

Six modes trade oversight for throughput (curl — permission-modes):

| Mode | Runs without asking | Best for |
|---|---|---|
| `default` (labeled **Manual** in CLI/IDE) | Reads only | Getting started, sensitive work |
| `acceptEdits` | Reads, file edits, common filesystem Bash (`mkdir`, `touch`, `mv`, `cp`, etc.) inside working dir | Iterating on reviewed code |
| `plan` | Reads only | Exploring before changing |
| `auto` | Everything, with a background classifier reviewing actions | Long tasks, reducing prompt fatigue |
| `dontAsk` | Only pre-approved (`permissions.allow`) tools + read-only Bash | Locked-down CI/scripts |
| `bypassPermissions` | Everything, no safety checks | Isolated containers/VMs **only** |

- **Auto mode** (requires v2.1.83+, Opus 4.6+/Sonnet 4.6+ on the Anthropic API) routes actions through a separate classifier model that blocks scope escalation, unrecognized infrastructure, and hostile-content-driven actions — e.g. piping a downloaded script into a shell, force-push/push-to-main, hard resets that discard uncommitted work, production deploys/migrations, mass cloud-storage deletion, IAM grants, secret-manager writes, launching another unsandboxed agent loop. It allows local file ops, installing declared dependencies, reading an env file and sending its contents to the matching API, read-only HTTP, pushing to your own branch. A boundary you state in conversation ("don't push") is treated as a hard block signal by the classifier until you lift it — but boundaries are re-read from the transcript each check, so **compaction can silently drop a stated boundary**; use a deny rule for a hard guarantee instead. If the classifier blocks 3x consecutive or 20x total in a session, auto mode pauses and reverts to prompting; in `-p` (non-interactive) mode, repeated blocks **abort the run** since there's no user to fall back to (curl — permission-modes).
- Auto mode also checks subagents at three points: task description at spawn time (blocks dangerous-looking delegated tasks before they start), each action during the run, and a full-history review when the subagent finishes (a flagged concern prepends a security warning to the subagent's returned results) (curl — permission-modes).
- **Protected paths** (version-control internals, editor/IDE config directories, the Claude Code project config directory itself, and shell/package-manager rc-files) are *never auto-approved* in any mode except `bypassPermissions` — even an explicit allow rule for a protected path does not bypass the per-mode prompt/route (curl — permission-modes).
- `bypassPermissions` (the "dangerously skip permissions" flag) is explicitly scoped to **isolated environments only** (containers/VMs without internet access) — "offers no protection against prompt injection or unintended actions." Refuses to start as root/sudo on Linux/macOS outside a recognized sandbox (curl — permission-modes).

### Sandboxing (Bash tool isolation)

- The Bash sandbox is OS-level isolation (macOS Seatbelt; Linux/WSL2 bubblewrap) that restricts filesystem and network access for Bash commands and their children — orthogonal to permission modes, which gate *whether a tool call runs at all*. Default: read/write only in the working directory + session temp dir; read access to the rest of the filesystem *except* denied paths (note: this **still allows reading credential files** in a user's home directory by default — you must add explicit sandbox credential-deny entries to close that gap) (curl — sandboxing).
- Network: no domains pre-allowed by default; first use of a new domain prompts. An allowed-domains list pre-approves; a TLS-termination setting (experimental) lets the built-in proxy terminate TLS itself, required for masked credential substitution — **without it, the sandbox's default proxy does not inspect TLS content**, meaning domain-fronting-style exfiltration through an over-broad allowed domain (e.g. a bare top-level domain) is a known limitation, explicitly called out in the docs (curl — sandboxing).
- Two sandbox operating modes: **auto-allow** (sandboxed Bash commands run without prompting — commands that can't be sandboxed fall back to normal permission flow) and **regular permissions** (all Bash still prompts even when sandboxed) (curl — sandboxing).
- Effective sandboxing requires **both** filesystem and network isolation together — "without network isolation, a compromised agent could exfiltrate sensitive files like SSH keys; without filesystem isolation, a compromised agent could backdoor system resources to gain network access" (curl — sandboxing).
- Enforce org-wide via managed settings that turn sandboxing on, fail closed if it can't start, and disable the unsandboxed-retry escape hatch (curl — sandboxing).

## 3. Hooks

- Hooks are **deterministic** — shell commands (or HTTP/prompt/agent-based handlers) that fire at fixed lifecycle points, unlike CLAUDE.md which is advisory. "Use hooks for actions that must happen every time with zero exceptions" (curl — best-practices, hooks-guide).
- Key events: `SessionStart`/`SessionEnd` (once/session), `UserPromptSubmit`/`Stop`/`StopFailure` (once/turn), `PreToolUse`/`PostToolUse` (every tool call). Also: `PermissionRequest`, `PermissionDenied` (auto-mode denial, supports a retry response), `SubagentStart`/`SubagentStop`, `PreCompact`/`PostCompact`, `WorktreeCreate`/`WorktreeRemove`, `FileChanged`, `ConfigChange`, `InstructionsLoaded` (curl — hooks).
- `PreToolUse` fires **before any permission-mode check** — a hook returning a deny decision blocks the tool even in `bypassPermissions` mode. The reverse doesn't hold: a hook allow cannot override a settings-level deny rule. Hooks can only tighten, never loosen, past what permission rules already allow (curl — hooks-guide).
- `PermissionRequest` hooks **do not fire in `-p` (non-interactive) mode** — use `PreToolUse` for automated permission decisions in scripts/CI (curl — hooks-guide).
- Security best practices for hook scripts (curl — hooks): validate/sanitize all input, never trust it blindly; always quote shell variables; block path traversal (check for `..`); use absolute paths (project-directory environment variable in exec form); skip sensitive files (env files, version-control internals, keys). **Disclaimer**: "Command hooks run with your system user's full permissions" — review and test every hook before adding it.
- Prompt-based and agent-based hooks exist for decisions requiring judgment rather than deterministic rules — e.g., routing permission requests through a model to auto-approve benign ones (curl — hooks-guide; corroborated by Boris Cherny team tip #8c below).
- Practical patterns from the guide: a post-edit hook piped to a formatter (auto-format after every edit); a notification hook for desktop alerts when Claude needs input; re-inject context after compaction via a post-compact hook; block edits to protected files via a pre-tool-use hook (curl — hooks-guide).
- A `Stop` hook can act as a deterministic completion gate: it re-runs your check script and blocks the turn ending until it passes — but **Claude Code overrides the hook and force-ends the turn after 8 consecutive blocks**, so a Stop-hook gate is not an infinite loop guarantee (curl — best-practices).

## 4. Subagents and multi-agent workflows

- Use a subagent when a side task would flood the main conversation with search results/logs/file contents you won't reference again — the subagent works in its own context window and returns only a summary (curl — sub-agents, best-practices).
- **Built-in subagents**: `Explore` (read-only, fast, model inherits from main convo capped at Opus on the Claude API; skips CLAUDE.md and git status for speed), `Plan` (read-only research during plan mode, also skips CLAUDE.md/git status), `general-purpose` (all tools, for complex multi-step work that needs both exploration and edits) (curl — sub-agents).
- Custom subagent files live in a project-level subagents directory (shareable) or a user-level subagents directory (available in all your projects) as Markdown + YAML frontmatter (`name`, `description`, `tools`, `model`). Claude auto-delegates based on the `description` field, so **write a clear, specific description** — it's the dispatch signal (curl — sub-agents, best-practices).
- Common patterns (curl — sub-agents): **isolate high-volume operations** ("use a subagent to run the test suite and report only failing tests"); **parallel research** (spawn multiple subagents for independent investigation paths, then synthesize — "works best when research paths don't depend on each other"); **chain subagents** (one subagent's output feeds the next, e.g. code-reviewer → optimizer).
- Choose main conversation over a subagent when: the task needs frequent back-and-forth, multiple phases share heavy context (plan → implement → test), it's a quick targeted change, or latency matters (subagents start with a fresh context and take time to re-gather it). Choose a subagent when: output is verbose and disposable, you want specific tool restrictions/permissions enforced, or the work is self-contained and summarizable (curl — sub-agents).
- A non-fork subagent starts genuinely fresh: it does **not** see conversation history, previously-invoked skills, or already-read files — only its own system prompt, the delegation task message, the full CLAUDE.md/memory hierarchy (except `Explore`/`Plan`, which skip it), a git-status snapshot, and any preloaded skills. If a rule must reach a subagent (e.g. "ignore the vendor directory"), restate it explicitly in the delegation prompt (curl — sub-agents).
- **Forks** (an explicit fork command, requiring opt-in on older versions) differ from named subagents: a fork inherits the *entire* current conversation (system prompt, tools, model, message history) rather than starting fresh, reuses the parent's prompt cache (cheaper), and is best when a named subagent would need too much re-explained background (curl — sub-agents).
- Nested subagents (subagent spawning its own subagents) are supported as of v2.1.172, capped at a fixed depth of 5 levels from the main conversation, not configurable (curl — sub-agents).
- **Agent SDK framing of subagents** (curl — agent-sdk): "Subagents are useful for two main reasons. First, they enable parallelization... Second, they help manage context: subagents use their own isolated context windows, and only send relevant information back to the orchestrator." The SDK's built-in compaction feature auto-summarizes prior messages as context limit approaches.
- **Multi-agent research-system findings, generalizable to any orchestrator/subagent design** (curl — multi-agent-research-system):
  - A multi-agent system (Opus lead + Sonnet subagents) beat single-agent Opus by **90.2%** on an internal breadth-first research eval; token usage alone explained ~80% of the performance variance in a related benchmark (BrowseComp), with tool-call count and model choice explaining most of the rest.
  - The tradeoff: agents burn ~4x the tokens of a chat interaction; multi-agent systems burn ~15x. Multi-agent architectures are worth it only when the task's value justifies that spend, and only for tasks with real parallelizable structure — "most coding tasks involve fewer truly parallelizable tasks than research."
  - Prompting lessons: **teach the orchestrator how to delegate** — vague sub-task descriptions cause duplicated work or gaps; give each subagent an explicit objective, output format, tool/source guidance, and clear task boundaries. **Scale effort to query complexity** explicitly in the prompt (simple fact-finding: 1 agent, 3-10 tool calls; complex research: 10+ subagents with divided responsibilities) — agents don't self-calibrate effort well. **Start wide, then narrow** search strategy, mirroring expert human research. Parallelizing at two levels (lead spawns 3-5 subagents at once; each subagent runs 3+ tools in parallel) cut research time up to 90%.
  - Production reliability: agents are stateful and errors compound, so build durable execution that can **resume from where an agent was**, not restart from scratch; use rainbow deployments so mid-flight agents aren't broken by a code push; full production tracing (of decision patterns, not conversation contents) is what actually let them diagnose failures.
  - Appendix tips: evaluate **end-state**, not turn-by-turn process, for agents that mutate persistent state across many turns; for long-horizon conversations, have agents summarize completed phases and store essentials in external memory rather than relying on ever-larger context; have subagents write outputs to a filesystem/artifact store and pass back lightweight references rather than routing everything through the lead agent as conversation text ("minimize the game of telephone").

### Git worktrees / parallel Claudes

- A git worktree is a separate working directory sharing repo history/remote with the main checkout — isolates one Claude session's file edits from another's. A `--worktree <name>` flag creates one under a `.claude/worktrees/<name>/` directory at the repo root on a branch named after it, branching from the remote's default branch by default (or local HEAD if no remote/fetch fails; configurable to always use local HEAD) (curl — worktrees).
- Add the worktrees directory to `.gitignore`. A `.worktreeinclude` file (gitignore syntax) auto-copies gitignored files like local env files into every new worktree, since a worktree is otherwise a fresh checkout missing untracked files (curl — worktrees).
- Subagents can be isolated in their own worktrees too (a worktree-isolation frontmatter field, or asking Claude to "use worktrees for your agents") — auto-cleaned when the subagent finishes without changes (curl — worktrees, sub-agents).
- Cleanup on exit: no uncommitted changes/untracked files/new commits → worktree + branch auto-removed; otherwise Claude prompts to keep or discard. Non-interactive worktree runs are **not** auto-cleaned — remove manually with a git worktree command (curl — worktrees).
- Best-practices doc frames worktrees as one of several parallelization options, alongside the desktop app (auto-creates a worktree per session), Claude Code on the web (cloud VMs), and agent teams (automated multi-session coordination). A **Writer/Reviewer pattern** across two sessions is called out explicitly: fresh context in the reviewer session avoids bias toward code it just wrote (curl — best-practices).

## 5. Plan mode, TDD, and verification loops

- **Give Claude a way to verify its work** is presented as the single most load-bearing best practice: "Claude stops when the work looks done. Without a check it can run, 'looks done' is the only signal available, and you become the verification loop." A check can be a test suite, build exit code, linter, fixture-diff script, or browser screenshot comparison (curl — best-practices).
- Four escalating ways to gate on that check: (1) ask Claude to run-and-iterate in one prompt; (2) set it as a `/goal` condition so a separate evaluator re-checks after every turn; (3) a `Stop` hook that deterministically blocks turn-ending until the script passes (capped at 8 consecutive blocks before Claude Code force-ends the turn); (4) a fresh-context subagent "second opinion" reviewer that tries to refute the work rather than the same agent grading itself (curl — best-practices).
- **Explore → Plan → Implement → Commit**, the four-phase recommended workflow: enter plan mode (reads/answers only, no edits) → ask for a detailed plan (a key combo opens it in your editor for direct edits) → exit plan mode and implement against the plan, writing/running tests → commit with a descriptive message and open a PR. Skip planning for changes you could describe in one sentence (curl — best-practices).
- Plan mode itself: reads/runs shell commands to explore, writes a plan, makes **no** source edits; permission prompts still apply as in Manual mode. Approving a plan hands off into accept-edits/manual/auto per your choice at approval time (curl — permission-modes).
- Agent SDK's verification section frames three concrete feedback mechanisms (curl — agent-sdk): **rules-based feedback** (linting — "it is usually better to generate TypeScript and lint it than pure JavaScript because it provides multiple additional layers of feedback"); **visual feedback** (screenshot/render comparison, e.g. via a browser-automation MCP server, checking layout/styling/content-hierarchy/responsiveness); **LLM-as-judge** ("generally not a very robust method, and can have heavy latency tradeoffs, but for applications where any boost in performance is worth the cost, it can be helpful").
- Common failure pattern named explicitly: **the trust-then-verify gap** — "Claude produces a plausible-looking implementation that doesn't handle edge cases. Fix: Always provide verification (tests, scripts, screenshots). If you can't verify it, don't ship it" (curl — best-practices).
- On adversarial review: "a reviewer prompted to find gaps will usually report some, even when the work is sound, because that is what it was asked to do. Chasing every finding leads to over-engineering... Tell the reviewer to flag only gaps that affect correctness or the stated requirements, and treat the rest as optional" (curl — best-practices).

## 6. Context management (clear, compact, checkpoints)

- Context window fill is framed as *the* primary constraint on session quality — "performance degrades as it fills... Claude may start 'forgetting' earlier instructions or making more mistakes" (curl — best-practices).
- Clearing context resets it entirely between unrelated tasks; recommended after **two failed correction attempts on the same issue** rather than continuing to correct in a polluted context — "a clean session with a better prompt almost always outperforms a long session with accumulated corrections" (curl — best-practices).
- Auto-compaction triggers automatically near the limit; a compact command with instructions lets you steer what the summary keeps (e.g. "focus on the auth bug fix") instead of relying on the automatic pass's guess. You can also customize compaction behavior via a CLAUDE.md instruction like "when compacting, always preserve the full list of modified files and test commands" (curl — best-practices, context-window).
- **What survives compaction** (see also §1): system prompt/output style unchanged (not part of message history); project-root CLAUDE.md and unscoped rules re-injected from disk; auto memory re-injected from disk; path-scoped rules and nested CLAUDE.md **lost** until the triggering file is read again; invoked-skill bodies re-injected but capped and truncated (keeps file *start*); hooks unaffected (they run as code, not context) (curl — context-window).
- A rewind command (or a keyboard shortcut) opens a checkpoint menu per-prompt-sent: restore code+conversation, restore conversation only, restore code only, or **summarize from/up-to a point** — a targeted, partial alternative to whole-conversation compaction. Checkpoints persist across sessions but only track Claude's own file-edit-tool changes, **not** Bash-driven file changes (deletes/moves/copies run via Bash are untracked) or external/concurrent-session edits — explicitly "not a replacement for version control" (curl — checkpointing, best-practices).
- A lightweight aside command answers a quick question from current context with no tool access and **discards** the exchange rather than adding it to history — use it instead of a subagent for a quick side question that shouldn't grow context (curl — sub-agents).
- Named failure patterns to recognize early (curl — best-practices): "the kitchen-sink session" (unrelated tasks mixed in one context → fix: clear context between tasks); "correcting over and over" (→ fix: clear + better initial prompt after two failed corrections); "the over-specified CLAUDE.md" (too long, rules get lost → fix: ruthless pruning); "the infinite exploration" (unscoped "investigate X" fills context → fix: scope narrowly or delegate to a subagent).

## 7. MCP configuration and scopes

- Three MCP installation scopes (curl — mcp): **Local** (default; current project only, private, stored in the user's Claude Code config file under that project's path — for personal/dev servers or credentials you don't want in version control); **Project** (a project-root config file, checked into git, shared with the team — Claude Code prompts for approval before using a project-scoped server the first time, for security); **User** (available across all your projects, private to you).
- Scope precedence when the same server name appears in multiple places: Local > Project > User > plugin-provided servers > claude.ai connectors. The **entire** server entry from the winning scope is used — fields are not merged across scopes (curl — mcp).
- Prefer the **HTTP transport** for remote servers ("the recommended option... most widely supported"); SSE is deprecated (curl — mcp).
- **Output-size discipline**: Claude Code warns above 10,000 tokens of MCP tool output and caps at 25,000 tokens by default (an environment variable can raise it); a server can declare a per-tool result-size annotation to raise its own ceiling (up to 500,000 chars) for outputs that are inherently large but necessary, e.g. full schemas (curl — mcp).
- "Verify you trust each server before connecting it. Servers that fetch external content can expose you to prompt injection risk" (curl — mcp) — directly relevant given Claude Code's own auto-mode classifier treats hostile tool-result content as a manipulation vector it must screen (curl — permission-modes, cross-reference).

## 8. Skills and plugins

- A **skill** is a markdown file with YAML frontmatter (a `SKILL.md`) that Claude loads automatically when relevant (matched via the `description` field) or on explicit `/skill-name` invocation. Unlike CLAUDE.md, a skill's body loads **only when used** — long reference material costs near-nothing until invoked (curl — skills, best-practices).
- Create a skill when you keep pasting the same instructions/checklist/procedure into chat, or a CLAUDE.md section has grown into a multi-step procedure rather than a static fact (curl — skills, memory).
- Skills follow the open **Agent Skills standard** (cross-tool), extended by Claude Code with invocation control (a `disable-model-invocation` flag for workflows with side effects you want to trigger manually, not auto-invoke), subagent execution, and dynamic context injection (e.g. an inline shell-command line runs and inlines its output before Claude sees the skill body) (curl — skills, best-practices).
- **Standalone (project `.claude/` directory) vs. plugins** decision: use standalone for personal/project-specific customization and quick iteration (short `/name` commands); use plugins (self-contained dirs with a plugin manifest) when sharing with a team/community, needing the same skills/agents across multiple projects, or wanting versioned releases — plugin skills are namespaced (`/plugin-name:hello`) to avoid collisions. Recommended path: prototype standalone, convert to a plugin once ready to share (curl — plugins).
- Plugins bundle skills, hooks, subagents, and MCP servers into one installable, distributable unit (community marketplace or first-party) (curl — best-practices, plugins).

## 9. Headless mode and the Agent SDK

- `-p`/`--print` runs Claude Code non-interactively for CI, pre-commit hooks, and scripts; `--bare` skips auto-discovery of hooks/skills/plugins/MCP servers/auto memory/CLAUDE.md for deterministic, machine-independent runs — **"the recommended mode for scripted and SDK calls, and will become the default for `-p` in a future release"** (curl — headless).
- `--allowedTools` scopes exactly which tools/commands run without a prompt for a given invocation (e.g. specific `git diff`/`git commit` patterns) — critical for unattended fan-out scripts. `--output-format json|stream-json` gives structured/streaming output for programmatic consumption, including a `--json-schema` option to constrain structured output shape (curl — headless).
- Background Bash tasks started during a `-p` run are killed ~5 seconds after the final result is returned (grace period for a task that finishes right after); background subagents/workflows are exempt and are waited on, capped at 10 minutes by default (configurable via an environment variable) (curl — headless).
- Fan-out pattern for large migrations: generate a task list, loop a `-p` invocation per item with tools scoped via `--allowedTools`, test on 2-3 files before scaling to the full set, refining the prompt based on early failures (curl — best-practices).
- **Agent SDK core framing** (curl — agent-sdk): "Claude needs the same tools that programmers use every day" — giving an agent a real computer (bash, file edit, file search) is what makes it generalize beyond coding to research, data analysis, document generation, etc. The canonical agent loop is **gather context → take action → verify work → repeat**. Context-gathering options ranked: prefer **agentic search** (grep/tail-style file-system search) as the default; add semantic search (embeddings) only if you need faster results or more variation, since it's "less accurate, more difficult to maintain, and less transparent." Prefer **code generation** for tasks needing precision/composability/reuse (their file-creation feature runs entirely on Claude writing Python scripts, not templated output).
- Evaluating your own agent (curl — agent-sdk): ask, for each failure — is it missing key information (restructure the search API)? Does it fail the same task repeatedly (add a formal rule to a tool call)? Can it not self-correct (give it more creative tool options)? Does performance vary as features are added (build a representative eval set from real usage, not synthetic sandbox cases)?

## 10. Writing effective tools for agents

(curl — writing-tools-for-agents, all claims from this source unless noted)

- A tool is a contract between a deterministic system and a non-deterministic agent — this reframes tool design away from "wrap an existing API 1:1" and toward "design for how an agent will perceive and use it." A common failure: thin API wrappers that don't account for agents' limited context relative to computer memory (e.g., a bulk list-everything tool that dumps all records, forcing the agent to burn context reading linearly, vs. a targeted search tool).
- **Consolidate multi-step operations into one tool** where they're commonly chained: e.g. a single scheduling tool (finds availability + creates the event) beats separate list-users/list-events/create-event tools; a log-search tool that returns only relevant lines + context beats a raw log-dump tool.
- **Namespace tools** (by service and/or resource) when an agent has access to many overlapping tools/MCP servers — prefix vs. suffix namespacing has measurable, LLM-dependent effects; test both against your own eval.
- **Return high-signal, natural-language context**, not low-level technical identifiers — resolving opaque IDs to human-readable names measurably reduces hallucination and improves retrieval precision. Offer a concise-vs-detailed response-format option when both natural-language ergonomics and technical-ID interop are needed downstream.
- **Token-efficiency defaults**: implement pagination, range selection, filtering, and truncation with sensible defaults for any response that could balloon; Claude Code itself restricts tool responses to 25,000 tokens by default (matches §7's MCP output-limit figure). If you truncate, steer the agent explicitly toward efficient strategies (many small targeted searches vs. one broad one) — and make error responses actionable, not opaque codes/tracebacks.
- **Prompt-engineer tool descriptions like onboarding a new hire** — make implicit context (query formats, niche terminology, resource relationships) explicit; name parameters unambiguously (an explicit `user_id` field rather than a bare `user` field). Anthropic reports Claude Sonnet 3.5 reached SOTA on SWE-bench Verified after precise tool-description refinements alone, without model changes.
- **Build evals with real-world-shaped tasks**, not sandbox toys — strong eval prompts require multiple (sometimes dozens of) chained tool calls and reflect actual internal workflows; weak eval prompts are single-call lookups. Pair each with a verifiable outcome (exact-match or LLM-judged), and avoid overly strict verifiers that reject valid alternate phrasings.
- **Let agents improve their own tools** — concatenating evaluation transcripts and handing them to Claude Code to refactor tools/descriptions is reported as the source of "most of the advice in this post." A dedicated tool-testing agent that repeatedly exercises a flawed tool and rewrites its description produced a 40% decrease in downstream task-completion time.

## 11. Boris Cherny's personal workflow practices

Boris Cherny created Claude Code. These are his own published tips, fetched via a third-party Thread Reader mirror of his X/Twitter posts (direct x.com fetches returned unreadable JS shells — see Unverified section). Tagged `(curl, secondary mirror of primary source)`.

**From his personal setup thread** (curl, secondary mirror — twitter-thread.com/t/2007179832300581177):
- Runs **5 Claude sessions in parallel in the terminal** (numbered tabs 1-5, system notifications for when a session needs input), plus **5-10 more on claude.ai/code** in parallel, handing sessions back and forth between local/web/phone ("teleport" between environments). Explicitly frames this as personal preference, not prescription: "There is no one correct way to use Claude Code... Each person on the Claude Code team uses it very differently."
- Uses the largest/most capable model with thinking enabled for everything — reasoning that a bigger, slower model needing less steering and better at tool use is "almost always faster... in the end" than a smaller model.
- The team shares **one CLAUDE.md for the Claude Code repo**, checked into git, edited by the whole team multiple times a week — "anytime we see Claude do something incorrectly we add it to the CLAUDE.md so Claude knows not to do it next time." Other teams maintain their own.
- Tags the Claude Code GitHub Action bot on coworkers' PRs to propose CLAUDE.md additions as part of code review — described as their version of "Compounding Engineering."
- Starts most sessions in Plan mode (a double key-press shortcut); iterates on the plan with Claude until satisfied, *then* switches to auto-accept-edits mode where Claude can usually one-shot the implementation. "A good plan is really important!"
- Turns every daily "inner loop" workflow into a checked-in slash command (e.g. a combined commit-push-PR command used dozens of times a day, using inline bash to pre-compute git status so the model doesn't need back-and-forth).
- Uses a few standing subagents for repeat workflows: a code-simplifier (runs after Claude finishes), a verify-app agent (detailed end-to-end test instructions).
- Uses a post-edit hook to auto-format code — "Claude usually generates well-formatted code out of the box, [the hook] handles the last 10% to avoid formatting errors in CI later."
- Does **not** use the "dangerously skip permissions" flag day to day; instead pre-allows known-safe bash commands via the permissions UI, checked into project settings and shared with the team.
- Connects Slack (MCP), BigQuery (CLI), and Sentry directly so Claude can post/query/pull logs itself; the Slack MCP config is checked into the project's MCP config and shared team-wide.
- For long-running unattended tasks: either prompt Claude to self-verify with a background agent when done, use an agent `Stop` hook for a more deterministic version, or use the community "ralph-wiggum" plugin; pairs this with a locked-down non-interactive permission mode or the bypass-permissions flag **inside a sandbox** so Claude "can cook without being blocked."
- Restates the verification principle as his single most important tip: "give Claude a way to verify its work. If Claude has that feedback loop, it will 2-3x the quality of the final result." His own claude.ai/code changes are tested by Claude itself via a Chrome browser extension — opens a browser, drives the UI, iterates until it works.

**From the team-sourced tips thread** (curl, secondary mirror — twitter-thread.com/t/2017742741636321619; explicitly framed as sourced from the wider Claude Code team, not just Boris personally):
1. **Parallelism is the top-ranked tip from the team**: spin up 3-5 git worktrees at once, one Claude session per worktree. Some name worktrees with single-keystroke shell aliases; some keep a dedicated "analysis" worktree just for reading logs/running BigQuery.
2. **Plan mode for every complex task**, pouring planning energy in so implementation one-shots. One pattern cited: have one Claude write the plan, spin up a second Claude to review it "as a staff engineer." Another: the moment something goes sideways mid-implementation, switch back to plan mode and re-plan rather than continuing to push forward.
3. **CLAUDE.md as a living, self-correcting artifact** — after every correction, explicitly tell Claude to update its CLAUDE.md so it doesn't make that mistake again, and iterate until the mistake rate measurably drops. One engineer maintains a notes directory per task/project (updated after every PR) and points CLAUDE.md at it.
4. **Turn anything done more than once a day into a skill/command**, checked into git and reused across projects. Team examples cited: a tech-debt-finder command run at end of every session to find/kill duplicated code; a command that syncs several days of Slack/GDrive/Asana/GitHub into one context dump; analytics-engineer-style agents that write and test dbt models.
5. Bug-fixing pattern: paste a Slack bug thread (via Slack MCP) and just say "fix" — "don't micromanage how." Point Claude at container logs directly for distributed-systems troubleshooting.
6. Prompting escalation techniques cited from the team: "Grill me on these changes and don't make a PR until I pass your test" (make Claude adversarially review itself); "Prove to me this works" (diff behavior between main and feature branch); after a mediocre fix, "knowing everything you know now, scrap this and implement the elegant solution."
7. Terminal/environment tips: a custom status line configured to show context usage + current git branch for easier session-juggling; color-coded/named terminal tabs, one per task/worktree; voice dictation ("you speak 3x faster than you type, and your prompts get way more detailed as a result").
8. Subagent tips: append "use subagents" to throw more compute at a problem; offload individual tasks to subagents to keep the main agent's context window clean; route permission-request decisions through a model via hook to auto-approve safe ones after scanning for attacks.
9. Data/analytics: ask Claude to use a CLI tool to pull and analyze metrics live rather than writing SQL by hand — "this works for any database that has a CLI, MCP, or API."
10. Learning-oriented uses: enable an explanatory/learning output style to have Claude explain the *why*; have Claude generate an HTML slide deck explaining unfamiliar code; ask for ASCII diagrams of new protocols/codebases; a spaced-repetition skill where you explain your understanding and Claude asks follow-ups to fill gaps.

---

## Sources

**Tier `(curl)` — full page fetched and read:**

- https://code.claude.com/docs/en/best-practices
- https://code.claude.com/docs/en/memory
- https://code.claude.com/docs/en/settings
- https://code.claude.com/docs/en/permission-modes
- https://code.claude.com/docs/en/sandboxing
- https://code.claude.com/docs/en/hooks-guide
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/sub-agents
- https://code.claude.com/docs/en/worktrees
- https://code.claude.com/docs/en/mcp
- https://code.claude.com/docs/en/skills
- https://code.claude.com/docs/en/plugins
- https://code.claude.com/docs/en/headless
- https://code.claude.com/docs/en/checkpointing
- https://code.claude.com/docs/en/context-window
- https://code.claude.com/docs/en/common-workflows (partial — see Update Log)
- https://www.anthropic.com/engineering/building-agents-with-the-claude-agent-sdk (redirects to claude.com/blog/building-agents-with-the-claude-agent-sdk)
- https://www.anthropic.com/engineering/writing-tools-for-agents
- https://www.anthropic.com/engineering/multi-agent-research-system
- https://claude.com/blog/how-anthropic-teams-use-claude-code

**Tier `(curl, secondary mirror of primary source)` — fetched and read, but the fetched page is a third-party archive of Boris Cherny's original X/Twitter posts, not the platform itself:**

- https://twitter-thread.com/t/2007179832300581177 (Boris Cherny personal setup, 15 tweets, posted "Jan 2")
- https://twitter-thread.com/t/2017742741636321619 (Claude Code team tips via Boris Cherny, 12 tweets, posted "Jan 31")

## Unverified / weak

- **Direct x.com posts could not be verified.** Fetching `x.com/bcherny/status/2007179832300581177` and the companion status ID both returned HTTP 200 but the response body is a JS-shell SPA with no matching text content (confirmed via a text search for known tweet content and the page title, both empty). I substituted the twitter-thread.com Thread Reader mirror, which *did* return real, matching text content, and used that instead — tagged distinctly above as a secondary mirror rather than a primary platform capture. The content is corroborated by independent WebSearch summaries of the same threads, which is reassuring but not itself a citation-grade source.
- **A third-party aggregator site claiming "107 tips" from Boris Cherny** was fetched to disk but **not used as a source** in this doc — it's a fan compilation, not a primary or first-party source, and I already had the two primary tweet threads directly. Flagging its existence in case a future update wants to mine it, with the same discount applied.
- **The Claude Code costs/usage doc** was fetched to disk but not read or incorporated in this revision — no claims in this doc rely on it. Marked "pending next update" in the Update Log.
- **The common-workflows doc** was only partially read (the "Work with tests" recipe). The bug-fix recipe, PR-creation recipe, and other prompt recipes on that page were not read in this pass — marked "pending next update."
- **The settings reference doc** is an extremely large reference (1700+ lines); only the scopes/precedence/permission/sandbox/plugin-config sections that mapped directly to a best-practice claim were read in full. Individual settings keys not mentioned above (there are 100+) were not verified and should be re-fetched on demand rather than assumed from memory.
- **The hooks reference doc** (as opposed to the hooks guide) is similarly large (4900+ lines); only the lifecycle table and the security-considerations section were read in full. Per-event input/output schemas were not individually verified.
- I did not fetch or verify Anthropic's "Effective context engineering for AI agents" post (surfaced only as a WebSearch lead while searching for the multi-agent research post) — not cited anywhere above; flagging as a candidate for a future update pass since it's directly on-topic for §6.
- No claim above is sourced from WebFetch output; WebFetch was not used at all in this research pass (all fetches were `curl` + `Read` or `WebSearch` for discovery only).

## Conformance map (filled by audit)

Filled 2026-07-04 by the runtime audit (`/Users/chadsimon/code/audits/claude-runtime-2026-07/AUDIT.md`); evidence lives there.

| # | Best-practice finding | Source section | Our runtime: conforms / deviates / N/A |
|---|---|---|---|
| 1 | CLAUDE.md kept under ~200 lines per file; bloat pruned ruthlessly | §1 | DEVIATES (mild): global 258 lines post-amendments; project 36, chad-twin 193. Trim candidate: move Refinements prose to a standard |
| 2 | Domain/procedural knowledge moved to skills instead of CLAUDE.md | §1 | Conforms: 32 skills + 19 standards docs; constitution keeps stubs pointing at owners |
| 3 | Path-scoped project rules directory used for large/monorepo instruction sets | §1 | Conforms as of 2026-07-04: `/Users/chadsimon/code/CLAUDE.md` created (was the audit's F2 gap) |
| 4 | Auto memory reviewed/audited periodically via `/memory`, not left unchecked | §1 | Conforms: curated MEMORY.md indexes (global + agent-memory) + omni-mem two-tier with lifecycle gates |
| 5 | Sensitive files excluded via `permissions.deny` (env files, secrets, credentials) | §2 | Conforms: deny list covers `.env*`, ssh/aws keys, `~/.claude.json`; observed firing this session (docs/.env reads denied) |
| 6 | Permission mode matched to task risk (not defaulting to `bypassPermissions` outside isolated envs) | §2 | DEVIATES (deliberate): full-auto posture — `defaultMode: auto` + bare Bash/Edit/Write + skip-prompt flags (audit F8; owner choice, documented) |
| 7 | Auto mode boundaries backed by deny rules, not only stated conversational boundaries (compaction risk) | §2 | Conforms: deny rules are the enforcement layer and survive compaction; verified deny-precedence on v2.1.201 |
| 8 | Sandbox enabled for Bash tool isolation where feasible; credentials explicitly denied/masked | §2 | Conforms: sandboxed Bash active (sandbox denials observed); secrets via rbw, never files |
| 9 | Hooks used for zero-exception enforcement rather than CLAUDE.md prose | §3 | Conforms: 26 hook wirings — policy_edit_gate, stop_gate L2, route classifier, lexical guards; CLAUDE.md keeps survive-hook-failure stubs |
| 10 | Hook scripts follow security best practices (quoting, path-traversal checks, absolute paths) | §3 | Not audited this pass — queued for next audit cycle |
| 11 | Subagents used to isolate high-volume/verbose operations from main context | §4 | Conforms: explorer/deep-research/worker fan-out pattern (this audit itself ran on it) |
| 12 | Custom subagent `description` fields specific enough for reliable auto-delegation | §4 | Conforms: 14 agent defs with boundary-explicit descriptions (incl. when-NOT-to-use) |
| 13 | Git worktrees (or equivalent) used for genuinely parallel sessions | §4 | Conforms: worker worktree isolation (`~/.claude/state/worktrees/`) + WorktreeCreate/Remove hooks |
| 14 | Explore → Plan → Implement → Commit workflow used for non-trivial changes | §5 | Conforms: plan mode + 6-stage coding-team pipeline; slice-boundary commits now mandated (2026-07-04 amendment) |
| 15 | Explicit, checkable verification signal before marking done | §5 | Conforms: testing-standard four-breadth gates + validator evidence requirements |
| 16 | Adversarial/fresh-context review step before treating autonomous work as done | §5 | Conforms: reviewer (draft-then-ground) + implementation-checker + Ralph postflight |
| 17 | Context cleared between unrelated tasks rather than accumulating | §6 | Conforms (different mechanism): auto-compact at 70% + PreCompact persistence to omni-mem; /clear discipline is habit, not config |
| 18 | Compaction behavior customized to preserve load-bearing state | §6 | Conforms: PreCompact hooks (omni-mem save + track marker) preserve state across compaction |
| 19 | MCP servers scoped appropriately and trust-vetted | §7 | Conforms: user-scope `~/.mcp.json`, all self-built/known servers; note audit F9 — `enabledMcpjsonServers` filter inert in trusted workspaces (v2.1.196+) |
| 20 | MCP/tool output kept token-efficient | §7, §10 | Not audited per-server this pass — queued |
| 21 | Standalone project config before plugin conversion | §8 | Conforms: local skills; only vetted third-party plugins (official + trailofbits) installed |
| 22 | Headless invocations with explicit `--allowedTools` scoping | §9 | Not audited this pass — `claude_run` wrapper's flag usage unverified; queued |
| 23 | Custom tools designed with agent-ergonomics principles | §10 | Conforms: grouped tool surfaces (chad-agent 26 tools/5 groups), mcp-error-migration standard for structured errors |
| 24 | Daily-repeated workflows turned into checked-in slash commands / skills | §11 | Conforms: go/govern/drive/daily-tech-brief/ecosystem-update et al. |
| 25 | Shared CLAUDE.md treated as a living, corrected-after-every-mistake artifact | §1, §11 | Conforms: git-versioned constitution with dated addenda; incident-driven rules (anti-stop/anti-overrun) cite their retrospectives |
