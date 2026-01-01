# Poster-chan AI

AI Chat Application with OpenAI-compatible API, image generation, web search, and text-to-speech capabilities.

## Features

### AI & Chat
- AI Chat with streaming responses
- **Native GPU inference** with llama-cpp-python (Intel SYCL, NVIDIA CUDA, CPU fallback)
- Ollama backend support (optional, for Docker-based setups)
- OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`)
- Per-user API keys for external app integration
- Stop button to halt AI response generation mid-stream
- Persistent chat history with file storage

### Vision & Documents
- Vision support (upload images and ask questions about them)
- **OCR text extraction** from images (via Tesseract)
- Mobile camera capture button (opens device camera directly)
- File uploads:
  - Images: JPG, PNG, GIF, WebP, **HEIC/HEIF** (Apple format)
  - PDF documents (text extraction and summarization)
  - Office documents (Word, Excel, PowerPoint)
  - Text files
- **Document translation** with language selection modal

### Image Generation
- Image Generation (geni command)
- Image-to-Image transformation (img2img command)
- Image Search

### Search & Web
- Web Search with AI summarization
- **Browser search engine integration** - use as default search engine
- Image Search

### Communication
- Text-to-Speech (with automatic language detection)
- **Email AI responses** to configured notification email
- Email verification for new registrations (when SMTP enabled)
- Email notifications (SMTP/IMAP support)

### System
- User registration (admin configurable)
- Ollama health check with auto-restart
- PWA support (installable on mobile/desktop)

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
- Detect your GPU (Intel Arc, NVIDIA, or CPU)
- Install the correct llama-cpp-python backend
- Set up a Python virtual environment
- Configure and start a systemd service
- Optionally download a starter model

To see required packages for your distro before installing:
```bash
./install.sh --packages
```

### Manual Setup

If you prefer manual control:

```bash
# Create virtual environment and install base dependencies
./setup.sh

# For GPU acceleration, manually install llama-cpp-python:
# Intel Arc:
source /opt/intel/oneapi/setvars.sh
CMAKE_ARGS="-DGGML_SYCL=ON -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
    pip install llama-cpp-python --force-reinstall --no-cache-dir

# NVIDIA:
CMAKE_ARGS="-DGGML_CUDA=ON" pip install llama-cpp-python --force-reinstall --no-cache-dir

# CPU only:
pip install llama-cpp-python
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

- **ComfyUI URL**: URL to ComfyUI for image generation
- **SearXNG URL**: URL to SearXNG instance for web search
- **Upload Path**: Directory to store uploads (default: `/var/lib/posterchanai`)
- **TTS Settings**: Voice, rate, and pitch for text-to-speech

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

## Ollama Health Check

Posterchanai includes an automatic health check that monitors Ollama and restarts it if unresponsive.

### Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `ollama_ping_enabled` | false | Enable/disable health check |
| `ollama_ping_interval` | 90 | Seconds between pings |
| `ollama_restart_after_failures` | 2 | Consecutive failures before restart |
| `ollama_restart_command` | `sudo systemctl restart ollama` | Command to restart Ollama |

### How it works

1. Sends a test prompt to Ollama every 90 seconds
2. If Ollama fails to respond, increments failure counter
3. After 5 consecutive failures, executes restart command
4. Logs all activity: `[HEALTH] Ping OK` or `[HEALTH] Ping FAILED (1/5)`

## Architecture

### Async Streaming

The native GPU backend uses an async queue architecture for true real-time streaming:
- LLM inference runs in a background thread pool
- Tokens are pushed to an asyncio.Queue as they're generated
- The main event loop yields tokens immediately without blocking
- This enables responsive streaming for both Web UI (WebSocket) and API (SSE)

## GPU Acceleration

Poster-chan AI supports three LLM backends:

1. **IPEX-LLM** (Recommended for Intel Arc) - Intel's optimized LLM inference with best Arc GPU performance
2. **Native GPU** - Direct llama-cpp-python with SYCL (Intel) or CUDA (NVIDIA)
3. **Ollama** - External Ollama instance (Docker or native)

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

Create `/etc/systemd/system/posterchanai-ipex.service`:

```ini
[Unit]
Description=Posterchan AI (IPEX-LLM)
After=network.target

[Service]
Type=simple
User=verita84
WorkingDirectory=/home/verita84/posterchanai
Environment="PATH=/home/verita84/posterchanai/venv-ipex/bin:/usr/local/bin:/usr/bin"
ExecStartPre=/bin/bash -c 'source /opt/intel/oneapi/setvars.sh'
ExecStart=/home/verita84/posterchanai/venv-ipex/bin/python run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
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

# Build llama-cpp-python with SYCL
source /opt/intel/oneapi/setvars.sh
CMAKE_ARGS="-DGGML_SYCL=on -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
    pip install llama-cpp-python==0.2.90 --force-reinstall --no-cache-dir
```

**Gentoo:**
```bash
# Download and install Intel oneAPI Base Toolkit
cd /tmp
wget https://registrationcenter-download.intel.com/akdlm/IRC_NAS/e6ff8e9c-ee28-47fb-abd7-5c524c983e1c/l_BaseKit_p_2024.2.1.100_offline.sh
chmod +x l_BaseKit_p_2024.2.1.100_offline.sh
sudo ./l_BaseKit_p_2024.2.1.100_offline.sh -a --silent --eula accept

# Build llama-cpp-python with SYCL
source /opt/intel/oneapi/2024.2/oneapi-vars.sh
CMAKE_ARGS="-DGGML_SYCL=on -DCMAKE_C_COMPILER=icx -DCMAKE_CXX_COMPILER=icpx" \
    pip install llama-cpp-python==0.2.90 --force-reinstall --no-cache-dir
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
