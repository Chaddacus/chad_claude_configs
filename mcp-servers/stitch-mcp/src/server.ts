import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import { StitchToolClient } from '@google/stitch-sdk';

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
    return {
      content: [{ type: 'text' as const, text: `Error: ${error.message}` }],
      isError: true,
    };
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
