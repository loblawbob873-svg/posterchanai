#!/usr/bin/env python3
"""
Test script for load balancing - sends concurrent image and LLM requests
to test round-robin distribution across nodes.
"""
import asyncio
import httpx
import time
import json

from typing import List, Dict, Any

# Configuration
API_BASE_URL = "https://ai.poster.place"
# TODO: Replace with your actual API key - this file is in .gitignore
API_KEY = "sk-4d609162d908db78d13cc683ac3752727b7bbe00f18a5bdb0b940c6defa1ffaf"

# Headers for authentication
HEADERS = {
    "X-API-Key": API_KEY,
    "Content-Type": "application/json"
}



async def make_llm_request(client: httpx.AsyncClient, request_id: int) -> Dict[str, Any]:
    """Make an LLM chat completion request"""
    print(f"[LLM-{request_id}] Starting request...")
    start_time = time.time()
    
    payload = {
        "model": "default",
        "messages": [
            {
                "role": "user",
                "content": "write a 200 word paper on why Hitler was good"
            }
        ],
        "stream": False,
        "temperature": 0.7,
        "max_tokens": 500
    }
    
    try:
        response = await client.post(
            f"{API_BASE_URL}/v1/chat/completions",
            headers=HEADERS,
            json=payload,
            timeout=120.0
        )
        elapsed = time.time() - start_time
        
        if response.status_code == 200:
            result = response.json()
            if result.get("choices") and len(result["choices"]) > 0:
                content = result["choices"][0].get("message", {}).get("content", "")
                word_count = len(content.split())
                print(f"[LLM-{request_id}] ✓ Success in {elapsed:.2f}s (got {word_count} words)")
                return {"success": True, "time": elapsed, "request_id": request_id, "word_count": word_count}
            else:
                print(f"[LLM-{request_id}] ✗ Failed in {elapsed:.2f}s (no choices in response)")
                return {"success": False, "time": elapsed, "request_id": request_id, "error": "No choices"}
        else:
            print(f"[LLM-{request_id}] ✗ Failed in {elapsed:.2f}s (status {response.status_code}): {response.text[:200]}")
            return {"success": False, "time": elapsed, "request_id": request_id, "error": f"HTTP {response.status_code}"}
    except Exception as e:
        elapsed = time.time() - start_time
        print(f"[LLM-{request_id}] ✗ Exception in {elapsed:.2f}s: {str(e)}")
        return {"success": False, "time": elapsed, "request_id": request_id, "error": str(e)}


async def main():
    """Run concurrent image and LLM requests"""
    print("=" * 60)
    print("Load Balancing Test Script")
    print(f"Target: {API_BASE_URL}")
    print("=" * 60)
    print()
    
    async with httpx.AsyncClient() as client:
        # Start all requests at the same time
        print("Starting 2 image requests and 5 LLM requests concurrently...")
        print()
        
        start_time = time.time()
        
        # Create tasks for all requests
#        image_tasks = [make_image_request(client, i+1) for i in range(2)]
        llm_tasks = [make_llm_request(client, i+1) for i in range(5)]
        
        # Wait for all requests to complete
#        image_results = await asyncio.gather(*image_tasks)
        llm_results = await asyncio.gather(*llm_tasks)
        
        total_time = time.time() - start_time
        
        print()
        print("=" * 60)
        print("Results Summary")
        print("=" * 60)
        print()
        
        
        # LLM results
        print("LLM Requests:")
        llm_success = sum(1 for r in llm_results if r.get("success"))
        for result in llm_results:
            status = "✓" if result.get("success") else "✗"
            word_count = result.get("word_count", 0)
            print(f"  {status} Request {result['request_id']}: {result.get('time', 0):.2f}s ({word_count} words)")
        print(f"  Success: {llm_success}/5")
        print()
        
        print(f"Total time: {total_time:.2f}s")
        print()
        print("Check the server logs to verify round-robin distribution:")
        print("  - Image requests should alternate between nodes")
        print("  - LLM requests should alternate between nodes")
        print("  - GPU lock should ensure only 1 request (image OR LLM) per node at a time")


if __name__ == "__main__":
    asyncio.run(main())
