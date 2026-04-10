#!/usr/bin/env python3
"""
Test script to check and fix server configurations for LLM and image generation
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from app.database import SessionLocal
from app.models import Setting
import httpx
import asyncio

def check_local_db():
    """Check local database configuration"""
    print("="*60)
    print("LOCAL DATABASE CHECK")
    print("="*60)
    db = SessionLocal()
    
    try:
        # Check image server URLs
        image_urls_setting = db.query(Setting).filter(Setting.key == "image_server_urls").first()
        if image_urls_setting and image_urls_setting.value:
            print(f"✓ Image Server URLs: {image_urls_setting.value}")
        else:
            print("✗ Image Server URLs NOT SET")
        
        # Check storage server URL
        storage_url_setting = db.query(Setting).filter(Setting.key == "image_server_urls").first()
        if storage_url_setting and storage_url_setting.value:
            print(f"✓ Storage Server URL: {storage_url_setting.value}")
        else:
            print("✗ Storage Server URL NOT SET")
        
        # Check ComfyUI URL
        comfyui_setting = db.query(Setting).filter(Setting.key == "comfyui_url").first()
        if comfyui_setting and comfyui_setting.value:
            print(f"✓ ComfyUI URL: {comfyui_setting.value}")
        else:
            print("✗ ComfyUI URL NOT SET")
        
        return None
    
    finally:
        db.close()

async def test_server(server_url):
    """Test a remote server"""
    print(f"\n{'='*60}")
    print(f"TESTING: {server_url}")
    print(f"{'='*60}")
    
    # Server-to-server requests use load-balanced header
    headers = {"X-Posterchanai-Load-Balanced": "true"}
    print("Using load-balanced header for server-to-server authentication")
    
    # Test 1: /v1/models endpoint (chat health check)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{server_url}/v1/models", headers=headers)
            print(f"Chat API (/v1/models): {response.status_code}")
            if response.status_code == 401:
                print("✗ 401 Unauthorized - Chat authentication failed!")
                return False
            elif response.status_code == 200:
                print("✓ Chat authentication successful!")
            else:
                print(f"⚠️  Unexpected status: {response.status_code}")
    except Exception as e:
        print(f"✗ Chat health check failed: {e}")
    
    # Test 2: Image generation endpoint (should authenticate)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            payload = {"prompt": "test"}
            response = await client.post(
                f"{server_url}/api/generate-image",
                json=payload,
                headers=headers
            )
            print(f"Image API status: {response.status_code}")
            if response.status_code == 401:
                print("✗ 401 Unauthorized - Image authentication failed!")
                try:
                    error_data = response.json()
                    print(f"  Error: {error_data.get('detail', 'Unknown')}")
                except:
                    pass
                return False
            elif response.status_code == 200:
                result = response.json()
                if result.get("error"):
                    print(f"⚠️  Auth OK but generation failed: {result.get('error')}")
                else:
                    print("✓ Image authentication successful!")
                return True
            else:
                print(f"⚠️  Unexpected status: {response.status_code}")
                return False
    except httpx.ConnectError:
        print("✗ Cannot connect to server")
        return False
    except Exception as e:
        print(f"✗ Error: {e}")
        return False

async def main():
    print("\n" + "="*60)
    print("SERVER CONFIGURATION TEST")
    print("="*60 + "\n")
    
    # Check local database
    check_local_db()
    
    # Test servers
    servers_to_test = [
        "http://192.168.0.1:3051",
        "http://192.168.0.1:3052",
    ]
    
    print("\n" + "="*60)
    print("TESTING REMOTE SERVERS")
    print("="*60)
    
    results = {}
    for server in servers_to_test:
        results[server] = await test_server(server)
    
    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    for server, success in results.items():
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"{status}: {server}")
    
    if not all(results.values()):
        print("\n⚠️  Some servers failed authentication!")
        print("   Server-to-server requests use load-balanced header authentication")

if __name__ == "__main__":
    asyncio.run(main())
