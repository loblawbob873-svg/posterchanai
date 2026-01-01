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

```bash
# Clone the repository
git clone <repo-url>
cd posterchanai

# Run setup (creates virtual environment and installs dependencies)
./setup.sh
```

## Running

### Development

```bash
source venv/bin/activate
python run.py
```

### Production (systemd)

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

- **Temperature**: Controls randomness (0.0 - 2.0)
- **Top P**: Nucleus sampling threshold
- **Top K**: Top-k sampling
- **Repeat Penalty**: Penalty for repeated tokens
- **Context Length**: Maximum context window size
- **Max Tokens**: Maximum tokens to generate
- **Keep Alive**: How long to keep model in memory (-1 = forever)

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

## GPU Acceleration

Poster-chan AI supports two LLM backends:

1. **Native GPU** (Recommended) - Direct llama-cpp-python with SYCL (Intel) or CUDA (NVIDIA)
2. **Ollama** - External Ollama instance (Docker or native)

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

### Ollama Setup (Docker)

For running Ollama on Intel Arc GPUs, use the BigDL/IPEX-LLM container:

```yaml
# docker-compose.yml
services:
  ollama-intel-arc:
    image: intelanalytics/ipex-llm-inference-cpp-xpu:latest
    container_name: ollama-intel-arc
    restart: unless-stopped
    devices:
      - /dev/dri:/dev/dri
    volumes:
      - ollama-volume:/root/.ollama
    ports:
      - 11434:11434
    environment:
      - OLLAMA_HOST=0.0.0.0
      - DEVICE=Arc
      - OLLAMA_INTEL_GPU=true
      - OLLAMA_NUM_GPU=999
      - OLLAMA_NUM_CTX=28024
      - OLLAMA_KEEP_ALIVE=-1
      - ZES_ENABLE_SYSMAN=1
    command: sh -c 'mkdir -p /llm/ollama && cd /llm/ollama && init-ollama && exec ./ollama serve'

volumes:
  ollama-volume: {}
```

### VRAM Usage Guidelines (16GB Intel Arc A770)

| Model Size | Quantization | Context Size | VRAM Usage |
|------------|--------------|--------------|------------|
| 14B | Q5_K_M | 28024 | ~15GB |
| 14B | Q4_K_M | 28024 | ~12GB |
| 8B | Q5_K_M | 40960 | ~10GB |

**Important**: Match the context size in Admin Panel with your GPU's VRAM capacity to prevent out-of-memory errors.

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
