# Advanced Documentation

This document contains detailed technical documentation for Poster-chan AI. For quick start and basic usage, see [README.md](../README.md).

## RAG (Retrieval-Augmented Generation)

Posterchanai includes built-in RAG support for indexing and querying codebases. This enables the AI to provide contextually aware responses based on your project files.

### Features

- **Fully local** - Uses sentence-transformers for embeddings, no external API needed
- **ChromaDB** - Persistent file-based vector store
- **Code-aware chunking** - Splits code by function/class boundaries:
  - Python: class/function definitions
  - JavaScript/TypeScript: functions, classes, arrow functions
  - Go: func/type definitions
  - Rust: fn/impl/struct blocks
  - Java/Kotlin: method definitions
  - Markdown: header sections
- **Incremental updates** - Only re-indexes changed files (SHA256 hash tracking)
- **Auto-context** - Relevant code snippets automatically injected into chat

### Configuration

Access Admin Panel > Settings to configure RAG:

| Setting | Default | Description |
|---------|---------|-------------|
| `rag_enabled` | true | Enable/disable RAG |
| `rag_embedding_model` | all-MiniLM-L6-v2 | Sentence-transformers model (~90MB) |
| `rag_chunk_size` | 1000 | Max characters per chunk |
| `rag_chunk_overlap` | 200 | Overlap between chunks |
| `rag_top_k` | 5 | Number of chunks to retrieve |
| `rag_min_similarity` | 0.3 | Minimum similarity threshold (0-1) |
| `rag_chromadb_path` | ./data/chromadb | Vector store location |
| `rag_auto_context` | true | Auto-inject RAG context into chat |
| `rag_auto_warmup` | true | Pre-load RAG data into RAM on startup |
| `rag_embedding_cache_max` | 250000 | Max cached embeddings (~375MB RAM) |
| `rag_query_cache_max` | 100000 | Max cached query results |
| `rag_query_cache_ttl` | 600 | Query cache TTL in seconds |
| `rag_hnsw_ef_search` | 100 | HNSW query accuracy (10-500) |
| `rag_hnsw_ef_construction` | 200 | HNSW index build quality (50-1000) |
| `rag_hnsw_m` | 16 | HNSW graph connectivity (4-64) |

### Performance Tuning

RAG includes aggressive RAM caching for fast queries:

**Auto-warmup on startup:**
- Embedding model pre-loaded (~200-400MB)
- ChromaDB indexes loaded into memory
- All document chunks cached
- Controlled by `rag_auto_warmup` setting

**Cache layers:**
| Cache | Default Size | RAM Usage |
|-------|--------------|-----------|
| Embedding cache | 250,000 entries | ~375 MB |
| Query results cache | 100,000 entries | ~100 MB |
| Settings cache | 1 hour TTL | <1 KB |
| ChromaDB collections | Permanent | Varies |

**For maximum performance:**
```bash
# Admin UI settings:
rag_embedding_cache_max = 500000   # ~750MB RAM
rag_query_cache_max = 200000       # More cached results
rag_query_cache_ttl = 1800         # 30 min TTL
rag_hnsw_ef_search = 200           # Better accuracy
```

**Clear caches:** Admin UI > RAG > "Clear RAG Caches" button

### Creating Collections

#### Option 1: Clone a Git Repository

```bash
curl -X POST http://localhost:3051/api/rag/collections/git \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Project",
    "git_url": "https://github.com/user/repo.git",
    "branch": "main",
    "file_patterns": "*.py,*.js,*.ts,*.md"
  }'
```

Indexing runs in the background. Check status with:
```bash
curl http://localhost:3051/api/rag/collections/1/stats \
  -H "Authorization: Bearer $TOKEN"
```

#### Option 2: Upload a Zip File

```bash
curl -X POST http://localhost:3051/api/rag/collections/upload \
  -H "Authorization: Bearer $TOKEN" \
  -F "name=My Project" \
  -F "file=@project.zip" \
  -F "file_patterns=*.py,*.js,*.md"
```

#### Option 3: Index a Local Folder

```bash
curl -X POST http://localhost:3051/api/rag/collections/folder \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Local Project",
    "source_path": "/home/user/projects/myapp",
    "file_patterns": "*.py,*.js,*.ts,*.md"
  }'
```

#### Option 4: VS Code Extension (Recommended for Real-time Sync)

Install the **Posterchanai RAG Sync** VS Code extension for seamless file synchronization:

1. Build and install the extension:
```bash
cd vscode-extension
npm install
npm run package
# Install the generated .vsix file in VS Code
```

2. In VS Code, click the "RAG Sync" status bar item
3. Enter your server URL and create a new collection (or use existing API key)
4. Files sync automatically as you edit!

**Features:**
- Auto-sync on file save
- Status bar indicator
- Configurable file patterns and ignored folders
- Batch sync all files command

See `vscode-extension/README.md` for detailed setup instructions.

<details>
<summary>Alternative: Manual Node.js Watcher Script</summary>

If you prefer not to use the VS Code extension:

1. Create a watcher for an existing collection:
```bash
curl -X POST http://localhost:3051/api/rag/watchers \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "collection_id": 1,
    "watch_path": "/home/user/projects/myapp"
  }'
```

2. Save the returned `api_key`

3. Create `watch.js`:
```javascript
const chokidar = require('chokidar');
const fs = require('fs');

const API_KEY = 'rag_your_api_key_here';
const API_URL = 'http://localhost:3051/api/rag/watcher-event';

chokidar.watch('.', {
  ignored: /(^|[\/\\])\.|node_modules|__pycache__|\.git/,
  persistent: true
}).on('all', async (event, path) => {
  if (!path.match(/\.(py|js|ts|tsx|md|txt)$/)) return;

  const eventMap = { add: 'created', change: 'modified', unlink: 'deleted' };
  const eventType = eventMap[event];
  if (!eventType) return;

  const content = eventType !== 'deleted' ? fs.readFileSync(path, 'utf8') : null;

  await fetch(`${API_URL}?api_key=${API_KEY}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event_type: eventType, file_path: path, content })
  });
});
```

4. Run: `npm install chokidar && node watch.js`
</details>

### Querying RAG

RAG context is **automatically injected** into chat when `rag_auto_context` is enabled. Just ask questions about your code!

For direct queries:
```bash
curl -X POST http://localhost:3051/api/rag/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "How does the authentication system work?",
    "top_k": 5
  }'
```

### RAG API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/rag/collections` | GET | List all collections |
| `/api/rag/collections` | POST | Create a collection |
| `/api/rag/collections/{id}` | GET | Get collection details |
| `/api/rag/collections/{id}` | DELETE | Delete collection |
| `/api/rag/collections/{id}/stats` | GET | Get collection statistics |
| `/api/rag/collections/{id}/reindex` | POST | Re-index collection |
| `/api/rag/collections/git` | POST | Clone and index git repo |
| `/api/rag/collections/upload` | POST | Upload and index zip file |
| `/api/rag/collections/folder` | POST | Index local folder |
| `/api/rag/watchers` | GET | List file watchers |
| `/api/rag/watchers` | POST | Create file watcher |
| `/api/rag/watchers/{id}` | DELETE | Delete watcher |
| `/api/rag/watcher-event` | POST | Handle file event (API key auth) |
| `/api/rag/query` | POST | Query RAG index |
| `/api/rag/status` | GET | Get RAG status |

### Dependencies

RAG requires these additional packages (included in requirements.txt):
```
chromadb>=0.4.0
sentence-transformers>=2.2.0
mcp>=1.0.0  # For MCP server
```

The embedding model (~90MB) is downloaded automatically on first use.

### MCP Server (Model Context Protocol)

Posterchanai includes an **integrated MCP server** that exposes RAG functionality to MCP-compatible clients like Continue.dev, Claude Desktop, and other AI coding assistants.

#### Configuration

The MCP server is **enabled by default** and starts automatically with the main application. No separate service is needed.

**Admin Settings** (Admin > Services > MCP Server):
| Setting | Default | Description |
|---------|---------|-------------|
| Enable MCP Server | On | Start MCP server with the app |
| Host | 0.0.0.0 | Interface to bind (0.0.0.0 for all) |
| Port | 8808 | Port for MCP clients |
| Auto-warmup | On | Pre-load embeddings into RAM on start |

**HTTP Endpoints:**
| Endpoint | Method | Description |
|----------|--------|-------------|
| `/sse` | GET | SSE connection for MCP clients |
| `/search` | POST | Query RAG index |
| `/status` | GET | Cache stats and health |
| `/warmup` | POST | Pre-load embedding model |

#### Configure Continue.dev

Add to your `~/.continue/config.yaml`:

```yaml
mcpServers:
  - name: posterchanai-rag
    url: http://YOUR_SERVER_IP:8808/sse
```

#### Available MCP Tools

| Tool | Description |
|------|-------------|
| `search_codebase` | Search indexed code for relevant snippets |
| `list_collections` | List all RAG collections |
| `reindex_collection` | Re-index a collection after code changes |

#### REST Endpoint for Git Hooks

The MCP server also exposes a REST endpoint for triggering reindex from git hooks:

```bash
curl -X POST "http://localhost:8808/reindex" \
  -H "Content-Type: application/json" \
  -d '{"collection_id": 2}'
```

