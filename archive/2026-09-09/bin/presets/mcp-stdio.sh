#!/bin/bash
# mcp-stdio: Spawn an MCP server module and exercise the real stdio protocol.
#
# Usage:
#   mcp-stdio.sh <module> [required_tool1,required_tool2,...]
# Example:
#   mcp-stdio.sh mcp_server.server get_state,apply_move,solve_cube
#
# Contract:
#   - Exits 0 iff:
#     * `uv run python -m <module>` launches an MCP server over stdio.
#     * Client can initialize the session.
#     * list_tools returns a non-empty tool set.
#     * Every name in <required_tools> (comma-separated) is present.
# What this catches:
#   - Module import errors (wrong paths, syntax).
#   - Tool-registration bugs (e.g., `tool_*` prefix pollution).
#   - Broken MCP protocol compliance.
# What this does NOT catch:
#   - Logic correctness of individual tools — write a slice test for that.
#
# NOTE on FastMCP list serialization:
#   FastMCP tools that return list[dict] serialize each element as a SEPARATE
#   TextContent block, NOT as one JSON array. To parse a list result:
#     [json.loads(c.text) for c in result.content]
#   NOT:
#     json.loads(result.content[0].text)  # ← this gets only the first element

set -u
if [ $# -lt 1 ]; then
  echo "usage: mcp-stdio.sh <module> [required_tool1,required_tool2,...]" >&2
  exit 2
fi
module="$1"
required="${2:-}"

exec uv run python - "$module" "$required" <<'PY'
import asyncio, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

module = sys.argv[1]
required = [t.strip() for t in sys.argv[2].split(",") if t.strip()] if len(sys.argv) > 2 else []


async def main() -> int:
    params = StdioServerParameters(command="uv", args=["run", "python", "-m", module])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            print(f"tools: {sorted(names)}")
            if not names:
                print("FAIL: server exposed zero tools", file=sys.stderr)
                return 1
            missing = set(required) - names
            if missing:
                print(f"FAIL: required tools missing: {sorted(missing)}", file=sys.stderr)
                return 1
            print("OK mcp-stdio passed")
            return 0


sys.exit(asyncio.run(main()))
PY
