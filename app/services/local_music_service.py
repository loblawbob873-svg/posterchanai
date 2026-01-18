"""
Local music directory service.
Handles browsing and metadata extraction from local music files.
"""
import logging
import json
import os
from pathlib import Path
from typing import Optional, List, Dict
from sqlalchemy.orm import Session
from app.models import UserSetting

logger = logging.getLogger(__name__)

# Supported audio file extensions
AUDIO_EXTENSIONS = {
    '.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.wma', '.opus',
    '.MP3', '.FLAC', '.WAV', '.OGG', '.M4A', '.AAC', '.WMA', '.OPUS'
}


def get_user_music_config(user_id: int, db: Session) -> Optional[Dict[str, any]]:
    """Get user's local music configuration. Falls back to user storage path if not configured.
    Always resolves paths relative to user storage."""
    setting = db.query(UserSetting).filter(
        UserSetting.user_id == user_id,
        UserSetting.key == "local_music_config"
    ).first()
    
    if setting and setting.value:
        try:
            config = json.loads(setting.value)
            directory = config.get('directory', '')
            
            # Always resolve relative paths (paths starting with / but not containing username)
            if directory and directory.startswith('/') and not directory.startswith('//'):
                from app.models import User, Setting
                user = db.query(User).filter(User.id == user_id).first()
                upload_path_setting = db.query(Setting).filter(Setting.key == "upload_path").first()
                
                if user and upload_path_setting and upload_path_setting.value:
                    upload_base = Path(upload_path_setting.value)
                    # If the path doesn't contain the username, treat it as relative to user storage
                    if user.username not in directory:
                        relative_path = directory.lstrip('/')
                        resolved_directory = str(upload_base / user.username / relative_path)
                        logger.info(f"[MUSIC CONFIG] Resolved {directory} to {resolved_directory}")
                        config['directory'] = resolved_directory
            
            return config
        except json.JSONDecodeError:
            logger.error(f"Invalid local_music_config JSON for user {user_id}")
    
    # Fall back to user storage path + /Music (using upload_path like File Manager)
    from app.models import User, Setting
    from pathlib import Path
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return None
    
    # Get upload path (same as File Manager)
    upload_path_setting = db.query(Setting).filter(Setting.key == "upload_path").first()
    upload_path = upload_path_setting.value if upload_path_setting and upload_path_setting.value else "/var/lib/posterchanai"
    
    upload_base = Path(upload_path)
    music_dir = upload_base / user.username / "Music"
    
    # Return default config
    return {
        'directory': str(music_dir),
        'recursive': True
    }


def save_user_music_config(user_id: int, db: Session, directory: str, recursive: bool = True):
    """Save user's local music configuration. Saves path as-is, resolution happens on read."""
    # Save the directory path exactly as provided by the user
    # Resolution will happen in get_user_music_config() and test_directory_access()
    config = {
        'directory': directory,
        'recursive': recursive
    }
    
    setting = db.query(UserSetting).filter(
        UserSetting.user_id == user_id,
        UserSetting.key == "local_music_config"
    ).first()
    
    if setting:
        setting.value = json.dumps(config)
        logger.info(f"Updating music config: directory={directory}, recursive={recursive}")
    else:
        setting = UserSetting(
            user_id=user_id,
            key="local_music_config",
            value=json.dumps(config)
        )
        db.add(setting)
        logger.info(f"Creating music config: directory={directory}, recursive={recursive}")
    
    db.commit()
    return True


