const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const https = require('https');
const http = require('http');

let statusBarItem;
let fileWatcher;
let syncQueue = [];
let isSyncing = false;
let isConnected = false;

/**
 * @param {vscode.ExtensionContext} context
 */
function activate(context) {
    console.log('Posterchanai RAG Sync is now active');

    // Create status bar item
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = 'posterchanai.configure';
    context.subscriptions.push(statusBarItem);

    // Register commands
    context.subscriptions.push(
        vscode.commands.registerCommand('posterchanai.configure', showConfigureMenu),
        vscode.commands.registerCommand('posterchanai.syncNow', syncAllFiles),
        vscode.commands.registerCommand('posterchanai.createCollection', createCollection),
        vscode.commands.registerCommand('posterchanai.disconnect', disconnect)
    );

    // Start watching if configured
    const config = vscode.workspace.getConfiguration('posterchanai');
    if (config.get('apiKey') && config.get('autoSync')) {
        startWatching();
    } else {
        updateStatusBar('disconnected');
    }

    // Listen for config changes
    context.subscriptions.push(
        vscode.workspace.onDidChangeConfiguration(e => {
            if (e.affectsConfiguration('posterchanai')) {
                const config = vscode.workspace.getConfiguration('posterchanai');
                if (config.get('apiKey') && config.get('autoSync')) {
                    startWatching();
                } else {
                    stopWatching();
                }
            }
        })
    );
}

function deactivate() {
    stopWatching();
    if (statusBarItem) {
        statusBarItem.dispose();
    }
}

function updateStatusBar(status, message = '') {
    switch (status) {
        case 'connected':
            statusBarItem.text = '$(check) RAG Sync';
            statusBarItem.tooltip = 'Posterchanai RAG: Connected and syncing';
            statusBarItem.backgroundColor = undefined;
            isConnected = true;
            break;
        case 'syncing':
            statusBarItem.text = '$(sync~spin) RAG Sync';
            statusBarItem.tooltip = `Syncing: ${message}`;
            statusBarItem.backgroundColor = undefined;
            break;
        case 'error':
            statusBarItem.text = '$(error) RAG Sync';
            statusBarItem.tooltip = `Error: ${message}`;
            statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
            break;
        case 'disconnected':
        default:
            statusBarItem.text = '$(plug) RAG Sync';
            statusBarItem.tooltip = 'Click to configure Posterchanai RAG Sync';
            statusBarItem.backgroundColor = undefined;
            isConnected = false;
            break;
    }
    statusBarItem.show();
}

async function showConfigureMenu() {
    const config = vscode.workspace.getConfiguration('posterchanai');
    const apiKey = config.get('apiKey');

    const options = [];

    if (!apiKey) {
        options.push({ label: '$(key) Set API Key', action: 'setApiKey' });
        options.push({ label: '$(add) Create New Collection', action: 'createCollection' });
    } else {
        options.push({ label: '$(sync) Sync All Files Now', action: 'syncNow' });
        options.push({ label: '$(key) Change API Key', action: 'setApiKey' });
        options.push({ label: '$(add) Create New Collection', action: 'createCollection' });
        options.push({ label: '$(gear) Open Settings', action: 'openSettings' });
        options.push({ label: '$(debug-disconnect) Disconnect', action: 'disconnect' });
    }

    const selected = await vscode.window.showQuickPick(options, {
        placeHolder: 'Posterchanai RAG Sync'
    });

    if (!selected) return;

    switch (selected.action) {
        case 'setApiKey':
            await setApiKey();
            break;
        case 'syncNow':
            await syncAllFiles();
            break;
        case 'createCollection':
            await createCollection();
            break;
        case 'openSettings':
            vscode.commands.executeCommand('workbench.action.openSettings', 'posterchanai');
            break;
        case 'disconnect':
            disconnect();
            break;
    }
}

async function setApiKey() {
    const config = vscode.workspace.getConfiguration('posterchanai');

    // First, get server URL
    const serverUrl = await vscode.window.showInputBox({
        prompt: 'Enter Posterchanai server URL',
        value: config.get('serverUrl') || 'http://localhost:3051',
        placeHolder: 'http://localhost:3051'
    });

    if (!serverUrl) return;

    // Then get API key
    const apiKey = await vscode.window.showInputBox({
        prompt: 'Enter RAG Watcher API Key (from Posterchanai RAG Management)',
        password: true,
        placeHolder: 'rag_xxxxxxxxxxxxxxxx'
    });

    if (!apiKey) return;

    // Save config
    await config.update('serverUrl', serverUrl, vscode.ConfigurationTarget.Global);
    await config.update('apiKey', apiKey, vscode.ConfigurationTarget.Global);

    // Test connection
    const success = await testConnection(serverUrl, apiKey);
    if (success) {
        vscode.window.showInformationMessage('Connected to Posterchanai RAG!');
        startWatching();
    } else {
        vscode.window.showErrorMessage('Failed to connect. Check your server URL and API key.');
        updateStatusBar('error', 'Connection failed');
    }
}

