"""
WebDAV Music Service - Remote music library streaming.

Provides:
- WebDAV connection and authentication
- Music file listing (mp3, flac, ogg, wav, m4a)
- Folder browsing
- Audio metadata extraction
- Streaming URL generation
- LLM-enhanced search and playlists
"""
import logging
import json
from typing import Optional, List, Dict, Any, TYPE_CHECKING
from dataclasses import dataclass
from urllib.parse import quote, unquote
import requests
from requests.auth import HTTPBasicAuth
from xml.etree import ElementTree
from sqlalchemy.orm import Session

from app.models import UserSetting
from app.services.crypto_service import decrypt_string, encrypt_string

if TYPE_CHECKING:
    from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Audio file extensions to include
AUDIO_EXTENSIONS = {'.mp3', '.flac', '.ogg', '.wav', '.m4a', '.aac', '.opus', '.wma'}


@dataclass
class AudioTrack:
    """Represents an audio file."""
    path: str  # Full WebDAV path
    filename: str
    title: str  # Extracted from filename
    artist: Optional[str]
    album: Optional[str]
    duration: Optional[int]  # Seconds
    size: int  # Bytes
    content_type: str


@dataclass
class MusicFolder:
    """Represents a folder in the WebDAV music library."""
    path: str
    name: str


def get_user_webdav_config(user_id: int, db: Session) -> Optional[Dict[str, str]]:
    """Get user's WebDAV music configuration."""
    setting = db.query(UserSetting).filter(
        UserSetting.user_id == user_id,
        UserSetting.key == "webdav_music_config"
    ).first()

    if not setting or not setting.value:
        return None

    try:
        config = json.loads(setting.value)
        # Decrypt password
        if config.get('password'):
            config['password'] = decrypt_string(config['password'])
        return config
    except json.JSONDecodeError:
        logger.error(f"Invalid webdav_music_config JSON for user {user_id}")
        return None


def save_user_webdav_config(user_id: int, db: Session, url: str, username: str, password: Optional[str] = None):
    """Save user's WebDAV music configuration."""
    logger.info(f"save_user_webdav_config called: user_id={user_id}, url={url}, username={username}, has_password={password is not None}")

    # Get existing config to preserve password if not provided
    existing = get_user_webdav_config(user_id, db)
    logger.info(f"Existing config: {existing is not None}, has_password={existing.get('password') if existing else None}")

    config = {
        'url': url.rstrip('/') if url else '',
        'username': username or '',
        'password': ''
    }

    # Handle password - encrypt if provided, keep existing if null
    if password:
        config['password'] = encrypt_string(password)
        logger.info("Using new password")
    elif existing and existing.get('password'):
        config['password'] = encrypt_string(existing['password'])
        logger.info("Preserving existing password")

    setting = db.query(UserSetting).filter(
        UserSetting.user_id == user_id,
        UserSetting.key == "webdav_music_config"
    ).first()

    if setting:
        setting.value = json.dumps(config)
        logger.info(f"Updating existing setting, new value: url={config['url']}, username={config['username']}, has_password={bool(config['password'])}")
    else:
        setting = UserSetting(
            user_id=user_id,
            key="webdav_music_config",
            value=json.dumps(config)
        )
        db.add(setting)
        logger.info(f"Creating new setting: url={config['url']}, username={config['username']}, has_password={bool(config['password'])}")

    db.commit()
    logger.info("WebDAV config committed to database")


def connect_webdav(url: str, username: str, password: str) -> bool:
    """Test WebDAV connection with PROPFIND."""
    try:
        headers = {
            'Content-Type': 'application/xml',
            'Depth': '0'
        }
        propfind_body = '''<?xml version="1.0" encoding="utf-8"?>
        <D:propfind xmlns:D="DAV:">
            <D:prop><D:resourcetype/></D:prop>
        </D:propfind>'''

        resp = requests.request(
            'PROPFIND', url,
            auth=HTTPBasicAuth(username, password),
            headers=headers,
            data=propfind_body,
            timeout=10
        )
        return resp.status_code in (200, 207)
    except Exception as e:
        logger.error(f"WebDAV connection failed: {e}")
        return False


