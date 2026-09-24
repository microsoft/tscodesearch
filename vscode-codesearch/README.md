# TsCodeSearch for VS Code

TsCodeSearch adds a search view for the local Tantivy index managed by the
tscodesearch daemon. It supports full-text and tree-sitter structural search
across configured source roots.

## Setup

Run `setup.cmd` from the repository root. Setup creates the Python environment,
registers the MCP server with VS Code Chat, installs this extension, and records
the repository path in the machine-specific `tscodesearch.repoPath` setting.

If the extension is installed separately, run **TsCodeSearch: Set Up** and
select the tscodesearch repository root.

## Commands

- **TsCodeSearch: Open Panel**
- **TsCodeSearch: Set Up**
- **TsCodeSearch: Add Root**
- **TsCodeSearch: Remove Root**
- **TsCodeSearch: Restart**
- **TsCodeSearch: Stop**
- **TsCodeSearch: Re-index Root**

The extension requires a trusted, local workspace because it starts the local
daemon and opens files from configured source roots.

## Development

```text
npm install
npm run compile
npm test
```

Press F5 from the `vscode-codesearch` folder to launch an Extension Development
Host.