**Git post-merge hook** (`.git/hooks/post-merge`):
```bash
#!/bin/bash
curl -s -X POST "http://localhost:8808/reindex" \
  -H "Content-Type: application/json" \
  -d '{"collection_id": 2}' &
echo "RAG re-index triggered"
```

#### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RAG_USER_ID` | 1 | User ID for RAG queries |

### Distributed RAG (Remote RAG API)

For load-balanced setups with multiple posterchanai servers, you can configure remote nodes to query a central RAG server instead of maintaining their own RAG index.

**Architecture:**
```
Request → Remote Node (192.168.0.85)
              ↓
           Query http://192.168.0.1:9999/search
              ↓
           Inject RAG context into prompt
              ↓
           Run local inference
```

**Setup:**

1. **On the main server (192.168.0.1)** - The MCP server starts automatically with the app. Configure the port in Admin > Services > MCP Server (default: 8808).

2. **On remote nodes (192.168.0.85, etc.)** - Configure the RAG API URL:
   - Go to Admin > RAG > Settings
   - Set **Remote RAG API URL** to `http://192.168.0.1:9999`
   - Save settings

3. **On the main server** - Leave **Remote RAG API URL** empty (uses local RAG)

**How it works:**
- When a remote node receives a chat request, it queries the central RAG server via HTTP
- The RAG server returns relevant code snippets
- The remote node injects the context into the prompt and runs inference locally
- No need to maintain RAG indexes on every node

**REST API Endpoint:**

The MCP RAG server exposes a `/search` endpoint for remote queries:

```bash
curl -X POST http://192.168.0.1:9999/search \
  -H "Content-Type: application/json" \
  -d '{"query": "how does authentication work", "top_k": 3}'
```

Response:
```json
{
  "results": [
    {
      "content": "def verify_api_key(...)...",
      "file_path": "app/routers/openai_api.py",
      "similarity": 0.72,
      "collection_name": "posterchanai"
    }
  ]
}
```

### Continue.dev Configuration

Continue.dev is an AI coding assistant that integrates with VS Code. Here's an optimized configuration for use with Posterchanai.

**Copy `docs/continue-config.yaml` to `~/.continue/config.yaml`** and update the API settings.

**Sample config for small context windows (5k-8k tokens):**

```yaml
name: Local Assistant
version: 1.0.0
schema: v1

models:
  - name: PosterChan AI
    provider: openai
    model: your-model-name.gguf
    apiBase: https://your-server.com/v1
    apiKey: your-api-key-here
    env:
      useLegacyCompletionsEndpoint: false
    defaultCompletionOptions:
      contextLength: 8000
      maxTokens: 512
    roles:
      - chat
      - edit

context:
  - provider: code
  - provider: diff
```

**Key settings for limited context:**
- `contextLength`: Set to match your model's context window
- `maxTokens`: Smaller output leaves more room for input context
- Minimal context providers (code + diff only) to avoid exceeding context limit

**For larger context windows (16k+)**, you can enable more providers:
```yaml
context:
  - provider: code
  - provider: docs
  - provider: diff
  - provider: terminal
  - provider: problems
  - provider: folder
  - provider: codebase
```

**Use `@codebase` manually** to search RAG when needed, rather than loading it automatically.

### Reasoning Models (Qwen3, DeepSeek R1)

Models with thinking/reasoning capabilities (like Qwen3 and DeepSeek R1) output their reasoning in `<think>...</think>` blocks. Posterchanai automatically filters these blocks during streaming, so users only see the final response. The thinking content is stripped in real-time before being sent to the frontend.

## Installation

### Quick Start (Recommended)

```bash
# Clone the repository
git clone <repo-url>
cd posterchanai

# Run the interactive installer
./install.sh
```

The installer will:
- Detect your GPU (Intel Arc, NVIDIA, AMD, or CPU)
- Install the correct llama-cpp-python backend
- Set up a Python virtual environment
- Configure and start a systemd service
- Optionally download a starter model

To see required packages for your distro before installing:
```bash
./install.sh --packages
```

### Upgrading

To upgrade an existing installation:

```bash
cd posterchanai
git pull
sudo systemctl restart posterchanai
```

**Automatic migrations:** New settings are automatically added to the database when the app starts. No manual migration is needed.

**Manual migration (optional):** If you want to verify or run migrations without starting the app:

```bash
source venv/bin/activate  # or venv-ipex
python scripts/migrate.py
```

### Manual Setup

If you prefer manual control:

```bash
# Create virtual environment and install base dependencies
./setup.sh

# For GPU acceleration, manually install llama-cpp-python:

# Intel Arc (use venv-ipex for Python 3.11):
source /opt/intel/oneapi/2025.0/oneapi-vars.sh  # or 2024.2
CMAKE_ARGS="-DGGML_SYCL=ON -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
    pip install llama-cpp-python --force-reinstall --no-cache-dir

# NVIDIA:
CMAKE_ARGS="-DGGML_CUDA=ON" pip install llama-cpp-python --force-reinstall --no-cache-dir

# AMD (ROCm):
CMAKE_ARGS="-DGGML_HIP=ON" pip install llama-cpp-python --force-reinstall --no-cache-dir

# CPU only:
pip install llama-cpp-python
```

**Important for Intel Arc:** When running manually (not via systemd), always source oneAPI first:
```bash
source /opt/intel/oneapi/2025.0/oneapi-vars.sh
source venv-ipex/bin/activate
python run.py
```

Or use the wrapper script which handles this automatically:
```bash
./run-ipex.sh
```

## Running

### Development

```bash
source venv/bin/activate
python run.py
```

### Production (systemd)

If you used `install.sh`, the service is already configured. Otherwise:

```bash
# Copy service file
sudo cp posterchanai.service /etc/systemd/system/

# Edit the service file to match your installation path and user
sudo nano /etc/systemd/system/posterchanai.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable posterchanai
sudo systemctl start posterchanai

# Check status
sudo systemctl status posterchanai
```

## Configuration

Access the admin panel at `http://localhost:3051/admin`

### AI Settings (Ollama)

- **Ollama URL**: URL to your Ollama instance (e.g., `http://localhost:11434`)
- **Default Model**: Model name to use for chat
- **Timeout**: Request timeout in milliseconds
- **Max Concurrent**: Maximum concurrent requests to Ollama
- **System Prompt**: Default system prompt for the AI

### Advanced Model Settings

These settings apply to both Native and Ollama backends:

- **Temperature**: Controls randomness (0.0 - 2.0)
- **Top P**: Nucleus sampling threshold
- **Top K**: Top-k sampling
- **Repeat Penalty**: Penalty for repeated tokens
- **Context Length**: Maximum context window size
- **Max Tokens**: Maximum tokens to generate
- **Mirostat**: Mirostat sampling mode (0=disabled, 1=v1, 2=v2)

Ollama-only settings:
- **Keep Alive**: How long to keep model in memory (-1 = forever)
- **Max Concurrent**: Maximum concurrent requests to Ollama
- **TFS-Z**: Tail-free sampling parameter

### Optional Services

- **SearXNG URL**: URL to SearXNG instance for web search
- **Upload Path**: Directory to store uploads (default: `/var/lib/posterchanai`)
- **TTS Settings**: Voice, rate, and pitch for text-to-speech

### Image Generation Settings

Posterchanai supports two image generation backends:

#### Native Diffusers Backend (Recommended for dedicated image servers)

Direct GPU image generation using the diffusers library. Supports SDXL models.

| Setting | Description |
|---------|-------------|
| `image_backend` | Set to `native` for direct GPU generation |
| `image_model_path` | Path to SDXL checkpoint (.safetensors) |
| `image_anime_model_path` | Path to anime-style model (auto-selected for anime prompts) |
| `image_gpu_device` | GPU backend: `cuda` (NVIDIA), `xpu` (Intel), `rocm` (AMD), `cpu` |
| `image_width` | Default image width (default: 1024) |
| `image_height` | Default image height (default: 1024) |
| `image_steps` | Sampling steps (default: 25) |
| `image_cfg` | CFG scale (default: 7.0) |

**VRAM Management:**

| Setting | Description |
|---------|-------------|
| `vram_mode` | Memory management mode (see below) |
| `image_idle_timeout` | Seconds before auto-unloading image model (default: 120, 0=disabled) |

**Image Idle Timeout:**

The image model automatically unloads after the configured idle timeout to free VRAM. This is useful for:
- Shared VRAM setups where LLM and image model compete for memory
- Preventing OOM errors from fragmented GPU memory
- Reducing power consumption when not generating images

Set to `0` to keep the image model loaded permanently (use with dedicated image servers).

VRAM modes:
- `shared` - LLM and image model share VRAM, swap as needed (default)
- `dedicated` - Keep both models loaded (requires high VRAM or dual GPU)
- `llm_only` - Keep LLM loaded, use external service for images
- `image_only` - Keep image model loaded, use external service for LLM

**Notes:**
- When using `image_only`, the LLM health check is automatically disabled
- Sequential processing: Only one image is generated at a time to prevent GPU overload
- **GPU Resource Serialization**: LLM and image generation requests are automatically serialized per node. Only one type (LLM or image) runs at a time on each node to prevent GPU RAM exhaustion, even with load balancing across multiple nodes
- CUDA memory fragmentation is handled automatically with `PYTORCH_CUDA_ALLOC_CONF`