def test_directory_access(directory: str, recursive: bool = True, db: Session = None, user_id: int = None) -> Dict[str, any]:
    """Test if directory exists and is accessible, count music files. Supports storage proxy if configured."""
    if not directory:
        return {"success": False, "error": "Directory path is required"}
    
    # Check if storage proxy is configured
    if db and user_id:
        from app.models import Setting
        storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
        
        if storage_server_url and storage_server_url.value:
            # Use storage proxy to test
            import httpx
            api_path = directory.lstrip('/') if directory.startswith('/') else directory
            
            try:
                url = f"{storage_server_url.value}/api/files/list?path={api_path}"
                logger.info(f"[MUSIC TEST PROXY] Testing directory via storage server: {url}")
                
                # Get auth token
                storage_token = db.query(Setting).filter(Setting.key == "storage_server_token").first()
                headers = {}
                if storage_token and storage_token.value:
                    headers["Authorization"] = f"Bearer {storage_token.value}"
                
                with httpx.Client(timeout=30.0) as client:
                    response = client.get(url, headers=headers)
                    if response.status_code == 200:
                        data = response.json()
                        # Count audio files
                        track_count = 0
                        for item in data.get('files', []):
                            if item.get('type') == 'file':
                                ext = Path(item['name']).suffix.lower()
                                if ext in AUDIO_EXTENSIONS:
                                    track_count += 1
                        
                        logger.info(f"[MUSIC TEST PROXY] Found {track_count} tracks")
                        return {
                            "success": True,
                            "message": f"Directory accessible",
                            "track_count": track_count
                        }
                    else:
                        logger.error(f"[MUSIC TEST PROXY] Storage server returned {response.status_code}")
                        return {"success": False, "error": f"Storage server error: {response.status_code}"}
            except Exception as e:
                logger.error(f"[MUSIC TEST PROXY] Error testing directory: {e}")
                return {"success": False, "error": f"Storage proxy error: {str(e)}"}
    
    # Local filesystem access (original code)
    path = Path(directory)
    
    if not path.exists():
        return {"success": False, "error": f"Directory does not exist: {directory}"}
    
    if not path.is_dir():
        return {"success": False, "error": f"Path is not a directory: {directory}"}
    
    if not os.access(directory, os.R_OK):
        return {"success": False, "error": f"Directory is not readable: {directory}"}
    
    # Count music files
    try:
        track_count = 0
        if recursive:
            for root, dirs, files in os.walk(directory):
                track_count += sum(1 for f in files if Path(f).suffix in AUDIO_EXTENSIONS)
        else:
            track_count = sum(1 for f in path.iterdir() if f.is_file() and f.suffix in AUDIO_EXTENSIONS)
        
        return {
            "success": True,
            "message": f"Directory accessible",
            "track_count": track_count
        }
    except Exception as e:
        return {"success": False, "error": f"Error scanning directory: {str(e)}"}


