# Video Streaming with On-The-Fly Transcoding ✅

## What Changed

Videos are now **transcoded in real-time and streamed directly** to save bandwidth, **WITHOUT saving to disk**.

## How It Works

### When You Play a Video:

1. **User clicks play** on any video (.mov, .mp4, .avi, etc.)
2. **FFmpeg starts** transcoding the video in the background
3. **Video streams** to your browser as it's being transcoded
4. **Nothing is saved** to disk - pure streaming!
5. **Next time**: Transcodes again from scratch (no storage used)

### Technical Details

**Transcoding Settings:**
- **Video Codec**: H.264 (libx264) - universally compatible
- **Audio Codec**: AAC at 128kbps
- **Preset**: veryfast (optimized for real-time streaming)
- **Max Bitrate**: 2 Mbps (bandwidth control)
- **Quality**: CRF 23 (good balance of quality/size)
- **Container**: MP4 with streaming flags

**Bandwidth Savings:**
- Original iPhone/HEVC videos: ~100-500 MB
- Transcoded H.264 stream: ~40-60% smaller
- Typical savings: 40-200 MB per video

## Benefits

✅ **Zero storage overhead** - Nothing saved to disk  
✅ **Bandwidth savings** - 40-60% smaller than original  
✅ **Universal compatibility** - H.264 works on all browsers  
✅ **Real-time streaming** - No waiting for pre-transcoding  
✅ **Automatic** - Happens transparently when you play a video

## Trade-offs

⚠️ **CPU usage** - Server transcodes every time video is played  
⚠️ **First-frame delay** - ~1-2 second delay before playback starts  
⚠️ **No seeking** - Can't skip forward/backward during first play  

## If You Want to Cache Transcoded Videos

If you'd rather save transcoded videos to disk for faster replays:

1. Comment out the new streaming code
2. Uncomment the old caching code
3. Transcoded videos will be saved in `.transcoded/` folder
4. Trade-off: Uses storage but faster on replays

## Logs

When playing a video, you'll see:
```
[STORAGE] Streaming transcoded video on-the-fly: myvideo.mov
```

## Test It

1. Open Photo Gallery: `http://192.168.0.1:3051`
2. Click any video file
3. Should start playing within 1-2 seconds
4. Check bandwidth: Should be ~40-60% less than file size
5. Check storage: No `.transcoded/` files created

## FFmpeg Command Used

```bash
ffmpeg -i input.mov \
  -c:v libx264 -preset veryfast -crf 23 \
  -maxrate 2M -bufsize 4M \
  -c:a aac -b:a 128k \
  -movflags frag_keyframe+empty_moov+faststart \
  -f mp4 pipe:1
```

This streams H.264/AAC MP4 directly to stdout, which is then streamed to the browser.

---

**Status**: ✅ Deployed and active on storage server (192.168.0.85)