#### ComfyUI/External Backend (Recommended for proxy setups)

Connects to an external image generation server (ComfyUI or another posterchanai instance).

| Setting | Description |
|---------|-------------|
| `image_backend` | Set to `comfyui` for external backend |
| `comfyui_url` | URL to ComfyUI or posterchanai (e.g., `http://nas.lan:3051`) |
| `comfyui_timeout` | Request timeout in milliseconds |
| `comfyui_default_model` | Default checkpoint name (for ComfyUI workflow fallback) |
| `comfyui_anime_model` | Anime checkpoint name (for ComfyUI workflow fallback) |

**How it works:**
1. First tries posterchanai's REST API (`/api/generate-image`, `/api/img2img`)
2. Falls back to ComfyUI's workflow API if REST API unavailable
3. This allows proxying to a dedicated posterchanai image server

#### Distributed Setup Example

For setups with a dedicated image generation server:

**Image Server (nas.lan with NVIDIA GPU):**
```
image_backend: native
image_gpu_device: cuda
image_model_path: /path/to/sdxl_model.safetensors
image_idle_timeout: 120      # Unload after 2 min idle to free VRAM
vram_mode: image_only
ollama_ping_enabled: false   # No LLM health check needed
```

**LLM Server (router.lan with Intel Arc):**
```
llm_backend: ipex
image_backend: comfyui
comfyui_url: http://nas.lan:3051
vram_mode: llm_only
ollama_ping_enabled: true    # LLM health check enabled
gpu_memory_check_enabled: true
gpu_memory_threshold: 95
gpu_type: intel
```

**How it works:**
- Image requests from the LLM server are proxied to the image server via REST API
- Each server only loads the model it's responsible for
- Health checks only monitor the local model type
- Sequential image generation prevents GPU overload on the image server

#### Load Balancing

Posterchanai supports round-robin load balancing for both chat and image generation across multiple servers.

**Admin > Site Settings > Load Balancing:**
- `Chat Server URLs` - Comma-separated list of posterchanai servers for LLM/chat requests
- `Image Server URLs` - Comma-separated list of posterchanai servers for image generation

**How it works:**
- All configured servers receive requests in round-robin order (50/50 split per server)
- Health checks skip unresponsive servers (re-checked after 30 seconds)
- **Local URLs use local inference directly** (no HTTP loop) - if the selected server is "self", the request is processed locally using the GPU
- Remote URLs are called via HTTP
- **GPU Resource Locking**: On each node, LLM and image generation requests are automatically serialized to prevent GPU RAM from being maxed out. Only one type (LLM or image) runs at a time per node, ensuring stable operation even under high load

**Example multi-server setup:**
```
Chat Server URLs: http://192.168.0.1:3051,http://192.168.0.85:3051
Image Server URLs: http://192.168.0.1:3052,http://192.168.0.85:3051
```

With 2 servers: 50% local (uses local GPU), 50% remote (HTTP to other server).
With 3 servers: ~33% each, with local URLs using direct GPU inference.

#### Intel Arc Dual-Instance Setup

Intel Arc GPUs can run both LLM (via IPEX-LLM) and image generation (via PyTorch XPU), but they require different Python environments. The solution is to run two instances:

| Instance | Port | Environment | Purpose |
|----------|------|-------------|---------|
| Main (IPEX-LLM) | 3051 | venv-ipex | Chat/LLM |
| Image (XPU) | 3052 | venv-xpu | Image generation |

**Setup:**
```bash
# Run the setup script
./scripts/setup-image-instance.sh

# Install and start the image service
cp posterchanai-xpu-image.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now posterchanai-xpu-image
```

**Configure main instance (Admin > Site Settings):**
- Image Server URLs: `http://localhost:3052`

**How it works:**
- Main instance handles chat on Intel Arc via IPEX-LLM
- Image instance handles image generation on Intel Arc via PyTorch XPU
- Separate databases prevent conflicts
- Main instance forwards all image requests to port 3052

### Image Generation REST API

Posterchanai provides REST endpoints for external integrations (e.g., Sharkey/Misskey).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/generate-image` | POST | Text-to-image generation |
| `/api/img2img` | POST | Image-to-image transformation |
| `/api/tag-image` | POST | WD14 image tagging (returns comma-separated tags) |

**Authentication:**
- Set `IMAGE_API_KEY` environment variable for API key auth
- Use `X-API-Key` header or `Bearer` token
- If no API key is set, endpoints are open (for internal network use)

**Example request:**
```bash
curl -X POST http://localhost:3051/api/generate-image \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{"prompt": "a cat sitting on a couch", "negative_prompt": "blurry"}'
```

**Response:**
```json
{"image": "base64-encoded-png-data"}
```

**Tag Image Example:**
```bash
curl -X POST http://localhost:3051/api/tag-image \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{"image": "base64-encoded-image", "threshold": 0.35}'
```

**Response:**
```json
{"tags": "1girl, solo, long hair, blonde hair, blue eyes, dress, standing, outdoors"}
```

The WD14 tagger uses the SmilingWolf/wd-v1-4-moat-tagger-v2 model via ONNX runtime. The model is automatically downloaded on first use.

### Email Settings (SMTP/IMAP)

- **SMTP**: Configure for sending emails
- **IMAP**: Configure for saving sent emails to Sent folder

### Site Settings

- **Allow Registration**: Enable/disable user registration
- **Ollama Health Check**: Auto-ping Ollama and restart if unresponsive

## OpenAI-Compatible API

Posterchanai provides an OpenAI-compatible API for external applications.

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | POST | Chat completion (streaming supported) |
| `/v1/models` | GET | List available models |
| `/api/chat/completions` | POST | Alternative endpoint |
| `/chat/completions` | POST | Root-level endpoint |

### Authentication

Use your API key in the Authorization header:

```bash
curl http://localhost:3051/v1/chat/completions \
  -H "Authorization: Bearer sk-your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'
```

### Using with OpenAI Python Client

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:3051/v1",
    api_key="sk-your-api-key"
)

response = client.chat.completions.create(
    model="llama3",
    messages=[{"role": "user", "content": "Hello!"}]
)
print(response.choices[0].message.content)
```

### API Keys

Users can generate their own API keys from the user menu. Keys are shown once on creation - save them securely.

## Commands

Type these commands in the chat (or use the mode buttons):

| Command | Description |
|---------|-------------|
| `search <query>` | Search the web and get AI-summarized results |
| `images <query>` | Search for images |
| `geni <prompt>` | Generate an AI image from your prompt |
| `img2img <prompt>` | Transform an uploaded image with your prompt |
| `regen` | Regenerate the last image with a new seed |
| `yt <url>` | Summarize a YouTube video transcript |
| `torrents` | Built-in torrent client: browse, download, pause, resume, delete |
| `nyaa <query>` | Search nyaa.si for anime torrents |
| `budget` | Budget manager (summary, bills, add, pay) |
| `firewall` | Firewall status and log search |
| `cal` | Calendar: today's events, week view, add events (aliases: sched, schedule) |
| `contacts` | Search or add CardDAV contacts (with clickable phone/email links) |
| `mail` | Email: inbox, folders, folder, read, reply, forward, archive, delete, send to contacts |
| `music` | WebDAV music streaming: browse, search, play, queue, mood playlists |
| `todo` | CalDAV task management: list, add, remove tasks |
| `news` | Fetch and summarize RSS feeds (alias for rss sync) |
| `rss` | Native RSS: list feeds, sync, add/remove (plugin) |
| `logs` | System logs analysis (admin only) |

## Voice Input

Click the microphone button to speak commands naturally. Voice input works in Chrome/Edge (Web Speech API) and falls back to local Whisper transcription in Brave/Firefox.

### Voice Command Examples

**Email:**
- "check my mail" / "inbox" / "messages"
- "read 2" - Read email #2
- "delete 3" - Delete email #3
- "archive 1" - Archive email #1
- "reply thanks for the update" - Reply to last read email
- "delete this" / "archive this" - Act on last read email
- "summarize this" - AI summary of last read email
- "translate this to spanish" - Translate last read email

**Calendar & Tasks:**
- "calendar" / "my schedule"
- "this week"
- "add event dinner tomorrow at 7pm"
- "my todos" / "todo list"
- "add todo buy groceries"
- "remind me to call mom"

**Music:**
- "play music" - Shuffle play
- "play 3" - Play track #3
- "next" / "skip"
- "previous" / "back"
- "stop" / "pause"
- "play something relaxing"

**Torrents:**
- "torrents" / "my torrents"

- "movies" / "tv" / "anime"
- "download movie 3"
- "download tv 5"
- "pause 1" / "resume 1"

**Search & Images:**
- "search best pizza recipe"
- "google python tutorials"
- "images of mountains"
- "generate image of a sunset"

**Budget:**
- "budget" / "my bills"
- "add bill rent 500"
- "add bill groceries for $200"

**Other:**
- "news"
- "contacts" / "who is john"
- "translate to french" - Translate last AI response
- "help"

### Whisper Fallback (for Brave/Firefox)