def list_folder(url: str, username: str, password: str, path: str = "/") -> Dict[str, Any]:
    """List contents of a WebDAV folder. Returns folders and audio files."""
    from urllib.parse import urlparse

    # Parse URL to get base path for relative path calculation
    parsed_url = urlparse(url)
    url_base_path = parsed_url.path.rstrip('/')  # e.g., /dav/files/verita84/Music

    # Handle path - could be relative (/) or absolute (/dav/files/...)
    if path == "/" or path == "":
        # Root of the configured music folder
        full_url = url.rstrip('/')
        current_path = url_base_path
    elif path.startswith(url_base_path):
        # Already a full path, use as-is
        full_url = f"{parsed_url.scheme}://{parsed_url.netloc}{path}"
        current_path = path.rstrip('/')
    else:
        # Relative path, append to URL
        if not path.startswith('/'):
            path = '/' + path
        full_url = f"{url.rstrip('/')}{path}"
        current_path = f"{url_base_path}{path}".rstrip('/')

    folders = []
    tracks = []

    try:
        headers = {
            'Content-Type': 'application/xml',
            'Depth': '1'
        }
        propfind_body = '''<?xml version="1.0" encoding="utf-8"?>
        <D:propfind xmlns:D="DAV:">
            <D:prop>
                <D:resourcetype/>
                <D:getcontentlength/>
                <D:getcontenttype/>
                <D:displayname/>
            </D:prop>
        </D:propfind>'''

        resp = requests.request(
            'PROPFIND', full_url,
            auth=HTTPBasicAuth(username, password),
            headers=headers,
            data=propfind_body,
            timeout=30
        )

        if resp.status_code != 207:
            logger.error(f"WebDAV PROPFIND failed: {resp.status_code}")
            return {'folders': [], 'tracks': [], 'error': f"Server returned {resp.status_code}"}

        # Parse XML response
        root = ElementTree.fromstring(resp.content)
        ns = {'D': 'DAV:'}

        for response in root.findall('D:response', ns):
            href_el = response.find('D:href', ns)
            if href_el is None:
                continue

            href = unquote(href_el.text or '')
            props = response.find('.//D:prop', ns)
            if props is None:
                continue

            # Check if collection (folder)
            resourcetype = props.find('D:resourcetype', ns)
            is_collection = resourcetype is not None and resourcetype.find('D:collection', ns) is not None

            displayname = props.find('D:displayname', ns)
            name = displayname.text if displayname is not None and displayname.text else href.rstrip('/').split('/')[-1]

            # Skip the current folder itself
            href_path = href.rstrip('/')
            if href_path == current_path or href_path == unquote(current_path):
                continue

            if is_collection:
                # Extract just the folder path relative to base
                folder_path = href
                folders.append(MusicFolder(
                    path=folder_path,
                    name=name
                ))
            else:
                # Check if audio file
                ext = ('.' + name.split('.')[-1].lower()) if '.' in name else ''
                if ext in AUDIO_EXTENSIONS:
                    size_el = props.find('D:getcontentlength', ns)
                    size = int(size_el.text) if size_el is not None and size_el.text else 0

                    content_type_el = props.find('D:getcontenttype', ns)
                    content_type = content_type_el.text if content_type_el is not None else 'audio/mpeg'

                    # Parse filename for title (remove extension)
                    title = name.rsplit('.', 1)[0] if '.' in name else name

                    # Try to parse "Artist - Title" format
                    artist = None
                    if ' - ' in title:
                        parts = title.split(' - ', 1)
                        artist = parts[0].strip()
                        title = parts[1].strip()

                    tracks.append(AudioTrack(
                        path=href,
                        filename=name,
                        title=title,
                        artist=artist,
                        album=None,
                        duration=None,
                        size=size,
                        content_type=content_type
                    ))

        # Sort folders and tracks
        folders.sort(key=lambda f: f.name.lower())
        tracks.sort(key=lambda t: t.title.lower())

        return {'folders': folders, 'tracks': tracks}

    except Exception as e:
        logger.error(f"WebDAV list_folder error: {e}")
        return {'folders': [], 'tracks': [], 'error': str(e)}


def search_tracks(url: str, username: str, password: str, query: str,
                  base_path: str = "/", max_results: int = 50) -> List[AudioTrack]:
    """Search for tracks by name recursively (filename match)."""
    results = []
    query_lower = query.lower()

    def search_folder(path: str, depth: int = 0, parent_matches: bool = False):
        if depth > 5 or len(results) >= max_results:
            return

        contents = list_folder(url, username, password, path)

        # Skip if error
        if contents.get('error'):
            logger.warning(f"Search skipping {path}: {contents.get('error')}")
            return

        # Check tracks
        for track in contents.get('tracks', []):
            if len(results) >= max_results:
                return
            # If parent folder matched query, include all tracks
            # Otherwise check title, filename, or artist
            if parent_matches or (
                query_lower in track.title.lower() or
                query_lower in track.filename.lower() or
                (track.artist and query_lower in track.artist.lower())):
                results.append(track)

        # Recurse into folders
        for folder in contents.get('folders', []):
            if len(results) >= max_results:
                return
            # Check if this folder name matches the query
            folder_matches = query_lower in folder.name.lower()
            search_folder(folder.path, depth + 1, parent_matches=folder_matches)

    search_folder(base_path)
    return results


def get_stream_url(path: str) -> str:
    """Generate a streaming URL for a track (proxied through our API)."""
    return f"/api/music/stream?path={quote(path, safe='')}"


