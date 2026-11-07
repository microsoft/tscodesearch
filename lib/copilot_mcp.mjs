import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

export function updateCopilotMcp(filePath, definition) {
  if (!definition && !existsSync(filePath)) return;
  const config = existsSync(filePath) ? JSON.parse(readFileSync(filePath, 'utf8')) : {};
  if (!config || typeof config !== 'object' || Array.isArray(config) ||
      (config.mcpServers !== undefined &&
       (!config.mcpServers || typeof config.mcpServers !== 'object' || Array.isArray(config.mcpServers)))) {
    throw new Error(`Invalid MCP configuration in ${filePath}.`);
  }
  if (definition) {
    config.mcpServers ??= {};
    config.mcpServers.tscodesearch = definition;
  } else {
    if (!config.mcpServers?.tscodesearch) return;
    delete config.mcpServers.tscodesearch;
  }
  mkdirSync(dirname(filePath), { recursive: true });
  writeFileSync(filePath, JSON.stringify(config, null, 2) + '\n', 'utf8');
}