Browsers that block Google's Web Speech API (like Brave) automatically use local Whisper transcription. Install on server:

```bash
pip install faster-whisper
```

First use downloads the model (~150MB). Requires HTTPS for microphone access.

### Contacts Command

The contacts command provides CardDAV address book functionality:

| Subcommand | Description |
|------------|-------------|
| `contacts all` | List all contacts |
| `contacts <query>` | Search contacts by name, email, or phone |
| `contacts add <name> <phone>` | Add a new contact with phone number |

**Examples:**
- `contacts all` - List all contacts
- `contacts john` - Search for contacts named John
- `contacts add "John Doe" 555-1234` - Add a new contact (quotes for names with spaces)

Configure CardDAV in User Settings > Calendar & Contacts tab.

### Mail Command

The mail command provides full IMAP/SMTP email functionality with encrypted password storage:

| Subcommand | Description |
|------------|-------------|
| `mail` | Show recent inbox messages from all accounts |
| `mail unread` | Show unread messages only |
| `mail folders <account>` | List all IMAP folders with browse buttons |
| `mail folder <account> <folder>` | Browse messages in a specific folder |
| `mail sum <account>` | AI summary of all inbox messages |
| `mail search <account> <query>` | Search messages by from, to, or subject |
| `mail read <account> [folder:]<id>` | Read a specific message |
| `mail summary <account> [folder:]<id>` | AI summary of a message with key points |
| `mail translate <account> [folder:]<id>` | Translate a message to English |
| `mail reply <account> [folder:]<id> <message>` | Reply to a message |
| `mail forward <account> [folder:]<id> <recipient> [message]` | Forward a message |
| `mail send [account] <recipient> <message>` | Send new email |
| `mail archive <account> <id>` | Archive a message (moves to INBOX.Archive) |
| `mail delete <account> [folder:]<id>` | Delete a message |
| `mail deleteall <account>` | Delete ALL messages in an account's inbox |

**Examples:**
- `mail` - Show inbox from all accounts
- `mail folders work` - List all folders for work account (with browse buttons)
- `mail folder work INBOX.Sent` - Browse sent messages
- `mail sum work` - AI summary of work inbox
- `mail search work invoice` - Search for "invoice" in work account
- `mail read work 5` - Read message #5 from work@... account
- `mail read work INBOX.Archive:123` - Read archived message #123
- `mail send john Hey!` - Send email to John (uses first account)
- `mail send work john Hey!` - Send from work account to John
- `mail reply work INBOX:456 Thanks!` - Reply to message #456
- `mail forward work 789 john@example.com` - Forward message #789 to John
- `mail forward work 789 john@example.com Check this out!` - Forward with custom message

**Tab Autocomplete:** Type `mail folders ` or `mail folder ` and press Tab to see available accounts, then Tab again for folder hints.

**Security:** Passwords are encrypted at rest using Fernet. SSRF protection blocks connections to internal IPs.

Configure email accounts in User Settings > Mail tab.

### Music Command

The music command provides WebDAV-based music streaming with a cyberpunk-styled player featuring a Web Audio API visualizer.

| Subcommand | Description |
|------------|-------------|
| `music` | Browse music library root |
| `music browse <path>` | Browse a specific folder |
| `music search <query>` | Search tracks by filename |
| `music play <#>` | Play track number from last results |
| `music queue` | Show current playback queue |
| `music queue add <#>` | Add track to queue |
| `music mood <vibe>` | AI-curated playlist based on mood |
| `music stop` | Stop playback |
| `music next` | Skip to next track |
| `music prev` | Previous track or restart current |

**Examples:**
- `music` - Browse library root
- `music browse /Jazz` - Browse Jazz folder
- `music search coltrane` - Search for Coltrane tracks
- `music play 3` - Play track #3 from results
- `music mood chill` - AI picks relaxing tracks

**Tab Autocomplete:** Type `music ` and press Tab for subcommands, `music mood ` for mood suggestions.

**Player Features:**
- Cyberpunk-styled floating player with glow effects
- Web Audio API visualizer (frequency bars with cyan-magenta gradient)
- Controls: play/pause, prev/next, progress bar, volume
- Queue management with drag-to-reorder
- Collapsible design to minimize screen space

**Setup:**
1. Go to User Settings > Music tab
2. Enter your WebDAV URL (e.g., `https://cloud.example.com/remote.php/dav/files/user/Music`)
3. Enter username and password
4. Click "Test Connection" to verify
5. Save settings

Works with Nextcloud, ownCloud, or any WebDAV-compatible server.

**Global Keyboard Shortcuts (Wayland):**

The web interface supports global shortcuts via the Media Session API and keyboard shortcuts:

**Automatic (Media Session API):**
- System media keys (Play/Pause, Next, Previous) work automatically when music is playing
- Works with most Wayland compositors (Hyprland, Sway, KDE, GNOME)
- No configuration needed - just use your keyboard/media keys when the browser tab is active

**Hyprland Configuration:**

See **[docs/HYPRLAND_MUSIC_CONTROLS.md](docs/HYPRLAND_MUSIC_CONTROLS.md)** for detailed setup instructions.

**Quick Setup (Automatic):**

Run the setup script to automatically add keybinds:

```bash
./scripts/setup-hyprland-keybinds.sh
```

This will add Alt+P, Alt+F, Alt+R, Alt+S shortcuts to your `~/.config/hypr/hyprland.conf`.

**Manual Setup:**

Or manually add to `~/.config/hypr/hyprland.conf`:

```bash
# Custom keyboard shortcuts (Alt+P, Alt+F, Alt+R, Alt+S)
bind = ALT, P, exec, /home/verita84/posterchanai/scripts/hyprland-music-control.sh toggle
bind = ALT, F, exec, /home/verita84/posterchanai/scripts/hyprland-music-control.sh next
bind = ALT, R, exec, /home/verita84/posterchanai/scripts/hyprland-music-control.sh prev
bind = ALT, S, exec, /home/verita84/posterchanai/scripts/hyprland-music-control.sh stop
```

**Note:** Adjust the path `/home/verita84/posterchanai` to match your installation directory.

**Alternative shortcuts:**
- Use `SUPER` instead of `ALT` if you prefer: `bind = SUPER, P, ...`
- Or use media keys if your keyboard has them: `bind = , XF86AudioPlay, ...`

**Simple Setup (Browser Focus Only):**

If you just want to focus the browser and use media keys:

```bash
# Focus browser and let Media Session API handle media keys
bind = , XF86AudioPlay, exec, hyprctl dispatch focuswindow "class:.*[Bb]rowser.*"
bind = , XF86AudioNext, exec, hyprctl dispatch focuswindow "class:.*[Bb]rowser.*"
bind = , XF86AudioPrev, exec, hyprctl dispatch focuswindow "class:.*[Bb]rowser.*"
```

**Advanced Setup (Global Control):**

For true global control that works even when browser isn't focused, use `ydotool` or `xdotool` to send keyboard events. See the full guide in `docs/HYPRLAND_MUSIC_CONTROLS.md`.

**Testing Your Setup:**

Use the testing script to verify everything works:

```bash
# Run all tests
./scripts/test-music-controls.sh --all

# Interactive menu
./scripts/test-music-controls.sh

# See help
./scripts/test-music-controls.sh --help
```

See `docs/TESTING_MUSIC_CONTROLS.md` for detailed testing instructions.

### Intelligent Actions

The intelligent action system automatically detects user intent from natural language and executes the appropriate command. No need to memorize exact syntax - just describe what you want.

**How it works:**
1. AI analyzes your message to detect if you want to perform an action
2. Extracts relevant data (dates, names, contacts, etc.) from your message and any pasted content
3. Executes the action automatically if confidence is high enough
4. Falls back to regular chat if no action is detected

**Example Usage:**

| What You Say | What Happens |
|--------------|--------------|
| "Add dentist appointment Friday at 2pm" | Creates calendar event |
| "Schedule team standup every weekday at 9am" | Creates recurring event |
| *[Paste email invite]* "Add this to my calendar" | Parses email, creates event with all details |
| "Save John's number: 555-123-4567" | Creates contact |
| *[Paste business card image]* "Save this contact" | OCR extracts info, creates contact |
| "Remind me to buy groceries" | Adds todo item |
| "I need to call the bank tomorrow" | Adds todo item |
| "Check my emails" | Shows inbox |
| "Email john@example.com saying I'll be late" | Sends email |
| "Play something relaxing" | Plays mood-based playlist |
| "Search for the latest AI news" | Web search with summary |
| "Create a picture of a sunset over mountains" | Generates image |
| "Summarize this video: [YouTube URL]" | Transcribes and summarizes |
| "Translate that to Spanish" | Translates last response |

**Configuration (Admin > AI Settings > Intelligent Actions):**

| Setting | Default | Description |
|---------|---------|-------------|
| `intent_detection_enabled` | true | Enable/disable intent detection |
| `intent_confidence_threshold` | 0.7 | Minimum confidence (0.0-1.0) to execute actions |

**Tips:**
- Higher confidence threshold = fewer false positives, more explicit commands needed
- Lower threshold = more actions detected, but may misinterpret some messages
- Paste email content, meeting notes, or business cards for automatic data extraction
- Works with OCR - upload images of documents and ask to extract/save the data