# LLM-enhanced functions

async def generate_mood_playlist(tracks: List[AudioTrack], mood: str,
                                  chat_service: "ChatService") -> List[AudioTrack]:
    """Use LLM to select tracks matching a mood from available tracks."""
    if not tracks:
        return []

    # Build track list for LLM (limit to avoid token overflow)
    track_list = "\n".join([
        f"{i+1}. {t.title}" + (f" - {t.artist}" if t.artist else "")
        for i, t in enumerate(tracks[:100])
    ])

    messages = [
        {"role": "system", "content": f"""You are a music curator. Given a list of songs and a mood/vibe request,
select songs that match. Return ONLY the numbers of matching songs, comma-separated. No explanation.
If no songs match, return "none".
Example output: 1,5,12,23"""},
        {"role": "user", "content": f"Mood: {mood}\n\nAvailable tracks:\n{track_list}"}
    ]

    try:
        response = await chat_service.chat(messages)
        response = response.strip()

        if response.lower() == "none":
            return []

        # Parse response - extract numbers
        import re
        numbers = re.findall(r'\d+', response)
        indices = [int(x) - 1 for x in numbers]
        return [tracks[i] for i in indices if 0 <= i < len(tracks)]
    except Exception as e:
        logger.error(f"Mood playlist generation error: {e}")
        return []


async def natural_language_search(query: str, available_tracks: List[AudioTrack],
                                   chat_service: "ChatService") -> List[AudioTrack]:
    """Use LLM to interpret natural language search and find matching tracks."""
    if not available_tracks:
        return []

    track_list = "\n".join([
        f"{i+1}. {t.title}" + (f" - {t.artist}" if t.artist else "")
        for i, t in enumerate(available_tracks[:100])
    ])

    messages = [
        {"role": "system", "content": """You are a music search assistant. Given a search query and available songs,
find the best matches. Return ONLY the numbers of matching songs, comma-separated. No explanation.
If no songs match, return "none".
Example output: 1,5,12"""},
        {"role": "user", "content": f"Search: {query}\n\nAvailable:\n{track_list}"}
    ]

    try:
        response = await chat_service.chat(messages)
        response = response.strip()

        if response.lower() == "none":
            return []

        import re
        numbers = re.findall(r'\d+', response)
        indices = [int(x) - 1 for x in numbers]
        return [available_tracks[i] for i in indices if 0 <= i < len(available_tracks)]
    except Exception as e:
        logger.error(f"Natural language search error: {e}")
        return []


def format_music_browse(contents: dict, path: str) -> str:
    """Format folder contents for display with cyberpunk styling."""
    display_path = path if path != "/" else "/ (root)"
    lines = [f"## ◈ MUSIC: {display_path} ◈\n"]

    # Parent folder link if not at root
    if path != "/" and path != "":
        parent = "/".join(path.rstrip('/').split('/')[:-1]) or "/"
        lines.append(f"[.. (back)](cmd:music browse {parent})\n")

    if contents.get('folders'):
        lines.append("**Folders:**")
        for folder in contents['folders']:
            folder_path = folder.path
            lines.append(f"  📁 [{folder.name}](cmd:music browse {folder_path})")
        lines.append("")

    if contents.get('tracks'):
        lines.append("**Tracks:**")
        for i, track in enumerate(contents['tracks'], 1):
            artist = f" - *{track.artist}*" if track.artist else ""
            size_mb = f" ({track.size / 1024 / 1024:.1f} MB)" if track.size else ""
            play_btn = f"[▶](cmd:music play {i})"
            queue_btn = f"[+Q](cmd:music queue add {i})"
            lines.append(f"  **{i}.** {play_btn} {queue_btn} {track.title}{artist}{size_mb}")

    if not contents.get('folders') and not contents.get('tracks'):
        if contents.get('error'):
            lines.append(f"_Error: {contents['error']}_")
        else:
            lines.append("_Empty folder_")

    return "\n".join(lines)


def format_music_tracks(tracks: List[AudioTrack], title: str) -> str:
    """Format track list for display with cyberpunk styling."""
    if not tracks:
        return f"## ◈ {title.upper()} ◈\n\n_No tracks found_"

    lines = [f"## ◈ {title.upper()} ◈\n"]

    # Add action buttons
    shuffle_btn = "[🔀 Shuffle All](cmd:music shuffle)"
    queue_all_btn = "[📥 Queue All](cmd:music queueall)"
    lines.append(f"{shuffle_btn} {queue_all_btn}\n")

    for i, track in enumerate(tracks, 1):
        artist = f" - *{track.artist}*" if track.artist else ""
        play_btn = f"[▶](cmd:music play {i})"
        queue_btn = f"[+Q](cmd:music queue add {i})"
        lines.append(f"**{i}.** {play_btn} {queue_btn} {track.title}{artist}")

    return "\n".join(lines)