def scan_music_directory(directory: str, recursive: bool = True, subfolder: str = '', db: Session = None, user_id: int = None) -> List[Dict[str, any]]:
    """
    Scan music directory and return list of files and folders.
    Returns a list of items with 'type' (file/folder), 'name', 'path', 'size', etc.
    Supports storage proxy if configured.
    """
    if not directory:
        return []
    
    # Check if storage proxy is configured
    if db and user_id:
        from app.models import Setting, User
        storage_server_url = db.query(Setting).filter(Setting.key == "storage_server_url").first()
        
        if storage_server_url and storage_server_url.value:
            # Use storage proxy - make HTTP request to list files
            user = db.query(User).filter(User.id == user_id).first()
            if user:
                import httpx
                # Build path: Music or Music/subfolder
                api_path = directory.lstrip('/') if directory.startswith('/') else directory
                if subfolder:
                    api_path = f"{api_path}/{subfolder}".replace('//', '/')
                
                try:
                    url = f"{storage_server_url.value}/api/files/list?path={api_path}"
                    logger.info(f"[MUSIC PROXY] Fetching from storage server: {url}")
                    
                    # Get auth token
                    storage_token = db.query(Setting).filter(Setting.key == "storage_server_token").first()
                    headers = {}
                    if storage_token and storage_token.value:
                        headers["Authorization"] = f"Bearer {storage_token.value}"
                    
                    with httpx.Client(timeout=30.0) as client:
                        response = client.get(url, headers=headers)
                        if response.status_code == 200:
                            data = response.json()
                            # Convert File Manager format to Music format
                            items = []
                            for item in data.get('files', []):
                                if item.get('type') == 'directory':
                                    items.append({
                                        'type': 'folder',
                                        'name': item['name'],
                                        'path': f"{subfolder}/{item['name']}" if subfolder else item['name'],
                                        'track_count': 0  # Would need separate request to count
                                    })
                                elif item.get('type') == 'file':
                                    # Check if it's an audio file
                                    name = item['name']
                                    ext = Path(name).suffix.lower()
                                    if ext in AUDIO_EXTENSIONS:
                                        items.append({
                                            'type': 'file',
                                            'name': name,
                                            'path': f"{subfolder}/{name}" if subfolder else name,
                                            'size': item.get('size', 0),
                                            'extension': ext
                                        })
                            logger.info(f"[MUSIC PROXY] Found {len(items)} items")
                            return items
                        else:
                            logger.error(f"[MUSIC PROXY] Storage server returned {response.status_code}")
                except Exception as e:
                    logger.error(f"[MUSIC PROXY] Error fetching from storage server: {e}")
                    # Fall through to local access
    
    # Local filesystem access (original code)
    base_path = Path(directory)
    if subfolder:
        scan_path = base_path / subfolder
    else:
        scan_path = base_path
    
    if not scan_path.exists() or not scan_path.is_dir():
        return []
    
    items = []
    
    try:
        for item in sorted(scan_path.iterdir()):
            # Skip hidden files
            if item.name.startswith('.'):
                continue
            
            if item.is_dir():
                # Count music files in subdirectory
                if recursive:
                    file_count = sum(1 for f in item.rglob('*') if f.is_file() and f.suffix in AUDIO_EXTENSIONS)
                else:
                    file_count = sum(1 for f in item.iterdir() if f.is_file() and f.suffix in AUDIO_EXTENSIONS)
                
                items.append({
                    'type': 'folder',
                    'name': item.name,
                    'path': str(item.relative_to(base_path)),
                    'track_count': file_count
                })
            elif item.is_file() and item.suffix in AUDIO_EXTENSIONS:
                # Get file metadata
                try:
                    size = item.stat().st_size
                    items.append({
                        'type': 'file',
                        'name': item.name,
                        'path': str(item.relative_to(base_path)),
                        'size': size,
                        'extension': item.suffix.lower()
                    })
                except Exception as e:
                    logger.warning(f"Error getting metadata for {item}: {e}")
    except Exception as e:
        logger.error(f"Error scanning directory {scan_path}: {e}")
    
    return items


def search_music_files(directory: str, query: str, recursive: bool = True, limit: int = 50) -> List[Dict[str, any]]:
    """Search for music files matching query."""
    if not directory or not query:
        return []
    
    base_path = Path(directory)
    if not base_path.exists() or not base_path.is_dir():
        return []
    
    query_lower = query.lower()
    results = []
    
    try:
        if recursive:
            search_iter = base_path.rglob('*')
        else:
            search_iter = base_path.iterdir()
        
        for item in search_iter:
            if len(results) >= limit:
                break
            
            if item.is_file() and item.suffix in AUDIO_EXTENSIONS:
                if query_lower in item.name.lower():
                    try:
                        size = item.stat().st_size
                        results.append({
                            'type': 'file',
                            'name': item.name,
                            'path': str(item.relative_to(base_path)),
                            'size': size,
                            'extension': item.suffix.lower()
                        })
                    except Exception as e:
                        logger.warning(f"Error getting metadata for {item}: {e}")
    except Exception as e:
        logger.error(f"Error searching directory {base_path}: {e}")
    
    return results


