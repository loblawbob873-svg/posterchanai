# Posterchanai RAG Sync - VS Code Extension

Automatically sync your codebase to Posterchanai RAG for AI-powered code assistance.

## Features

- **Auto-sync on save** - File changes are automatically sent to your Posterchanai RAG collection
- **Status bar indicator** - See connection status at a glance
- **Configurable file patterns** - Control which files are indexed
- **Folder exclusions** - Ignore node_modules, .git, and other folders
- **Manual sync** - Force sync all files when needed
- **Create collections** - Set up new RAG collections directly from VS Code

## Installation

### From VSIX (Recommended)

1. Download the `.vsix` file from releases
2. In VS Code: `Ctrl+Shift+P` → "Extensions: Install from VSIX..."
3. Select the downloaded `.vsix` file

### Build from Source

```bash
cd vscode-extension
npm install
npm run package
```

This creates `posterchanai-rag-sync-1.0.0.vsix` which you can install.

## Setup

### Option 1: Use Existing Watcher (Quick)

If you already have a RAG collection with a watcher in Posterchanai:

1. Click the "RAG Sync" status bar item (or `Ctrl+Shift+P` → "Posterchanai: Configure RAG Sync")
2. Enter your Posterchanai server URL (e.g., `http://localhost:3051`)
3. Enter your watcher API key (from Posterchanai RAG Management)
4. Done! Files will sync automatically

### Option 2: Create New Collection

To create a new RAG collection for your project:

1. Click "RAG Sync" → "Create New Collection"
2. Enter your Posterchanai auth token (from browser cookies)
3. Enter a collection name
4. Configure file patterns (e.g., `*.py,*.js,*.ts,*.md`)
5. The extension will create the collection and start syncing

## Configuration

Open VS Code settings (`Ctrl+,`) and search for "posterchanai":

| Setting | Default | Description |
|---------|---------|-------------|
| `posterchanai.serverUrl` | `http://localhost:3051` | Posterchanai server URL |
| `posterchanai.apiKey` | (empty) | RAG watcher API key |
| `posterchanai.filePatterns` | `*.py,*.js,*.ts,...` | File patterns to sync |
| `posterchanai.ignoredFolders` | `node_modules,.git,...` | Folders to ignore |
| `posterchanai.autoSync` | `true` | Auto-sync file changes |

### Default File Patterns

```
*.py, *.js, *.ts, *.tsx, *.jsx, *.go, *.rs, *.java, *.kt, *.md, *.txt
```

### Default Ignored Folders

```
node_modules, .git, __pycache__, venv, .venv, dist, build, .next, target
```

## Commands

Access via Command Palette (`Ctrl+Shift+P`):

| Command | Description |
|---------|-------------|
| Posterchanai: Configure RAG Sync | Open configuration menu |
| Posterchanai: Sync All Files Now | Force sync all matching files |
| Posterchanai: Create New RAG Collection | Create collection for this workspace |
| Posterchanai: Disconnect | Clear API key and stop syncing |

## Status Bar Icons

| Icon | Status |
|------|--------|
| $(plug) RAG Sync | Disconnected - click to configure |
| $(check) RAG Sync | Connected and watching for changes |
| $(sync~spin) RAG Sync | Currently syncing files |
| $(error) RAG Sync | Error - hover for details |

## How It Works

1. **File Watcher**: VS Code monitors files matching your patterns
2. **Change Detection**: On create/modify/delete, files are queued for sync
3. **API Call**: File content is sent to `/api/rag/watcher-event`
4. **Server Processing**: Posterchanai chunks, embeds, and indexes the file
5. **RAG Context**: When chatting, relevant code is retrieved and injected

## File Size Limits

- Files larger than 500KB are skipped automatically
- This prevents indexing large generated files or binaries

## Troubleshooting

### "Connection failed"

- Check server URL is correct and server is running
- Verify API key is valid (get a fresh one from RAG Management)
- Check network connectivity

### Files not syncing

- Ensure `autoSync` is enabled in settings
- Check file matches configured patterns
- Verify file is not in an ignored folder
- Check VS Code output panel for errors

### Large workspace slow to sync

- Initial "Sync All Files" may take time for large codebases
- Subsequent auto-syncs only send changed files
- Consider adding more patterns to `ignoredFolders`

## Getting Your API Key

1. Log into Posterchanai web interface
2. Go to RAG Management (if available in your installation)
3. Create or select a collection with type "watcher"
4. Copy the watcher API key
5. Paste into VS Code extension configuration

## Security Notes

- API key is stored in VS Code's global settings
- File contents are sent over HTTP/HTTPS to your server
- Use HTTPS in production for encrypted transfer
- API keys should be kept confidential

## License

MIT