async function createCollection() {
    const config = vscode.workspace.getConfiguration('posterchanai');
    const serverUrl = config.get('serverUrl') || 'http://localhost:3051';

    // Get auth token from user
    const token = await vscode.window.showInputBox({
        prompt: 'Enter your Posterchanai auth token (from browser cookies or API)',
        password: true,
        placeHolder: 'Bearer token'
    });

    if (!token) return;

    // Get collection name
    const workspaceName = vscode.workspace.name || 'My Project';
    const name = await vscode.window.showInputBox({
        prompt: 'Collection name',
        value: workspaceName,
        placeHolder: 'My Project'
    });

    if (!name) return;

    // Get file patterns
    const patterns = await vscode.window.showInputBox({
        prompt: 'File patterns to index',
        value: config.get('filePatterns'),
        placeHolder: '*.py,*.js,*.ts,*.md'
    });

    if (!patterns) return;

    try {
        updateStatusBar('syncing', 'Creating collection...');

        // Create collection
        const collectionResponse = await httpRequest(serverUrl, '/api/rag/collections', 'POST', {
            name: name,
            description: `VS Code workspace: ${vscode.workspace.rootPath || 'unknown'}`,
            collection_type: 'watcher',
            file_patterns: patterns
        }, { 'Authorization': `Bearer ${token}` });

        if (!collectionResponse.id) {
            throw new Error('Failed to create collection');
        }

        // Create watcher
        const watcherResponse = await httpRequest(serverUrl, '/api/rag/watchers', 'POST', {
            collection_id: collectionResponse.id,
            watch_path: vscode.workspace.rootPath || '.'
        }, { 'Authorization': `Bearer ${token}` });

        if (!watcherResponse.api_key) {
            throw new Error('Failed to create watcher');
        }

        // Save the watcher API key
        await config.update('apiKey', watcherResponse.api_key, vscode.ConfigurationTarget.Global);
        await config.update('filePatterns', patterns, vscode.ConfigurationTarget.Global);

        vscode.window.showInformationMessage(
            `Collection "${name}" created! API key saved. Starting sync...`
        );

        // Start watching and do initial sync
        startWatching();
        await syncAllFiles();

    } catch (error) {
        vscode.window.showErrorMessage(`Failed to create collection: ${error.message}`);
        updateStatusBar('error', error.message);
    }
}

async function testConnection(serverUrl, apiKey) {
    try {
        // Send a test event
        await httpRequest(serverUrl, `/api/rag/watcher-event?api_key=${apiKey}`, 'POST', {
            event_type: 'modified',
            file_path: '.posterchanai-test',
            content: '# Connection test'
        });
        return true;
    } catch (error) {
        console.error('Connection test failed:', error);
        return false;
    }
}

function startWatching() {
    const config = vscode.workspace.getConfiguration('posterchanai');
    const apiKey = config.get('apiKey');

    if (!apiKey) {
        updateStatusBar('disconnected');
        return;
    }

    // Stop existing watcher
    stopWatching();

    // Get patterns
    const patterns = (config.get('filePatterns') || '*.py,*.js,*.ts')
        .split(',')
        .map(p => p.trim())
        .filter(p => p);

    const ignored = (config.get('ignoredFolders') || 'node_modules,.git')
        .split(',')
        .map(p => p.trim())
        .filter(p => p);

    // Create glob pattern
    const globPattern = `**/{${patterns.join(',')}}`;

    // Create file watcher
    fileWatcher = vscode.workspace.createFileSystemWatcher(globPattern);

    fileWatcher.onDidCreate(uri => handleFileEvent('created', uri, ignored));
    fileWatcher.onDidChange(uri => handleFileEvent('modified', uri, ignored));
    fileWatcher.onDidDelete(uri => handleFileEvent('deleted', uri, ignored));

    updateStatusBar('connected');
    console.log('Posterchanai RAG Sync: Watching for file changes');
}

function stopWatching() {
    if (fileWatcher) {
        fileWatcher.dispose();
        fileWatcher = null;
    }
    updateStatusBar('disconnected');
}

function disconnect() {
    const config = vscode.workspace.getConfiguration('posterchanai');
    config.update('apiKey', '', vscode.ConfigurationTarget.Global);
    stopWatching();
    vscode.window.showInformationMessage('Disconnected from Posterchanai RAG');
}

function handleFileEvent(eventType, uri, ignored) {
    const filePath = uri.fsPath;
    const relativePath = vscode.workspace.asRelativePath(uri);

    // Check if in ignored folder
    for (const folder of ignored) {
        if (relativePath.includes(folder + '/') || relativePath.includes(folder + '\\')) {
            return;
        }
    }

    // Add to queue
    syncQueue.push({ eventType, filePath, relativePath });
    processSyncQueue();
}

