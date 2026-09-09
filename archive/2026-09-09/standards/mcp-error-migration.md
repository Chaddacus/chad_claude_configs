# MCP Error Categories — Migration Guide

**Cert tie-in:** Claude Architect Foundations Task 2.2 (structured tool errors).

This is the operator/caller-side guide for the dual-shape rollout of CW-owned
MCP servers. Producer-side contract lives in
`omni-mem/packages/contracts/src/mcp-errors.ts`.

## Wire shape

A failed MCP tool call now returns a successful JSONRPC response (no `error`
envelope) with this `result` shape:

```json
{
  "isError": true,
  "content": [
    { "type": "text", "text": "<category>: <detail>" },
    { "type": "text", "text": "{\"__cwError\":true,\"category\":\"<category>\",\"isRetryable\":<bool>,\"detail\":\"<string>\",\"retryAfterMs\":<optional int>}" }
  ]
}
```

Two content items, both `type: "text"`:
1. **Legacy token** — `<category>: <detail>`. Existing string-sniffing callers
   that match patterns like `task_not_found:` keep working because `detail`
   still leads with the token they expect.
2. **Structured envelope** — JSON with `__cwError: true` discriminator. New
   callers parse this for routing.

The legacy token will be **dropped in M3.8** (≥4 weeks after M3.3 lands).
After that, only the structured envelope ships.

## Categories

| Category | Default `isRetryable` | When to use |
|---|---|---|
| `validation` | false | Bad input shape; caller bug. |
| `auth` | false | Missing identity / forbidden. Re-auth required. |
| `not_found` | false | Resource doesn't exist. |
| `conflict` | false | Optimistic-lock / state-mismatch. Caller decides if retry helps. |
| `rate_limit` | true | 429-equivalent. Honor `retryAfterMs` if present. |
| `transient` | true | Upstream blip. Backoff and retry. |
| `internal` | true | Server bug. Retry then escalate after 2 attempts. |

`isRetryable` is decoupled from category because `conflict` and `internal`
have caller-specific retry semantics.

## Caller migration

### Before (string sniffing)

```ts
try {
  const r = await mcpClient.callTool("inbox_show", { taskId });
  ...
} catch (err) {
  if (err.message.includes("task_not_found")) {
    return null;
  }
  throw err;
}
```

This still works during the dual-shape phase because the legacy token is
present in `result.content[0].text`. But the caller is now reading from a
successful response, not an exception — most MCP clients surface
`isError: true` differently from a JSONRPC error.

### After (structured)

```ts
import { extractCwError } from "@omni-mem/contracts/mcp-errors";

const r = await mcpClient.callTool("inbox_show", { taskId });
if (r.isError) {
  const cwError = extractCwError(r.content);
  if (cwError?.category === "not_found") return null;
  if (cwError?.isRetryable) {
    await sleep(cwError.retryAfterMs ?? 500);
    return retry();
  }
  throw new Error(`${cwError?.category ?? "unknown"}: ${cwError?.detail ?? "no detail"}`);
}
```

## Producer migration (server author)

```ts
import { CwErrorThrowable } from "@omni-mem/contracts/mcp-errors";

// Inside a tool handler:
if (!found) {
  throw new CwErrorThrowable({
    category: "not_found",
    isRetryable: false,
    detail: `task_not_found: ${taskId}`,
  });
}
```

The dispatcher detects `CwErrorThrowable` and converts it to the wire shape
above. Plain `throw new Error(...)` still flows through the JSONRPC error
envelope unchanged — those represent protocol-level failures (unknown
method, framing error), not tool errors.

## Adoption status

| Server | Path | Status |
|---|---|---|
| omni-mem | `~/code/omni-mem/packages/cli/src/mcp.ts` | M3.1-M3.3 **merged to main** `cloudwarriors-ai/omni-mem#26` (squash `9e1a03e`, 2026-05-13). 12 throw sites, 30 tests. |
| omni-mem-manage | `~/code/omni-mem/packages/management-mcp/` | not started |
| stitch-mcp | `~/.claude/mcp-servers/stitch-mcp/` | M3.5 done — committed `532f669` on `codex/omni-mem-inbox-hook` (inline-vendored helpers, `categorizeStitchError` maps upstream HTTP/error-code patterns to categories) |
| sentinel | `cloudwarriors-ai/sentinel` (Python/FastMCP) | **merged to main** via promotion PR `#11` (`a2332d9`, 2026-05-13). Includes M3.4 (#9, 9 tool error sites) + M3.4-followup (#10, helper-internal raises via `SentinelToolError` + `@cw_helper_errors`). 560 tests pass on `dev`. |
| playwright | upstream | external — not in scope |
| openaiDeveloperDocs | upstream | external — not in scope |
| cloudwarriors | upstream | external — not in scope |

## Drop-legacy schedule

M3.8 (drop legacy `category: detail` content item from `wrapError()`) is
calendared **2026-06-10** — 4 weeks after the omni-mem main merge (2026-05-13).
Track caller migration in this file's Adoption status table — do not run M3.8
until every internal caller in the table has migrated (extractCwError, not regex).
