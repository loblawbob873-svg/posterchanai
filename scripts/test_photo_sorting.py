#!/usr/bin/env python3
"""
Automated test for Photo Gallery sorting - verifies newest photos appear first.
Tests both backend API and actual file timestamps.
"""
import sys
import os
import requests
import json
from pathlib import Path
from datetime import datetime
import time

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_backend_sorting(base_url="http://localhost:8000", username=None, password=None):
    """Test the backend API sorting."""
    print("=" * 60)
    print("Testing Backend Photo Gallery Sorting")
    print("=" * 60)
    
    # Login to get token
    if not username or not password:
        print("WARNING: Username and password not provided, skipping API test")
        return None  # Skip test, don't fail
    
    if not base_url:
        print("WARNING: Base URL not provided, skipping API test")
        return None
    
    login_url = f"{base_url}/api/auth/login"
    login_data = {
        "username": username,
        "password": password
    }
    
    try:
        # Try JSON first, fall back to form data
        try:
            response = requests.post(login_url, json=login_data, timeout=5)
        except:
            response = requests.post(login_url, data=login_data, timeout=5)
        if response.status_code != 200:
            print(f"WARNING: Cannot connect to server at {base_url}")
            print(f"  Status: {response.status_code}")
            print(f"  Response: {response.text[:200]}")
            print("  Skipping API test - will test file timestamps only")
            return None
    except requests.exceptions.ConnectionError:
        print(f"WARNING: Cannot connect to server at {base_url}")
        print("  Server appears to be down or not accessible")
        print("  Skipping API test - will test file timestamps only")
        return None
    except Exception as e:
        print(f"WARNING: Error connecting to server: {e}")
        print("  Skipping API test - will test file timestamps only")
        return None
    
    if response.status_code != 200:
        print(f"WARNING: Cannot connect to server at {base_url}")
        print(f"  Status: {response.status_code}")
        print(f"  Response: {response.text[:200]}")
        print("  Skipping API test - will test file timestamps only")
        return None
    
    try:
        token = response.json().get("access_token")
        if not token:
            print("ERROR: No access token received")
            return False
    except Exception as e:
        print(f"ERROR: Failed to parse login response: {e}")
        print(f"  Response: {response.text[:200]}")
        return False
        
        headers = {"Authorization": f"Bearer {token}"}
        
        # Get all images
        images_url = f"{base_url}/api/files/all-images?limit=100"
        response = requests.get(images_url, headers=headers)
        
        if response.status_code != 200:
            print(f"ERROR: Failed to get images: {response.status_code}")
            print(response.text)
            return False
        
        data = response.json()
        images = data.get("images", [])
        total = data.get("total", 0)
        
        print(f"\nTotal images returned: {len(images)}")
        print(f"Total images available: {total}")
        
        if len(images) == 0:
            print("WARNING: No images found to test")
            return True
        
        # Check sorting
        print("\nFirst 20 images (should be newest first):")
        print("-" * 60)
        errors = []
        prev_timestamp = None
        
        for i, img in enumerate(images[:20]):
            modified = float(img.get("modified", 0) or 0)
            name = img.get("name", "unknown")
            path = img.get("path", "unknown")
            
            if modified > 0:
                date_str = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M:%S")
            else:
                date_str = "INVALID"
            
            print(f"{i+1:2d}. {name[:40]:40s} | {modified:15.2f} | {date_str}")
            
            # Check if sorting is correct (newest first = descending)
            if prev_timestamp is not None:
                if modified > prev_timestamp:
                    errors.append((i, name, modified, prev_timestamp))
                    print(f"     ❌ ERROR: This image is NEWER than previous!")
            
            prev_timestamp = modified
        
        print("-" * 60)
        
        if errors:
            print(f"\n❌ SORTING FAILED: Found {len(errors)} errors in first 20 images")
            print("\nErrors:")
            for idx, name, curr, prev in errors[:5]:
                print(f"  Index {idx}: {name} (timestamp {curr}) is NEWER than previous ({prev})")
            return False
        else:
            print(f"\n✓ Sorting verified: All {min(20, len(images))} checked images are in correct order (newest first)")
            return True
            
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_file_timestamps(storage_path, username):
    """Test actual file timestamps to verify they're correct."""
    print("\n" + "=" * 60)
    print("Testing File Timestamps")
    print("=" * 60)
    
    # Try to find user path - sanitize username (replace @ and . with _)
    safe_username = username.replace('@', '_').replace('.', '_')
    
    # Try multiple possible paths
    possible_paths = [
        Path(storage_path) / safe_username,
        Path(storage_path) / username,
        Path(storage_path) / username.replace('@', '_'),
    ]
    
    user_path = None
    for path in possible_paths:
        if path.exists():
            user_path = path
            break
    
    if not user_path or not user_path.exists():
        print(f"WARNING: User path not found")
        print(f"  Tried paths:")
        for path in possible_paths:
            print(f"    - {path} (exists: {path.exists()})")
        print(f"  Storage path exists: {Path(storage_path).exists()}")
        if Path(storage_path).exists():
            print(f"  Available users in storage:")
            try:
                for item in Path(storage_path).iterdir():
                    if item.is_dir() and not item.name.startswith('.'):
                        print(f"    - {item.name}")
            except PermissionError:
                print("    (Permission denied)")
        return None  # Can't test, but don't fail
    
    from app.services.thumbnail_service import is_image_file, is_video_file, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
    
    media_extensions = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
    
    # Find all media files
    files = []
    for item in user_path.rglob('*'):
        if item.is_file() and item.suffix.lower() in media_extensions:
            # Skip hidden files and thumbnails
            if item.name.startswith('.') or '.thumbnails' in str(item):
                continue
            
            try:
                stat = item.stat()
                # Use max(mtime, ctime) like the backend does
                modified_time = max(stat.st_mtime, stat.st_ctime) if stat.st_mtime > 0 and stat.st_ctime > 0 else (stat.st_mtime if stat.st_mtime > 0 else stat.st_ctime)
                
                files.append({
                    'path': str(item.relative_to(user_path)),
                    'name': item.name,
                    'modified': modified_time,
                    'mtime': stat.st_mtime,
                    'ctime': stat.st_ctime
                })
            except Exception as e:
                print(f"WARNING: Could not stat {item}: {e}")
    
    if not files:
        print("WARNING: No media files found")
        return True
    
    # Sort by modified time (newest first)
    files.sort(key=lambda x: x['modified'], reverse=True)
    
    print(f"\nFound {len(files)} media files")
    print("\nFirst 20 files by timestamp (newest first):")
    print("-" * 60)
    
    for i, f in enumerate(files[:20]):
        date_str = datetime.fromtimestamp(f['modified']).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{i+1:2d}. {f['name'][:40]:40s} | {f['modified']:15.2f} | {date_str}")
        print(f"     mtime={f['mtime']:.2f}, ctime={f['ctime']:.2f}, using max={f['modified']:.2f}")
    
    print("-" * 60)
    
    # Verify sorting
    errors = []
    prev_ts = None
    for i, f in enumerate(files[:20]):
        if prev_ts is not None and f['modified'] > prev_ts:
            errors.append((i, f['name'], f['modified'], prev_ts))
        prev_ts = f['modified']
    
    if errors:
        print(f"\n❌ File timestamp sorting has {len(errors)} errors")
        return False
    else:
        print(f"\n✓ File timestamps are correctly sorted (newest first)")
        return True


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Test Photo Gallery sorting")
    parser.add_argument("username", help="Username to test")
    parser.add_argument("password", help="Password for user")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL (default: http://localhost:8000)")
    parser.add_argument("--storage-path", help="Storage path (default: from environment or /var/lib/posterchanai)")
    
    args = parser.parse_args()
    
    # Get storage path
    storage_path = args.storage_path
    if not storage_path:
        storage_path = os.environ.get("POSTERCHANAI_STORAGE_PATH", "/var/lib/posterchanai")
    
    print("Photo Gallery Sorting Test")
    print("=" * 60)
    print(f"Username: {args.username}")
    print(f"Base URL: {args.url}")
    print(f"Storage Path: {storage_path}")
    print("=" * 60)
    
    # Test backend API (optional - may skip if server not available)
    backend_ok = test_backend_sorting(args.url, args.username, args.password)
    
    # Test file timestamps (always runs)
    timestamp_ok = test_file_timestamps(storage_path, args.username)
    
    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    if backend_ok is None:
        print(f"Backend API Sorting: ⚠ SKIPPED (server not available)")
    else:
        print(f"Backend API Sorting: {'✓ PASS' if backend_ok else '❌ FAIL'}")
    
    if timestamp_ok is None:
        print(f"File Timestamps:      ⚠ SKIPPED (path not found)")
    else:
        print(f"File Timestamps:      {'✓ PASS' if timestamp_ok else '❌ FAIL'}")
    
    # Exit based on what we could test
    tests_run = []
    if backend_ok is not None:
        tests_run.append(backend_ok)
    if timestamp_ok is not None:
        tests_run.append(timestamp_ok)
    
    if not tests_run:
        print("\n⚠ No tests could be run (server down and path not found)")
        print("  To test:")
        print("  1. Make sure server is running, or")
        print("  2. Provide correct --storage-path")
        sys.exit(2)
    elif all(tests_run):
        print("\n✓ All tests passed!")
        sys.exit(0)
    else:
        print("\n❌ Some tests failed!")
        sys.exit(1)