def get_file_path(directory: str, relative_path: str) -> Optional[Path]:
    """Get absolute path for a music file, with security check."""
    if not directory or not relative_path:
        return None
    
    base_path = Path(directory).resolve()
    file_path = (base_path / relative_path).resolve()
    
    # Security check: ensure file_path is within base_path
    try:
        file_path.relative_to(base_path)
    except ValueError:
        logger.warning(f"Security: Attempted path traversal: {relative_path}")
        return None
    
    if not file_path.exists() or not file_path.is_file():
        return None
    
    return file_path


def format_music_browse(items: List[Dict[str, any]], current_path: str = '') -> str:
    """Format music browse results for display."""
    if not items:
        return "🎵 No music files found in this directory.\n\nMake sure your music directory is configured in User Settings."
    
    output = []
    
    if current_path:
        output.append(f"📂 Current: {current_path}\n")
    
    # Separate folders and files
    folders = [item for item in items if item['type'] == 'folder']
    files = [item for item in items if item['type'] == 'file']
    
    if folders:
        output.append("📁 **Folders:**")
        for folder in folders:
            track_info = f" ({folder['track_count']} tracks)" if folder.get('track_count', 0) > 0 else ""
            output.append(f"  📁 {folder['name']}{track_info}")
        output.append("")
    
    if files:
        output.append("🎵 **Music Files:**")
        for idx, file in enumerate(files, 1):
            size_mb = file.get('size', 0) / (1024 * 1024)
            ext = file.get('extension', '').upper().replace('.', '')
            output.append(f"  {idx}. {file['name']} [{ext}, {size_mb:.1f} MB]")
    
    output.append("\n💡 Use `music play <number>` to play a track")
    output.append("💡 Use `music search <query>` to search for songs")
    
    return "\n".join(output)


def format_music_tracks(tracks: List[Dict[str, any]]) -> str:
    """Format music track list for display."""
    if not tracks:
        return "🎵 No tracks found matching your search."
    
    output = ["🔍 **Search Results:**\n"]
    for idx, track in enumerate(tracks, 1):
        size_mb = track.get('size', 0) / (1024 * 1024)
        ext = track.get('extension', '').upper().replace('.', '')
        # Show relative path for context
        output.append(f"{idx}. {track['name']}")
        output.append(f"   📂 {track['path']} [{ext}, {size_mb:.1f} MB]")
    
    output.append(f"\n💡 Found {len(tracks)} tracks. Use `music play <number>` to play.")
    
    return "\n".join(output)


def get_stream_url(file_path: str, quality: Optional[str] = None) -> str:
    """Generate stream URL for a music file."""
    from urllib.parse import quote
    
    encoded_path = quote(file_path, safe='')
    url = f"/api/music/stream?path={encoded_path}"
    
    if quality:
        url += f"&quality={quality}"
    
    return url


def generate_mood_playlist(directory: str, mood: str, recursive: bool = True) -> List[Dict[str, any]]:
    """
    Generate a playlist based on mood.
    For now, this is a simple implementation that returns random tracks.
    Can be enhanced with AI-based mood detection from metadata/filenames.
    """
    import random
    
    if not directory:
        return []
    
    base_path = Path(directory)
    if not base_path.exists() or not base_path.is_dir():
        return []
    
    all_tracks = []
    
    try:
        if recursive:
            search_iter = base_path.rglob('*')
        else:
            search_iter = base_path.iterdir()
        
        for item in search_iter:
            if item.is_file() and item.suffix in AUDIO_EXTENSIONS:
                try:
                    size = item.stat().st_size
                    all_tracks.append({
                        'type': 'file',
                        'name': item.name,
                        'path': str(item.relative_to(base_path)),
                        'size': size,
                        'extension': item.suffix.lower()
                    })
                except Exception as e:
                    logger.warning(f"Error getting metadata for {item}: {e}")
    except Exception as e:
        logger.error(f"Error generating playlist from {base_path}: {e}")
    
    # Return random selection of up to 20 tracks
    if all_tracks:
        random.shuffle(all_tracks)
        return all_tracks[:20]
    
    return []