### Torrents Command

Built-in BitTorrent client with **required Tor proxy support** for anonymous downloading. Browse torrent sites, download, and manage torrents directly from chat. **All torrent and nyaa searches require HTTP proxy to Tor.**

| Subcommand | Description |
|------------|-------------|
| `torrents` | Browse all torrent categories |
| `torrents movies` | Browse movie torrents |
| `torrents tv` | Browse TV show torrents |
| `torrents anime` | Browse anime torrents |
| `torrents games` | Browse game torrents |
| `torrents list` | Show active/completed downloads |
| `torrents download <#>` | Download torrent from browse results |
| `torrents pause <#>` | Pause a download |
| `torrents resume <#>` | Resume a paused download |
| `torrents rm <#>` | Remove a torrent (keeps files) |
| `torrents info <#>` | Show detailed torrent info |
| `nyaa <query>` | Search nyaa.si for anime |
| `nyaa download <#>` | Download from nyaa search results |

**Examples:**
- `torrents` - Browse all categories
- `torrents movies` - Browse latest movies
- `torrents download 3` - Download torrent #3 from results
- `torrents list` - Show all downloads with progress
- `torrents pause 1` - Pause download #1
- `nyaa one piece` - Search for One Piece on nyaa.si
- `nyaa download 2` - Download result #2

- **Tor proxy required** - All requests MUST be routed through HTTP proxy over Tor (configure in Admin Settings)
- **Image thumbnails** - Displays thread images in results
- **Clickable links** - Visit threads directly from results
- **Thread stats** - Shows reply and image counts

**Setup:**
1. Configure HTTP proxy in Admin → Site Settings → BitTorrent Client → HTTP Proxy Host
2. Or enable built-in HTTP proxy in Admin → Site Settings → Built-in HTTP Proxy

**Features:**
- **Tor proxy required** - All torrent/nyaa requests MUST be routed through HTTP proxy over Tor (configure in Admin Settings)
- **Progress tracking** - Real-time download/upload speeds and progress bars
- **Seeding** - Continues seeding after download completes
- **Category browsing** - Movies, TV, Anime, Games, and more
- **Nyaa integration** - Direct anime torrent search (requires proxy)

**Setup:**

1. Install libtorrent with Python bindings (see Requirements section)
2. Enable in Admin > Services > BitTorrent Client
3. Configure download path and optional proxy

**Admin Settings (Admin > Services > BitTorrent):**

| Setting | Description |
|---------|-------------|
| `bt_enabled` | Enable/disable torrent client |
| `bt_download_path` | Where to save downloaded files |
| `bt_proxy_url` | HTTP proxy URL (e.g., `http://127.0.0.1:8118` for Privoxy/Tor) |
| `torrent_site_url` | TorrentGalaxy or compatible site URL |

### Todo Command

The todo command provides CalDAV-integrated task management (VTODO items).

| Subcommand | Description |
|------------|-------------|
| `todo` | List all tasks from configured calendars |
| `todo list` | Same as `todo` |
| `todo add <task>` | Add a new task to the first calendar |
| `todo rm <#>` | Remove task by number |

**Examples:**
- `todo` - List all tasks
- `todo add Buy groceries` - Add a new task
- `todo rm 1` - Remove task #1

**Tab Autocomplete:** Type `todo ` and press Tab for subcommands.

Configure CalDAV calendars in User Settings > Calendar & Contacts tab. Tasks are stored as VTODO items.

### RSS Plugin (Native RSS)

A self-contained RSS feed reader plugin with AI-powered article summarization.

**Features:**
- Subscribe to RSS/Atom feeds per user
- Automatic fetching every 30 minutes
- AI-generated summaries stored in "RSS News" conversation
- OPML import from other RSS readers

**Setup:**
1. Enable in Admin → Services → Native RSS
2. Restart the service
3. Users enable RSS in User Settings → News & RSS
4. Add feed URLs or import OPML file

**Commands:**

| Subcommand | Description |
|------------|-------------|
| `rss` | List your RSS feeds |
| `rss sync` | Manually fetch and summarize articles |
| `rss add <url> [name]` | Add a new feed |
| `rss remove <id>` | Remove a feed by ID |
| `rss search <query>` | Search old articles |

**Examples:**
- `rss` - List all feeds with status
- `rss add https://news.ycombinator.com/rss Hacker News` - Add feed
- `rss sync` - Fetch articles now
- `rss remove 3` - Remove feed #3
- `rss search python` - Search for articles containing "python"

**OPML Import/Export:**
1. Export OPML from your current RSS reader (Feedly, Inoreader, etc.)
2. Go to User Settings → News & RSS
3. Click "Import OPML" and select your file
4. Feeds are imported with folder names preserved
5. Click "Export OPML" to download your feeds for backup or migration

The plugin is located in `plugins/rss/` and can be customized independently.

### Edit Image (img2img) with Face Swap

The `img2img` command transforms uploaded images while preserving the subject's identity through:

1. **WD14 Tagging** - Automatically detects identity features (skin tone, body type, etc.)
2. **Identity Preservation** - Adds detected features to the prompt (e.g., "dark brown skin", "plus-size body")
3. **NSFW Model Selection** - Automatically uses the anime/NSFW model for nude-related prompts
4. **Face Swap** - Pastes the original face onto the generated image using InsightFace

**Example usage:**
```
img2img nude
img2img wearing a red dress
img2img anime style
```

**How it works:**
1. WD14 tagger extracts identity tags from the original image
2. Tags like `dark_skin`, `fat`, `large_breasts` are added to preserve identity
3. NSFW keywords trigger the uncensored model automatically
4. After generation, InsightFace detects faces and swaps the original face back

**Dependencies:**
- `onnxruntime` - Required for WD14 tagging and InsightFace
- `insightface` - Face detection and swapping
- `opencv-python-headless` - Image processing for face swap
- `mkl` - Intel Math Kernel Library (required for InsightFace on Intel systems)

**Note for Intel systems:** The MKL library path must be in `LD_LIBRARY_PATH` for face detection to work.
If using a virtual environment, ensure `$VENV/lib` is included:
```bash
export LD_LIBRARY_PATH=/path/to/venv/lib:$LD_LIBRARY_PATH
```
For systemd services, add the path to the Environment directive in the service file.

### Regen Auto-Trainer

The img2img/regen system automatically logs successful transformations for LLM few-shot training. This improves future regen accuracy over time.

**How it works:**
1. Every successful regen auto-logs source tags (via WD14) + modification prompt to `regen_log.json`
2. Examples accumulate until you sync
3. Run `sync` to batch-write all pending examples to training files

**Commands:**
```bash
# See pending examples
python3 regen_trainer.py list

# Write all pending to training files
python3 regen_trainer.py sync

# Clear pending without writing (discard)
python3 regen_trainer.py clear
```

**Training file updated:**
- `app/services/chat_service.py` - Few-shot examples in the regen prompt

Run `sync` periodically (e.g., weekly) to incorporate successful regen patterns into the LLM's training examples. Restart service after sync to apply changes.

## Browser Search Engine Integration

You can use Poster-chan AI as your browser's default search engine. This allows you to search directly from the address bar.

### Setup

Add a custom search engine in your browser with this URL:

```
https://your-domain.com/?q=%s
```

**Chrome/Brave:**
1. Go to Settings > Search engine > Manage search engines
2. Click "Add" under "Site search"
3. Name: `Poster-chan AI`
4. Shortcut: `ai` (or whatever you prefer)
5. URL: `https://your-domain.com/?q=%s`

**Firefox:**
1. Install the "Add custom search engine" extension
2. Add your search URL

### How it works

