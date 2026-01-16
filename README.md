# Poster-chan AI

AI Chat Application with OpenAI-compatible API, image generation, web search, and text-to-speech capabilities.

## Quick Start

### Installation

The easiest way to get started is using the interactive installer:

```bash
# Clone the repository
git clone <repo-url>
cd posterchanai

# Run the interactive installer
./install.sh
```

The installer will:
- Detect your GPU (Intel Arc, NVIDIA, AMD, or CPU)
- Install the correct dependencies
- Set up a Python virtual environment
- Configure and start a systemd service
- Optionally download a starter model

To see required packages for your distro before installing:
```bash
./install.sh --packages
```

### Running

**Development:**
```bash
source venv/bin/activate
python run.py
```

**Production (systemd):**
If you used `install.sh`, the service is already configured. Otherwise, see [Advanced Setup](docs/ADVANCED.md#running) for manual configuration.

Access the web interface at `http://localhost:3051`

### Upgrading

```bash
cd posterchanai
git pull
sudo systemctl restart posterchanai
```

New settings are automatically added to the database when the app starts. No manual migration needed.

## Features

### AI & Chat
- AI Chat with streaming responses
- Native GPU inference (Intel SYCL, NVIDIA CUDA, AMD ROCm, CPU fallback)
- Ollama backend support (optional)
- Custom AI Service - Connect to your own AI (Ollama, Open-WebUI, Posterchanai)
- OpenAI-compatible API
- Per-user API keys
- Persistent chat history

### Intelligent Actions
- **AI-powered intent detection** - Automatically detects when you want to perform an action
- **Natural language commands** - Just describe what you want, no memorizing syntax
- **Supported actions:**
  - Calendar: Add events from natural language or pasted email invites
  - Contacts: Save contact info from business cards or text
  - Todo: Create tasks from reminders or meeting notes
  - Email: Send, check, and reply to emails
  - Music: Play by mood, search, or specific requests
  - Search: Web search and news lookup
  - Image generation: Create images from descriptions
  - YouTube: Summarize videos from URLs

### Vision & Documents
- Vision support (upload images and ask questions)
- OCR text extraction from images
- File uploads: Images, PDFs, Office documents, Text files
- Document translation

### Image Generation
- Native GPU image generation (NVIDIA CUDA, Intel XPU, AMD ROCm, CPU)
- ComfyUI backend support
- Text-to-image and image-to-image transformation
- REST API for external integrations

### Search & Web
- Web Search with AI summarization
- Auto URL fetching - mention a URL and the AI will read and summarize it
- Browser search engine integration
- Image Search

### Music
- WebDAV music streaming (Nextcloud, ownCloud, etc.)
- Cyberpunk audio visualizer
- AI-powered mood playlists
- Queue management

### Terminal UI (TUI)
- Full-featured terminal client with vim-style navigation
- Access all features from the command line
- Real-time streaming responses
- Built-in music player

### RAG (Retrieval-Augmented Generation)
- Codebase indexing for code-aware AI responses
- Local embeddings (runs offline)
- Git repository, zip file, or VS Code integration
- Auto-context injection

## Basic Configuration

Access the admin panel at `http://localhost:3051/admin`

### AI Settings
- **Backend Type**: Choose Native GPU, IPEX-LLM, or Ollama
- **Model Path**: Path to your GGUF model file
- **Temperature, Top P, Top K**: Control randomness and sampling
- **Context Length**: Maximum context window size

### Image Generation
- **Backend**: Native diffusers or ComfyUI
- **Model Path**: Path to your SDXL or SD 1.5 model
- **GPU Device**: cuda, xpu, rocm, or cpu

### Optional Services
- **SearXNG URL**: For web search
- **SMTP/IMAP**: For email functionality
- **Upload Path**: Where to store uploads

See [Advanced Configuration](docs/ADVANCED.md#configuration) for detailed settings.

## Commands

Type these commands in the chat:

| Command | Description |
|---------|-------------|
| `search <query>` | Search the web and get AI-summarized results |
| `images <query>` | Search for images |
| `geni <prompt>` | Generate an AI image from your prompt |
| `img2img <prompt>` | Transform an uploaded image with your prompt |
| `yt <url>` | Summarize a YouTube video transcript |
| `cal` | Calendar: today's events, week view, add events |
| `contacts` | Search or add CardDAV contacts |
| `mail` | Email: inbox, folders, read, reply, forward, send |
| `music` | WebDAV music streaming: browse, search, play, queue |
| `todo` | CalDAV task management: list, add, remove tasks |
| `torrents` | Built-in torrent client: browse, download, manage |
| `budget` | Budget manager (summary, bills, add, pay) |
| `news` | Fetch and summarize RSS feeds |

See [Command Reference](docs/ADVANCED.md#commands) for detailed command usage.

## Terminal UI (TUI)

A full-featured terminal client with vim-style navigation.

### Installation
```bash
cd tui
pip install -r requirements.txt
```

### Running
```bash
python -m tui
# Or with arguments
python -m tui --server https://your-server.com --user yourusername
```

See [TUI Documentation](docs/ADVANCED.md#terminal-ui-tui) for keyboard shortcuts and features.

## Voice Input

Click the microphone button to speak commands naturally. Voice input works in Chrome/Edge (Web Speech API) and falls back to local Whisper transcription in Brave/Firefox.

See [Voice Commands](docs/ADVANCED.md#voice-input) for examples.

## Custom AI Service

Users can connect to their own AI service running on their desktop or home server.

1. Click your username → Settings
2. In **Custom AI Service** section:
   - Enable **Use Custom LLM**
   - Select service type (Ollama, Open-WebUI, or Posterchanai)
   - Enter service URL and model name
   - Test connection and save

See [Custom AI Service Setup](docs/ADVANCED.md#custom-ai-service) for details.

## OpenAI-Compatible API

Posterchanai provides an OpenAI-compatible API for external applications.

### Basic Usage
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

### Python Client
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

Users can generate their own API keys from the user menu.

See [API Documentation](docs/ADVANCED.md#openai-compatible-api) for complete endpoint reference.

## Supported File Types

| Type | Extensions | Description |
|------|------------|-------------|
| Images | jpg, png, gif, webp, heic, heif | OCR text extraction + vision AI |
| PDF | pdf | Text extracted and sent to AI |
| Word | docx, doc | Text and tables extracted |
| Excel | xlsx, xls | Spreadsheet data extracted |
| PowerPoint | pptx, ppt | Slide text extracted |
| Text | txt, md, json, py, js, etc. | Sent directly to AI |

## Requirements

- Python 3.11+
- For GPU acceleration: See [GPU Setup](docs/ADVANCED.md#gpu-acceleration)
- For OCR: Tesseract OCR (`apt install tesseract-ocr` or `emerge app-text/tesseract`)
- Optional: ComfyUI instance (for image generation)
- Optional: SearXNG instance (for web search)

See [Requirements](docs/ADVANCED.md#requirements) for complete dependency list.

## Getting Help

- **Quick Start Issues**: See [Installation Troubleshooting](docs/ADVANCED.md#installation-troubleshooting)
- **GPU Setup**: See [GPU Acceleration](docs/ADVANCED.md#gpu-acceleration)
- **Configuration**: See [Advanced Configuration](docs/ADVANCED.md#configuration)
- **API Reference**: See [API Documentation](docs/ADVANCED.md#openai-compatible-api)
- **RAG Setup**: See [RAG Documentation](docs/ADVANCED.md#rag-retrieval-augmented-generation)

## Advanced Topics

For detailed technical documentation, see:

- **[Advanced Setup & Configuration](docs/ADVANCED.md)** - GPU setup, systemd configuration, advanced settings
- **[RAG Documentation](docs/ADVANCED.md#rag-retrieval-augmented-generation)** - Codebase indexing, MCP server, distributed RAG
- **[IPEX-LLM Setup](docs/IPEX-LLM-SETUP.md)** - Intel Arc GPU optimization
- **[API Reference](docs/ADVANCED.md#api-endpoints)** - Complete API endpoint documentation
- **[Architecture](docs/ADVANCED.md#architecture)** - System architecture and design
