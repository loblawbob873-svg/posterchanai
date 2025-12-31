# Posterchanai

AI Chat Application with OpenAI-compatible API, image generation, web search, and text-to-speech capabilities.

## Features

- AI Chat with streaming responses (direct Ollama connection)
- OpenAI-compatible API (`/v1/chat/completions`, `/v1/models`)
- Per-user API keys for external app integration
- Vision support (upload images and ask questions about them)
- Image Generation (geni command)
- Image-to-Image transformation (img2img command)
- Web Search with AI summarization
- Image Search
- Text-to-Speech
- User registration (admin configurable)
- Email notifications (SMTP/IMAP support)
- Ollama health check with auto-restart
- File uploads:
  - Images (with vision AI support)
  - PDF documents (text extraction and summarization)
  - Office documents (Word, Excel, PowerPoint)
  - Text files
- Persistent chat history with file storage
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

Type these commands in the chat:

| Command | Description |
|---------|-------------|
| `search <query>` | Search the web and get AI-summarized results |
| `images <query>` | Search for images |
| `geni <prompt>` | Generate an AI image from your prompt |
| `img2img <prompt>` | Transform an uploaded image with your prompt |
| `regen` | Regenerate the last image with a new seed |

## Supported File Types

| Type | Extensions | Description |
|------|------------|-------------|
| Images | jpg, png, gif, webp | Sent to vision AI for analysis |
| PDF | pdf | Text extracted and sent to AI |
| Word | docx, doc | Text and tables extracted |
| Excel | xlsx, xls | Spreadsheet data extracted |
| PowerPoint | pptx, ppt | Slide text extracted |
| Text | txt, md, json, etc. | Sent directly to AI |

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

## Requirements

- Python 3.11+
- Ollama instance (for chat)
- ComfyUI instance (optional, for image generation)
- SearXNG instance (optional, for web search)
