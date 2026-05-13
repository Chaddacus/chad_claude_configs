import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import { StitchToolClient } from '@google/stitch-sdk';

// Inline copy of the cert Task 2.2 wire shape (matches @omni-mem/contracts/
// mcp-errors and cloudwarriors-ai/sentinel src/sentinel/errors.py exactly).
// Vendored inline rather than depending on @omni-mem/contracts because this
// server is a 85-line standalone ESM; pulling in the contracts package is
// overengineering. Migration guide: ~/.claude/standards/mcp-error-migration.md
const CW_ERROR_TAG = '__cwError' as const;
type CwErrorCategory =
  | 'validation'
  | 'auth'
  | 'not_found'
  | 'conflict'
  | 'rate_limit'
  | 'transient'
  | 'internal';
const DEFAULT_RETRYABLE: Record<CwErrorCategory, boolean> = {
  validation: false,
  auth: false,
  not_found: false,
  conflict: false,
  rate_limit: true,
  transient: true,
  internal: true,
};

function wrapCwError(
  category: CwErrorCategory,
  detail: string,
  options: { isRetryable?: boolean; retryAfterMs?: number } = {},
) {
  const isRetryable = options.isRetryable ?? DEFAULT_RETRYABLE[category];
  const envelope: Record<string, unknown> = {
    [CW_ERROR_TAG]: true,
    category,
    isRetryable,
    detail,
    ...(options.retryAfterMs !== undefined && { retryAfterMs: options.retryAfterMs }),
  };
  return {
    isError: true as const,
    content: [
      { type: 'text' as const, text: `${category}: ${detail}` },
      { type: 'text' as const, text: JSON.stringify(envelope) },
    ],
  };
}

/**
 * Map an upstream Stitch error into a CwError category. The upstream SDK
 * mostly throws on HTTP failures + auth issues; we cover the main shapes
 * and fall back to "internal" with isRetryable=true so callers retry once
 * before escalating.
 */
function categorizeStitchError(error: { message?: string; status?: number; code?: string }): {
  category: CwErrorCategory;
  retryAfterMs?: number;
} {
  const status = typeof error.status === 'number' ? error.status : undefined;
  const code = typeof error.code === 'string' ? error.code.toUpperCase() : '';
  if (status === 401 || status === 403 || code.includes('UNAUTHORIZED') || code.includes('FORBIDDEN')) {
    return { category: 'auth' };
  }
  if (status === 404 || code.includes('NOT_FOUND')) {
    return { category: 'not_found' };
  }
  if (status === 429 || code.includes('RATE_LIMIT')) {
    return { category: 'rate_limit', retryAfterMs: 1000 };
  }
  if (status === 400 || status === 422 || code.includes('VALIDATION') || code.includes('INVALID')) {
    return { category: 'validation' };
  }
  if (status !== undefined && status >= 500) {
    return { category: 'transient' };
  }
  if (code.includes('TIMEOUT') || code.includes('ECONNRESET') || code.includes('ENOTFOUND')) {
    return { category: 'transient' };
  }
  return { category: 'internal' };
}

// MCP uses stdio — stdout is JSON-RPC only. Redirect console.log to stderr.
console.log = (...args: any[]) => console.error('[stitch-mcp]', ...args);

const API_KEY = process.env.STITCH_API_KEY;
if (!API_KEY) {
  console.error('[stitch-mcp] STITCH_API_KEY env var is required');
  process.exit(1);
}

// Lazy-initialized StitchToolClient (the one that actually works)
let client: StitchToolClient | null = null;
let cachedTools: any[] | null = null;

async function getClient(): Promise<StitchToolClient> {
  if (!client) {
    client = new StitchToolClient({ apiKey: API_KEY });
  }
  return client;
}

async function getStitchTools(): Promise<any[]> {
  if (!cachedTools) {
    const c = await getClient();
    const result = await c.listTools();
    cachedTools = result.tools || [];
  }
  return cachedTools;
}

// Server setup — identical to claude-mem pattern
const server = new Server(
  { name: 'stitch-mcp', version: '1.0.0' },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => {
  const tools = await getStitchTools();
  return {
    tools: tools.map((t: any) => ({
      name: `stitch_${t.name}`,
      description: t.description || t.name,
      inputSchema: t.inputSchema || { type: 'object', properties: {} },
    })),
  };
});

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  // Strip the stitch_ prefix to get the actual Stitch tool name
  const stitchToolName = name.replace(/^stitch_/, '');
  const c = await getClient();
  try {
    const result = await c.callTool(stitchToolName, args || {});
    // StitchToolClient returns MCP-formatted results already
    if (result && result.content) {
      return result;
    }
    return {
      content: [{ type: 'text' as const, text: JSON.stringify(result, null, 2) }],
    };
  } catch (error: any) {
    const { category, retryAfterMs } = categorizeStitchError(error);
    return wrapCwError(category, error.message ?? String(error), { retryAfterMs });
  }
});

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
