import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { updateCopilotMcp } from '../lib/copilot_mcp.mjs';

test('portable registration preserves other servers and supports uninstall', () => {
  const directory = mkdtempSync(join(tmpdir(), 'tscodesearch-mcp-'));
  const file = join(directory, 'mcp-config.json');
  try {
    writeFileSync(file, JSON.stringify({ mcpServers: { widget: { command: 'widget' } }, extra: true }));
    const definition = { command: 'uv', args: ['run', '--no-sync', 'python', 'mcp_server.py'] };
    updateCopilotMcp(file, definition);
    updateCopilotMcp(file, definition);
    const config = JSON.parse(readFileSync(file, 'utf8'));
    assert.deepEqual(config.mcpServers.tscodesearch, definition);
    assert.deepEqual(config.mcpServers.widget, { command: 'widget' });
    assert.equal(config.extra, true);
    updateCopilotMcp(file);
    assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), {
      mcpServers: { widget: { command: 'widget' } }, extra: true,
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('portable registration creates a configuration and rejects malformed input', () => {
  const directory = mkdtempSync(join(tmpdir(), 'tscodesearch-mcp-'));
  const file = join(directory, 'mcp-config.json');
  try {
    updateCopilotMcp(file);
    updateCopilotMcp(file, { command: 'uv' });
    assert.deepEqual(JSON.parse(readFileSync(file, 'utf8')), {
      mcpServers: { tscodesearch: { command: 'uv' } },
    });
    for (const invalid of ['{', '[]', 'null', '{"mcpServers":[]}']) {
      writeFileSync(file, invalid);
      assert.throws(() => updateCopilotMcp(file, { command: 'uv' }));
      assert.equal(readFileSync(file, 'utf8'), invalid);
    }
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
