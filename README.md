# Posterchanai

AI Chat Application with image generation, web search, and text-to-speech capabilities.

## Features

- AI Chat with streaming responses
- Vision support (upload images and ask questions about them)
- Image Generation (geni command)
- Image-to-Image transformation (img2img command)
- Web Search with AI summarization
- Image Search
- Text-to-Speech
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

Access the admin panel at `http://localhost:8000/admin` (default login: admin/admin)

### Required Settings

- **OpenWebUI URL**: URL to your OpenWebUI instance (e.g., `http://localhost:3000`)
- **OpenWebUI API Key**: API key for authentication
- **OpenWebUI Model**: Model name to use for chat

### Optional Settings

- **ComfyUI URL**: URL to ComfyUI for image generation
- **ComfyUI Default Model**: Default checkpoint model
- **ComfyUI Anime Model**: Model for anime-style images
- **SearXNG URL**: URL to SearXNG instance for web search
- **Upload Path**: Directory to store uploads (default: `/var/lib/posterchanai`)
- **TTS Settings**: Voice, rate, and pitch for text-to-speech

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
- `POST /api/auth/logout` - Logout
- `GET /api/auth/me` - Get current user

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

## Requirements

- Python 3.11+
- OpenWebUI instance (for chat)
- ComfyUI instance (optional, for image generation)
- SearXNG instance (optional, for web search)
