import { build } from 'esbuild';
import { chmodSync } from 'fs';

await build({
  entryPoints: ['src/server.ts'],
  bundle: true,
  platform: 'node',
  target: 'node18',
  format: 'cjs',
  outfile: 'dist/stitch-mcp.cjs',
  minify: true,
  logLevel: 'error',
  banner: { js: '#!/usr/bin/env node' },
});

chmodSync('dist/stitch-mcp.cjs', 0o755);
console.log('Built dist/stitch-mcp.cjs');