When you search using the custom search engine:
1. Creates a new conversation automatically
2. Activates Search mode
3. Executes your query with AI-summarized web results
4. URL parameter is cleared (refresh won't repeat the search)

## Document Translation

The Translate button allows you to translate entire documents to different languages.

### Supported formats
- Images (text extracted via OCR)
- PDF documents
- Text files (.txt, .md)
- Word documents (.docx)

### Usage
1. Click the **Translate** button
2. Select target language from dropdown
3. Choose a file to translate
4. AI will translate the full document

## Custom AI Service

Users can connect to their own AI service running on their desktop or home server, allowing them to use their personal LLM and ComfyUI instances instead of the server's default.

### Supported Services

**LLM (Chat):**
- **Ollama** - Direct Ollama API (`/api/chat`)
- **Open-WebUI** - OpenAI-compatible API (`/v1/chat/completions`)
- **Posterchanai** - OpenAI-compatible API (`/v1/chat/completions`)

**Image Generation:**
- **ComfyUI** - User's own ComfyUI instance

### Setup

1. Click your username in the sidebar
2. Click **Settings**
3. In the **Custom AI Service** section:

**For Chat/LLM:**
- Enable **Use Custom LLM**
- Select **Service Type** (Ollama or Open-WebUI/Posterchanai)
- Enter **Service URL** (e.g., `http://192.168.1.100:11434` for Ollama)
- Enter **Model Name** (e.g., `llama3:latest` for Ollama, or your model ID for Open-WebUI)
- Optionally enter **API Key** (required for Open-WebUI/Posterchanai)
- Click **Test Connection** to verify

**For Image Generation:**
- Enable **Use Custom ComfyUI**
- Enter **ComfyUI URL** (e.g., `http://192.168.1.100:8188`)
- Click **Test Connection** to verify

4. Click **Save All Settings**

### Quick Toggle

Once configured, a quick toggle appears in the user menu to switch between:
- **Server AI** - Uses the server's default AI service
- **Custom AI** - Uses your personal AI service

This allows one-click switching without opening settings.

### How It Works

- Chat requests are routed to your custom LLM endpoint
- Image generation requests are routed to your custom ComfyUI
- **All uploads and generated images remain stored on the main server**
- Your conversation history stays on the main server
- If your custom service is unavailable, you'll see an error message

### Security Notes

- Your API key is stored encrypted on the server
- The server only proxies requests to your custom endpoint
- No data is shared with the custom service except the chat messages/prompts

## Email AI Responses

You can email any AI response to your configured email address.

### Setup
1. Click your username in the sidebar
2. Click **Settings**
3. Enter your notification email
4. Click **Save Settings**

### Usage
- Click the email button (envelope icon) on any AI message
- The response will be sent to your configured email

**Note:** Requires SMTP to be configured in Admin settings.

## Supported File Types

| Type | Extensions | Description |
|------|------------|-------------|
| Images | jpg, png, gif, webp, **heic, heif** | OCR text extraction + vision AI |
| PDF | pdf | Text extracted and sent to AI |
| Word | docx, doc | Text and tables extracted |
| Excel | xlsx, xls | Spreadsheet data extracted |
| PowerPoint | pptx, ppt | Slide text extracted |
| Text | txt, md, json, py, js, etc. | Sent directly to AI |

### OCR (Optical Character Recognition)

Images containing text are automatically processed with Tesseract OCR:
- Extracts text from photos of documents, receipts, screenshots
- Handles EXIF orientation (phone photos rotated correctly)
- Large images automatically resized for processing
- Works with all supported image formats including HEIC

## File Storage

Uploads and generated images are stored at the configured `upload_path`:
- `/var/lib/posterchanai/<username>/<conversation_id>/`
- Files are automatically deleted when conversations are deleted

## API Endpoints

### Authentication
- `POST /api/auth/login` - Login
- `POST /api/auth/register` - Register (if enabled)
- `POST /api/auth/logout` - Logout
- `GET /api/auth/me` - Get current user
- `GET /api/auth/api-keys` - List user's API keys
- `POST /api/auth/api-keys` - Create new API key
- `DELETE /api/auth/api-keys/{id}` - Delete API key

### Conversations
- `GET /api/conversations` - List conversations
- `POST /api/conversations` - Create conversation
- `GET /api/conversations/{id}` - Get conversation with messages
- `DELETE /api/conversations/{id}` - Delete conversation
- `DELETE /api/conversations` - Delete all conversations

### WebSocket
- `WS /api/ws/chat/{conversation_id}` - Real-time chat

### Admin
- `GET /api/admin/settings` - Get settings
- `PUT /api/admin/settings` - Update settings
- `GET /api/admin/users` - List users
- `POST /api/admin/users` - Create user
- `DELETE /api/admin/users/{id}` - Delete user
- `POST /api/admin/test-email` - Send test email

## Nginx Reverse Proxy

Example nginx configuration for running behind a reverse proxy with SSL:

```nginx
upstream posterchanai {
    server 127.0.0.1:3051;
}

server {
    listen 80;
    server_name ai.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name ai.example.com;

    ssl_certificate /etc/letsencrypt/live/ai.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ai.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    client_max_body_size 100M;

    location / {
        proxy_pass http://posterchanai;
        proxy_http_version 1.1;
        proxy_redirect off;

        # WebSocket support (required for chat)
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Standard proxy headers
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $http_host;
        proxy_set_header X-Forwarded-Port 443;

        # Disable buffering for streaming responses
        proxy_buffering off;
        chunked_transfer_encoding off;

        # Long timeouts for AI generation
        proxy_connect_timeout 3600s;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        send_timeout 3600s;
    }
}
```

Key settings for WebSocket and streaming:
- `proxy_http_version 1.1` - Required for WebSocket
- `proxy_set_header Upgrade $http_upgrade` - WebSocket upgrade header
- `proxy_set_header Connection "upgrade"` - WebSocket connection header
- `proxy_buffering off` - Disable buffering for SSE streaming
- Long timeouts for AI generation requests

## LLM Health Check

Posterchanai includes an automatic health check that monitors the LLM backend and recovers if unresponsive. Works with all backends: Native GPU, IPEX-LLM, and Ollama.

**Important:** The LLM health check is automatically disabled when `vram_mode` is set to `image_only`, since there's no local LLM to monitor in that configuration.

### Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `ollama_ping_enabled` | false | Enable/disable health check |
| `ollama_ping_interval` | 120 | Seconds between health checks |
| `ollama_restart_after_failures` | 2 | Consecutive failures before recovery |
| `ollama_restart_command` | `sudo systemctl restart ollama` | Command to restart Ollama (Ollama backend only) |

### GPU VRAM Monitoring

The health check can also monitor GPU memory usage and automatically reload the model when VRAM exceeds a threshold. This prevents out-of-memory crashes from memory fragmentation over time.

| Setting | Default | Description |
|---------|---------|-------------|
| `gpu_memory_check_enabled` | false | Enable GPU VRAM monitoring |
| `gpu_memory_threshold` | 99 | VRAM usage percentage to trigger reload |
| `gpu_type` | nvidia | GPU type: `nvidia` or `intel` |

**Supported GPUs:**
- **NVIDIA**: Uses `nvidia-smi` to query memory
- **Intel Arc**: Uses debugfs via sudo helper script (`scripts/gpu_memory.sh`)

**Intel Arc Setup:**

For Intel Arc GPUs, the helper script needs passwordless sudo access:

```bash
# Add to sudoers (run once)
echo 'youruser ALL=(root) NOPASSWD: /path/to/posterchanai/scripts/gpu_memory.sh' | \
    sudo tee /etc/sudoers.d/gpu_memory
sudo chmod 440 /etc/sudoers.d/gpu_memory
```

### How it works

1. Checks if the LLM backend is responsive every 120 seconds
   - **Native/IPEX**: Verifies model is loaded in memory
   - **Ollama**: Pings the Ollama API
2. If check fails, increments failure counter
3. After consecutive failures, triggers recovery:
   - **Native/IPEX**: Reloads the model in-process
   - **Ollama**: Executes the configured restart command
4. If GPU memory monitoring is enabled and VRAM >= threshold:
   - Logs: `GPU VRAM usage at 99.2% (>= 99% threshold) - triggering model reload to free memory`
   - Reloads the model to free fragmented VRAM
5. All activity logged with `[HEALTH]` prefix

## Architecture

### Async Streaming

The native GPU backend uses an async queue architecture for true real-time streaming:
- LLM inference runs in a background thread pool
- Tokens are pushed to an asyncio.Queue as they're generated
- The main event loop yields tokens immediately without blocking
- This enables responsive streaming for both Web UI (WebSocket) and API (SSE)

## GPU Acceleration

Poster-chan AI supports four LLM backends:

1. **IPEX-LLM** (Recommended for Intel Arc) - Intel's optimized LLM inference with best Arc GPU performance
2. **Native GPU** - Direct llama-cpp-python with SYCL (Intel), CUDA (NVIDIA), or HIP (AMD ROCm)
3. **Ollama** - External Ollama instance (Docker or native)
4. **AMD ROCm** - Native llama-cpp-python with HIP backend for AMD GPUs

### IPEX-LLM Setup (Intel Arc - Recommended)

IPEX-LLM provides the best performance on Intel Arc GPUs using Intel's optimized inference backend. This is the same technology used by Docker-based Ollama solutions for Intel Arc.

**See [docs/IPEX-LLM-SETUP.md](docs/IPEX-LLM-SETUP.md) for complete setup instructions for Gentoo, Debian/Ubuntu, and Fedora.**

**Quick Requirements:**
- Python 3.11 (required - not compatible with Python 3.12+)
- Intel oneAPI Base Toolkit
- Intel Arc GPU (A770, A750, A380, etc.)

**Setup:**

```bash
# Create separate Python 3.11 virtual environment
python3.11 -m venv venv-ipex
source venv-ipex/bin/activate

# Install Intel's custom PyTorch (required for XPU support)
pip install torch==2.1.0a0 \
    intel-extension-for-pytorch==2.1.30+xpu \
    oneccl_bind_pt==2.1.300+xpu \
    --extra-index-url https://pytorch-extension.intel.com/release-whl/stable/xpu/us/

# Install IPEX-LLM with XPU support
pip install ipex-llm[xpu]==2.2.0

# Install transformers for tokenizer support
pip install transformers>=4.37.0

# Install other app dependencies
pip install -r requirements.txt
```

**Running with IPEX-LLM:**

```bash
# Set Intel oneAPI environment
source /opt/intel/oneapi/2024.2/oneapi-vars.sh  # Gentoo
# OR
source /opt/intel/oneapi/setvars.sh  # Ubuntu/Debian

# Activate IPEX venv
source venv-ipex/bin/activate

# Run the app
python run.py
```

**Configuration:**

1. In Admin Panel, set **Backend Type** to "IPEX-LLM"
2. Set **Model Path** to your model (GGUF or HuggingFace path)
3. Click **Save Settings**
4. Click **Reload Model** to load the new model

**Systemd Service (for IPEX):**

The installer automatically creates a systemd service with the correct environment. If setting up manually, create `/etc/systemd/system/posterchanai-ipex.service`:

```ini
[Unit]
Description=Posterchan AI (IPEX-LLM)
After=network.target

[Service]
Type=simple
User=verita84
WorkingDirectory=/home/verita84/posterchanai

# Python virtual environment
Environment="PATH=/home/verita84/posterchanai/venv-ipex/bin:/opt/intel/oneapi/2025.0/bin:/usr/local/bin:/usr/bin"
Environment="VIRTUAL_ENV=/home/verita84/posterchanai/venv-ipex"

# Intel oneAPI libraries - CRITICAL for SYCL/llama.cpp
# These must be set explicitly since 'source oneapi-vars.sh' doesn't work in systemd
Environment="LD_LIBRARY_PATH=/opt/intel/oneapi/2025.0/lib:/usr/local/lib"
Environment="OCL_ICD_FILENAMES=/opt/intel/oneapi/2025.0/lib/libintelocl.so"
Environment="ONEAPI_ROOT=/opt/intel/oneapi/2025.0"

# IPEX-LLM optimizations
Environment="ENABLE_SDP_FUSION=1"
Environment="SYCL_CACHE_PERSISTENT=1"
Environment="BIGDL_LLM_XMX_DISABLED=1"
Environment="ZES_ENABLE_SYSMAN=1"
Environment="TORCH_DEVICE_BACKEND_AUTOLOAD=0"

# Preload VTune stub to suppress symbol warnings (optional)
Environment="LD_PRELOAD=/usr/local/lib/libittnotify.so"

ExecStart=/home/verita84/posterchanai/run-ipex.sh
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Note:** Adjust the oneAPI path (`2025.0`) to match your installed version (`2024.2`, etc.).

**Intel Arc Troubleshooting:**

| Issue | Solution |
|-------|----------|
| `cannot enable executable stack` | Run `sudo scanelf -Xe venv-ipex/lib/python*/site-packages/intel_extension_for_pytorch/lib/libintel-ext-pt*.so` |
| `LIBUR_LOADER version not found` | Reinstall PyTorch XPU packages to match new intel-compute-runtime |
| XPU not detected | Verify with `clinfo` and ensure intel-compute-runtime is installed |
| Segfault on model load | Check intel-compute-runtime version matches level-zero version |

**Known Issue - Executable Stack on Hardened Kernels (Gentoo):**

Intel IPEX libraries are built with RWX (read-write-execute) stack requirements, which is blocked by default on systems with strict memory protection (Gentoo, hardened kernels). The error appears as:

```
ImportError: libintel-ext-pt-cpu.so: cannot enable executable stack as shared object requires: Invalid argument
```

**Fix:** The installer handles this automatically using `scanelf` (pax-utils) or `patchelf`. If you installed manually:

```bash
# Method 1: Using scanelf (recommended for Gentoo)
sudo scanelf -Xe venv-ipex/lib/python3.11/site-packages/intel_extension_for_pytorch/lib/libintel-ext-pt*.so

# Method 2: Using patchelf
patchelf --clear-execstack venv-ipex/lib/python3.11/site-packages/intel_extension_for_pytorch/lib/libintel-ext-pt-cpu.so
```

**Upgrading intel-compute-runtime:**

When upgrading `intel-compute-runtime` on Gentoo, you may need to reinstall PyTorch XPU packages:

```bash
# Unmask newer version if needed
echo "=dev-libs/intel-compute-runtime-25.40.35563.4 ~amd64" >> /etc/portage/package.accept_keywords

# Reinstall after upgrade
source venv-ipex/bin/activate
pip install torch==2.5.1+cxx11.abi torchvision==0.20.1+cxx11.abi intel-extension-for-pytorch==2.5.10+xpu \
    --extra-index-url https://pytorch-extension.intel.com/release-whl/stable/xpu/us/ --force-reinstall

# Fix executable stack again
sudo scanelf -Xe venv-ipex/lib/python*/site-packages/intel_extension_for_pytorch/lib/libintel-ext-pt*.so
```

### Native GPU Setup (No Docker Required)

Native GPU mode runs the LLM directly in the Python process using llama-cpp-python with GPU acceleration.

#### Intel Arc GPU (SYCL)

**Ubuntu/Debian:**
```bash
# Install Intel oneAPI Base Toolkit
wget -O- https://apt.repos.intel.com/intel-gpg-keys/GPG-PUB-KEY-INTEL-SW-PRODUCTS.PUB | \
  sudo gpg --dearmor -o /usr/share/keyrings/intel.gpg
echo "deb [signed-by=/usr/share/keyrings/intel.gpg] https://apt.repos.intel.com/oneapi all main" | \
  sudo tee /etc/apt/sources.list.d/intel-oneapi.list
sudo apt update && sudo apt install intel-oneapi-base-toolkit

# Build llama-cpp-python with SYCL (MUST use icx/icpx compilers)
source /opt/intel/oneapi/setvars.sh
CMAKE_ARGS="-DGGML_SYCL=ON -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
    pip install llama-cpp-python --force-reinstall --no-cache-dir
```

**Gentoo:**
```bash
# Download and install Intel oneAPI Base Toolkit (get latest from Intel)
# https://www.intel.com/content/www/us/en/developer/tools/oneapi/base-toolkit-download.html
cd /tmp
wget https://registrationcenter-download.intel.com/akdlm/.../l_BaseKit_p_VERSION_offline.sh
chmod +x l_BaseKit_p_*.sh
sudo ./l_BaseKit_p_*.sh -a --silent --eula accept

# Build llama-cpp-python with SYCL (MUST use icx/icpx compilers)
source /opt/intel/oneapi/2025.0/oneapi-vars.sh  # or 2024.2
CMAKE_ARGS="-DGGML_SYCL=ON -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
    pip install llama-cpp-python --force-reinstall --no-cache-dir
```

#### NVIDIA GPU (CUDA)

**Ubuntu/Debian:**
```bash
# Ensure CUDA toolkit is installed
sudo apt install nvidia-cuda-toolkit

# Build llama-cpp-python with CUDA
CMAKE_ARGS="-DGGML_CUDA=on" \
    pip install llama-cpp-python --force-reinstall --no-cache-dir
```

**Gentoo:**
```bash
# Ensure CUDA is installed
emerge dev-util/nvidia-cuda-toolkit

# Build llama-cpp-python with CUDA
CMAKE_ARGS="-DGGML_CUDA=on" \
    pip install llama-cpp-python --force-reinstall --no-cache-dir
```

#### AMD GPU (ROCm)

Supports AMD Radeon RX 6000/7000 series and some RX 5000 series GPUs.

**Gentoo:**
```bash
# ROCm packages are ~amd64, add to package.accept_keywords first:
echo -e 'dev-build/rocm-cmake\ndev-util/hipcc\ndev-libs/rocm-core\ndev-libs/roct-thunk-interface\ndev-libs/rocm-device-libs\ndev-libs/rocr-runtime\ndev-libs/rocm-comgr\ndev-util/rocminfo\ndev-util/rocm-smi\ndev-libs/rocm-opencl-runtime\ndev-util/hip\nsci-libs/hipBLAS\nsci-libs/hipBLAS-common\nsci-libs/rocBLAS\nsci-libs/rocSOLVER\ndev-util/Tensile' | sudo tee /etc/portage/package.accept_keywords/rocm

# Install ROCm + hipBLAS (required for llama.cpp)
# NOTE: Requires 30-50GB free in /var/tmp for building rocBLAS/Tensile!
# If using tmpfs/zram, unmount it first: sudo umount /var/tmp
emerge -av dev-libs/rocm-opencl-runtime dev-util/hip dev-libs/rocr-runtime sci-libs/hipBLAS

# Add user to required groups
sudo usermod -aG video,render $USER
# Log out and back in

# Build llama-cpp-python with HIP
CMAKE_ARGS="-DGGML_HIP=on" \
    pip install llama-cpp-python --force-reinstall --no-cache-dir
```

**Arch Linux:**
```bash
# Install ROCm
pacman -S rocm-hip-sdk rocm-opencl-sdk

# Add user to required groups
sudo usermod -aG video,render $USER

# Build llama-cpp-python with HIP
CMAKE_ARGS="-DGGML_HIP=on" \
    pip install llama-cpp-python --force-reinstall --no-cache-dir
```

**Ubuntu/Debian:**
```bash
# Add AMD's repo and install ROCm
wget https://repo.radeon.com/amdgpu-install/latest/ubuntu/$(lsb_release -cs)/amdgpu-install_6.0.60002-1_all.deb
sudo apt install ./amdgpu-install_*.deb
sudo amdgpu-install --usecase=rocm

# Add user to required groups
sudo usermod -aG video,render $USER

# Build llama-cpp-python with HIP
CMAKE_ARGS="-DGGML_HIP=on" \
    pip install llama-cpp-python --force-reinstall --no-cache-dir
```

**Environment Variables (run-amd.sh):**

The installer auto-generates `run-amd.sh` with these environment variables:
```bash
export ROCM_PATH=/opt/rocm
export HIP_PATH=/opt/rocm
export HSA_OVERRIDE_GFX_VERSION=10.3.1  # Adjust for your GPU
export PYTORCH_HIP_ALLOC_CONF=expandable_segments:True
```

**GFX Version Reference:**
| GPU Series | GFX Version | HSA_OVERRIDE_GFX_VERSION |
|------------|-------------|--------------------------|
| RX 7900 XTX/XT | gfx1100 | 11.0.0 |
| RX 7800/7700 | gfx1101 | 11.0.1 |
| RX 7600 | gfx1102 | 11.0.2 |
| RX 6800/6900 | gfx1030 | 10.3.0 |
| RX 6700 XT | gfx1031 | 10.3.1 |
| RX 6600 | gfx1032 | 10.3.2 |

Check your GPU's GFX version with: `rocminfo | grep gfx`

**PyTorch ROCm for Image Generation:**

For native image generation (diffusers) on AMD GPUs, you need PyTorch with ROCm support:

```bash
# ROCm 7.0 nightly (required for Python 3.13)
# Note: ~5GB download, use temp dir if /tmp is small (zram)
mkdir -p ~/tmp
TMPDIR=~/tmp pip install --no-cache-dir torch torchvision \
    --index-url https://download.pytorch.org/whl/nightly/rocm7.0
rm -rf ~/tmp
```

The installer handles this automatically for AMD GPUs.

**AMD ROCm Troubleshooting:**

| Issue | Solution |
|-------|----------|
| "No space left on device" | Use `TMPDIR=~/tmp` for pip install (default /tmp may be small zram) |
| "HIP out of memory" | Close other GPU apps, use SD 1.5 instead of SDXL, reduce image size |
| "invalid device function" | Set correct `HSA_OVERRIDE_GFX_VERSION` in run-amd.sh |
| PyTorch not detecting GPU | Ensure ROCm is installed and user is in `video` and `render` groups |
| Slow first generation | Normal - HIP compiles shaders on first run (~1-2 min) |
| Slower than NVIDIA | ROCm uses CPU offload for SDXL to fit in 12GB - expect ~2x slower |
| llama-cpp-python SEGV crash | See "Known Issues" below - use CPU mode or Ollama as workaround |

**Known Issues - llama-cpp-python ROCm on Gentoo:**

On Gentoo Linux, `llama-cpp-python` with ROCm/HIP support may crash with SEGFAULT during inference. This is because:

1. Gentoo installs ROCm libraries in `/usr/lib64/` instead of `/opt/rocm/lib/`
2. HIP cmake files are in `/usr/lib64/cmake/hip/` instead of `/opt/rocm/lib/cmake/hip/`
3. The `CMAKE_ARGS` passed via pip don't properly configure cmake to find HIP

**Workarounds:**

1. **Use CPU mode** (automatic fallback) - LLM runs on CPU, slower but works
2. **Use Ollama** - Install Ollama with ROCm support (pre-built, works correctly)
3. **Manual llama.cpp build** - Clone llama.cpp and build manually with correct cmake paths:
   ```bash
   git clone https://github.com/ggerganov/llama.cpp
   cd llama.cpp
   cmake -B build -DGGML_HIPBLAS=ON -DCMAKE_PREFIX_PATH=/usr/lib64/cmake
   cmake --build build --config Release
   # Use the built llama-server binary instead of llama-cpp-python
   ```

**Image generation (diffusers) works correctly** with PyTorch ROCm - only llama-cpp-python has the build issue.

**VRAM Requirements for Image Generation:**

| Model Type | Min VRAM | Recommended |
|------------|----------|-------------|
| SD 1.5 | 4GB | 6GB |
| SDXL | 8GB | 12GB |
| SD 1.5 + LLM | 8GB | 12GB |
| SDXL + LLM | 12GB | 16GB+ |

**Upgrading PyTorch ROCm:**

If you need to upgrade or reinstall PyTorch for AMD:

```bash
# Activate venv first
source venv/bin/activate

# Force reinstall with ROCm support
mkdir -p ~/tmp
TMPDIR=~/tmp pip install --force-reinstall --no-cache-dir torch torchvision \
    --index-url https://download.pytorch.org/whl/nightly/rocm7.0
rm -rf ~/tmp

# Verify installation
python -c "import torch; print(f'PyTorch {torch.__version__}, ROCm: {torch.version.hip}, GPU: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else \"Not detected\"}')"

# Restart service
sudo systemctl restart posterchanai-rocm
```

#### CPU Fallback

If no GPU is available:
```bash
pip install llama-cpp-python
```

### Native GPU Configuration

1. In Admin Panel, set **Backend Type** to "Native GPU"
2. Set **Model Path** to your GGUF model file (e.g., `/home/user/models/model.gguf`)
3. Set **GPU Layers** to `-1` (all layers on GPU) or a specific number
4. Click **Save Settings**
5. Click **Reload Model** to load the new model

### VRAM Optimization Settings

The following settings in Admin Panel control VRAM usage:

| Setting | Description | VRAM Impact |
|---------|-------------|-------------|
| **Context Length** | Maximum tokens in context window | Higher = More VRAM |
| **Batch Size** | Tokens processed per batch (1-2048) | Higher = More VRAM, faster prompts |
| **GPU Layers** | Layers offloaded to GPU (-1 = all) | More layers = More VRAM |

**Recommended settings for 16GB Intel Arc A770:**

| Model Size | Quantization | Context | Batch | VRAM Usage |
|------------|--------------|---------|-------|------------|
| 14B | Q5_K_M | 25024 | 128 | ~15GB |
| 14B | Q5_K_M | 20480 | 256 | ~15GB |
| 14B | Q4_K_M | 28024 | 256 | ~12GB |
| 8B | Q5_K_M | 40960 | 512 | ~10GB |

**Recommended settings for 12GB AMD RX 6700/6750 XT:**

| Model Size | Quantization | Context | Batch | VRAM Usage |
|------------|--------------|---------|-------|------------|
| 8B | Q5_K_M | 8192 | 128 | ~6GB |
| 8B | Q4_K_M | 16384 | 128 | ~7GB |
| 14B | Q4_K_M | 4096 | 64 | ~10GB |

**For AMD GPUs with image generation:**
- LLM only: Use full context settings above
- LLM + SDXL: Reduce LLM context to 4096, or use "unload" mode in Image Settings
- LLM + SD 1.5: Can use larger contexts, SD 1.5 needs less VRAM

**Important**: Match the context size in Admin Panel with your GPU's VRAM capacity to prevent out-of-memory errors.

### Request Queue Logging

The IPEX service logs all requests for troubleshooting:

```
[IPEX] [REQ-1] Queued: "What is the meaning of life?..." (pending: 1)
[IPEX] [REQ-1] Processing started
[IPEX] [REQ-1] Completed in 2.3s (pending: 0)
```

For streaming requests:
```
[IPEX] [STREAM-2] Queued: "Tell me a story..." (pending: 1)
[IPEX] [STREAM-2] Processing started
[IPEX] [STREAM-2] Completed in 15.2s
```

This helps diagnose:
- Queue depth (how many requests are waiting)
- Processing time per request
- Timeout issues
- Request ordering

## Requirements

- Python 3.11+
- **For Native GPU mode:**
  - Intel Arc: Intel oneAPI Base Toolkit
  - NVIDIA: CUDA Toolkit
  - GGUF model file
- **For Ollama mode:**
  - Ollama instance (local or Docker)
- **For OCR (image text extraction):**
  - Tesseract OCR (`apt install tesseract-ocr` or `emerge app-text/tesseract`)
- ComfyUI instance (optional, for image generation)
- SearXNG instance (optional, for web search)

### Python Dependencies (auto-installed by setup.sh)

Key packages:
- `pytesseract` - OCR text extraction
- `pillow-heif` - HEIC/HEIF image support
- `edge-tts` - Text-to-speech
- `python-docx`, `openpyxl`, `python-pptx` - Office document support
- `PyMuPDF` - PDF text extraction

### Built-in Torrent Client (Optional)

The built-in torrent client uses `libtorrent-rasterbar` with Python bindings. All traffic is routed through an HTTP proxy (for Tor anonymity).

**Gentoo:**
```bash
# Enable Python USE flag
echo "net-libs/libtorrent-rasterbar python" >> /etc/portage/package.use/libtorrent
emerge -av net-libs/libtorrent-rasterbar

# Verify Python bindings are installed
python3 -c "import libtorrent; print(libtorrent.version)"
```

**Debian/Ubuntu:**
```bash
apt install python3-libtorrent
```

**Virtual Environment Setup:**

If using a venv, create it with system site-packages access:
```bash
python -m venv venv --system-site-packages
```

The libtorrent service will automatically detect system-installed packages even in a regular venv.

### Image Generation Dependencies (requirements-image.txt)

- `diffusers` - HuggingFace Stable Diffusion pipelines
- `transformers` - Model tokenizers
- `accelerate` - GPU acceleration utilities
- `safetensors` - Efficient model loading
- `onnxruntime` - WD14 tagger inference (CPU/GPU)
- `huggingface_hub` - Model downloads
