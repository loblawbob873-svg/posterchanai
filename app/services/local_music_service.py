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
    """Get user's local music configuration."""
    setting = db.query(UserSetting).filter(
        UserSetting.user_id == user_id,
        UserSetting.key == "local_music_config"
    ).first()
    
    if not setting or not setting.value:
        return None
    
    try:
        config = json.loads(setting.value)
        return config
    except json.JSONDecodeError:
        logger.error(f"Invalid local_music_config JSON for user {user_id}")
        return None


def save_user_music_config(user_id: int, db: Session, directory: str, recursive: bool = True):
    """Save user's local music configuration."""
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


def test_directory_access(directory: str, recursive: bool = True) -> Dict[str, any]:
    """Test if directory exists and is accessible, count music files."""
    if not directory:
        return {"success": False, "error": "Directory path is required"}
    
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


def scan_music_directory(directory: str, recursive: bool = True, subfolder: str = '') -> List[Dict[str, any]]:
    """
    Scan music directory and return list of files and folders.
    Returns a list of items with 'type' (file/folder), 'name', 'path', 'size', etc.
    """
    if not directory:
        return []
    
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