async function processSyncQueue() {
    if (isSyncing || syncQueue.length === 0) return;

    isSyncing = true;
    const config = vscode.workspace.getConfiguration('posterchanai');
    const serverUrl = config.get('serverUrl');
    const apiKey = config.get('apiKey');

    while (syncQueue.length > 0) {
        const event = syncQueue.shift();
        updateStatusBar('syncing', event.relativePath);

        try {
            let content = null;
            if (event.eventType !== 'deleted') {
                try {
                    content = fs.readFileSync(event.filePath, 'utf8');
                    // Skip very large files (> 500KB)
                    if (content.length > 500000) {
                        console.log(`Skipping large file: ${event.relativePath}`);
                        continue;
                    }
                } catch (e) {
                    console.error(`Failed to read file: ${event.filePath}`, e);
                    continue;
                }
            }

            await httpRequest(serverUrl, `/api/rag/watcher-event?api_key=${apiKey}`, 'POST', {
                event_type: event.eventType,
                file_path: event.relativePath,
                content: content
            });

        } catch (error) {
            console.error(`Failed to sync ${event.relativePath}:`, error);
            // Don't update status bar for individual failures
        }
    }

    isSyncing = false;
    updateStatusBar('connected');
}

async function syncAllFiles() {
    const config = vscode.workspace.getConfiguration('posterchanai');
    const apiKey = config.get('apiKey');

    if (!apiKey) {
        vscode.window.showWarningMessage('Please configure API key first');
        return;
    }

    const patterns = (config.get('filePatterns') || '*.py,*.js,*.ts')
        .split(',')
        .map(p => p.trim())
        .filter(p => p);

    const ignored = (config.get('ignoredFolders') || 'node_modules,.git')
        .split(',')
        .map(p => p.trim())
        .filter(p => p);

    // Find all matching files
    const globPattern = `**/{${patterns.join(',')}}`;
    const files = await vscode.workspace.findFiles(globPattern);

    // Filter out ignored folders
    const filteredFiles = files.filter(uri => {
        const relativePath = vscode.workspace.asRelativePath(uri);
        for (const folder of ignored) {
            if (relativePath.includes(folder + '/') || relativePath.includes(folder + '\\')) {
                return false;
            }
        }
        return true;
    });

    if (filteredFiles.length === 0) {
        vscode.window.showInformationMessage('No files to sync');
        return;
    }

    // Confirm sync
    const confirm = await vscode.window.showInformationMessage(
        `Sync ${filteredFiles.length} files to Posterchanai RAG?`,
        'Yes', 'No'
    );

    if (confirm !== 'Yes') return;

    // Sync all files
    let synced = 0;
    let failed = 0;

    await vscode.window.withProgress({
        location: vscode.ProgressLocation.Notification,
        title: 'Syncing to Posterchanai RAG',
        cancellable: true
    }, async (progress, token) => {
        for (let i = 0; i < filteredFiles.length; i++) {
            if (token.isCancellationRequested) break;

            const uri = filteredFiles[i];
            const relativePath = vscode.workspace.asRelativePath(uri);

            progress.report({
                message: `${i + 1}/${filteredFiles.length}: ${relativePath}`,
                increment: (1 / filteredFiles.length) * 100
            });

            try {
                const content = fs.readFileSync(uri.fsPath, 'utf8');

                // Skip very large files
                if (content.length > 500000) {
                    console.log(`Skipping large file: ${relativePath}`);
                    continue;
                }

                const serverUrl = config.get('serverUrl');
                await httpRequest(serverUrl, `/api/rag/watcher-event?api_key=${apiKey}`, 'POST', {
                    event_type: 'modified',
                    file_path: relativePath,
                    content: content
                });
                synced++;
            } catch (error) {
                console.error(`Failed to sync ${relativePath}:`, error);
                failed++;
            }
        }
    });

    vscode.window.showInformationMessage(
        `Synced ${synced} files${failed > 0 ? `, ${failed} failed` : ''}`
    );
}

function httpRequest(baseUrl, path, method, body, extraHeaders = {}) {
    return new Promise((resolve, reject) => {
        const url = new URL(path, baseUrl);
        const isHttps = url.protocol === 'https:';
        const lib = isHttps ? https : http;

        const options = {
            hostname: url.hostname,
            port: url.port || (isHttps ? 443 : 80),
            path: url.pathname + url.search,
            method: method,
            headers: {
                'Content-Type': 'application/json',
                ...extraHeaders
            }
        };

        const req = lib.request(options, (res) => {
            let data = '';
            res.on('data', chunk => data += chunk);
            res.on('end', () => {
                if (res.statusCode >= 200 && res.statusCode < 300) {
                    try {
                        resolve(JSON.parse(data));
                    } catch {
                        resolve(data);
                    }
                } else {
                    reject(new Error(`HTTP ${res.statusCode}: ${data}`));
                }
            });
        });

        req.on('error', reject);
        req.setTimeout(30000, () => {
            req.destroy();
            reject(new Error('Request timeout'));
        });

        if (body) {
            req.write(JSON.stringify(body));
        }
        req.end();
    });
}

module.exports = {
    activate,
    deactivate
};
