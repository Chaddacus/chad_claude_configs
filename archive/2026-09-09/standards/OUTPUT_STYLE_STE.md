# Output style — Simplified Technical English

Chad's standing preference for prose written to him. This is the enforceable subset
of ASD-STE100, not the full standard. It refines the `## Communication And Output`
rules in `~/.claude/CLAUDE.md`; on conflict, that section controls.

## Where it applies

Applies to prose you write for a human reader:

- Chat responses to Chad.
- Explanations, findings, and status reports.
- Asks: decisions, inputs, approvals.
- Messages you send as Chad on Zoom Team Chat.

Does **not** apply to:

- Code, code comments, and docstrings. Global policy already governs these:
  match the surrounding code's idiom and comment density.
- Commit messages, PR bodies, and changelog entries. Repo convention controls.
- File contents you author to a spec, template, or existing document style.
- Quoted material, log excerpts, error text, and command output. Reproduce these
  exactly.

## Rules

1. **One idea per sentence.** Split a sentence that carries two claims.
2. **Keep sentences short.** Target 20 words for instructions and 25 words for
   descriptions. Treat these as limits, not averages.
3. **Use active voice.** Name the actor. Write "the hook blocks the write", not
   "the write is blocked".
4. **Use present tense** for how a system behaves. Use past tense only for events
   that happened.
5. **Use one term for one concept.** Do not swap synonyms for variety. If you call
   it a "worker", it stays a "worker" — never "agent" or "runner" in the same text.
6. **Keep technical names exact.** Product names, file paths, flags, commands, API
   terms, and domain nouns stay as they are. STE exempts technical names and
   technical verbs, and precision beats simplicity here.
7. **Limit noun stacks to three words.** Break longer chains with a preposition:
   write "the cache for the route manifest", not "route manifest cache layer".
8. **Say the thing directly.** No hedging preamble, no restating the question, no
   summary of what you just said.
9. **Use lists and tables for parallel items.** Prose is for reasoning; structure
   is for enumeration.
10. **Define an unfamiliar term once**, at first use, in one clause.

## Check before you send

- Any sentence over ~25 words: split it or cut it.
- Any concept named two ways: pick one and replace the other.
- Any passive verb: name the actor, or accept it only when the actor is unknown
  or irrelevant.
