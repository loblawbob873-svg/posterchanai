#!/bin/bash

API_KEY="sk-4d609162d908db78d13cc683ac3752727b7bbe00f18a5bdb0b940c6defa1ffaf"
BASE_URL="https://ai.poster.place"

echo "=== Sending 4 simultaneous requests (2 chat + 2 image) ==="

# Chat request 1
curl -s -X POST "$BASE_URL/api/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "model": "gemma-3-12b-it-vl-GPT-5.1-High-Heretic-Uncensored-Thinking.i1-Q4_K_M.gguf",
    "messages": [{"role": "user", "content": "Hello, respond with just \"chat1\"."}],
    "max_tokens": 50
  }' &
PID1=$!

# Chat request 2
curl -s -X POST "$BASE_URL/api/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "model": "gemma-3-12b-it-vl-GPT-5.1-High-Heretic-Uncensored-Thinking.i1-Q4_K_M.gguf",
    "messages": [{"role": "user", "content": "Hello, respond with just \"chat2\"."}],
    "max_tokens": 50
  }' &
PID2=$!

# Image request 1
curl -s -X POST "$BASE_URL/api/generate-image" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "prompt": "a small red apple on white background, simple",
    "width": 512,
    "height": 512,
    "steps": 20
  }' &
PID3=$!

# Image request 2
curl -s -X POST "$BASE_URL/api/generate-image" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_KEY" \
  -d '{
    "prompt": "a small blue circle on white background, simple",
    "width": 512,
    "height": 512,
    "steps": 20
  }' &
PID4=$!

echo "Sent requests, waiting for responses..."
echo ""

# Wait for all to complete
wait $PID1
RESULT1=$?
echo "Chat 1 completed (exit code: $RESULT1)"

wait $PID2
RESULT2=$?
echo "Chat 2 completed (exit code: $RESULT2)"

wait $PID3
RESULT3=$?
echo "Image 1 completed (exit code: $RESULT3)"

wait $PID4
RESULT4=$?
echo "Image 2 completed (exit code: $RESULT4)"

echo ""
echo "=== All requests completed ==="