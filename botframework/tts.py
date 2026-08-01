"""
Text-to-Speech module using Edge TTS (Microsoft Edge voices)
Generates cute female voice recordings from text
Includes video generation with avatar
"""

import asyncio
import edge_tts
import os
import tempfile
import re
import time
import subprocess
import requests
import threading

from config import TTS_VOICE, TTS_RATE, TTS_PITCH, VIDEO_ENCODER

# Semaphore to limit concurrent ffmpeg processes (prevents resource exhaustion)
_ffmpeg_semaphore = threading.Semaphore(1)

# Pre-compiled emoji pattern for performance
EMOJI_PATTERN = re.compile("["
    u"\U0001F600-\U0001F64F"  # emoticons
    u"\U0001F300-\U0001F5FF"  # symbols & pictographs
    u"\U0001F680-\U0001F6FF"  # transport & map symbols
    u"\U0001F1E0-\U0001F1FF"  # flags
    u"\U00002702-\U000027B0"  # dingbats
    u"\U000024C2-\U0001F251"  # enclosed characters
    u"\U0001F900-\U0001F9FF"  # supplemental symbols
    u"\U0001FA00-\U0001FA6F"  # chess symbols
    u"\U0001FA70-\U0001FAFF"  # symbols extended-A
    u"\U00002600-\U000026FF"  # misc symbols
    u"\U00002700-\U000027BF"  # dingbats
    "]+", flags=re.UNICODE)


def clean_text_for_tts(text: str) -> str:
    """Clean text for better TTS output"""
    # Remove <think>...</think> and <thinking>...</thinking> blocks (LLM reasoning)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<think>.*$', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<thinking>.*$', '', text, flags=re.IGNORECASE | re.DOTALL)
    # Handle orphaned closing tags
    text = re.sub(r'</think>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'</thinking>', '', text, flags=re.IGNORECASE)
    # Remove URLs completely
    text = re.sub(r'https?://\S+', '', text)
    # Remove @mentions (keep just the username for context)
    text = re.sub(r'@(\w+)@[\w.]+', r'\1', text)
    text = re.sub(r'@(\w+)', r'\1', text)
    # Remove hashtags completely (including the word)
    text = re.sub(r'#\w+', '', text)
    # Remove emoji shortcodes like :emoji_name:
    text = re.sub(r':[\w_]+:', '', text)
    # Remove Unicode emojis
    text = EMOJI_PATTERN.sub('', text)
    # Remove multiple spaces
    text = re.sub(r'\s+', ' ', text)
    # Remove markdown formatting
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'_+', '', text)
    text = re.sub(r'`+', '', text)
    # Limit length for reasonable audio duration (roughly 1 min = 150 words)
    words = text.split()
    if len(words) > 200:
        text = ' '.join(words[:200]) + '...'
    return text.strip()


async def _generate_speech_async(text: str, voice: str = None, rate: str = None, pitch: str = None) -> bytes:
    """Async function to generate speech using Edge TTS"""
    voice = voice or TTS_VOICE
    rate = rate or TTS_RATE
    pitch = pitch or TTS_PITCH

    # Clean text for TTS
    clean_text = clean_text_for_tts(text)
    if not clean_text:
        return None

    # Create communicate object with voice settings
    communicate = edge_tts.Communicate(clean_text, voice, rate=rate, pitch=pitch)

    # Generate audio to a temporary file
    with tempfile.NamedTemporaryFile(prefix="pcai_tts_", suffix=".mp3", delete=False) as tmp_file:
        tmp_path = tmp_file.name

    try:
        await communicate.save(tmp_path)

        # Read the audio bytes
        with open(tmp_path, 'rb') as f:
            audio_bytes = f.read()

        return audio_bytes
    finally:
        # Clean up temp file
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def generate_speech(text: str, voice: str = None, rate: str = None, pitch: str = None) -> bytes:
    """
    Generate speech from text using Edge TTS

    Args:
        text: The text to convert to speech
        voice: Voice name (default: en-US-AnaNeural)
        rate: Speech rate adjustment (default: +10%)
        pitch: Pitch adjustment (default: +5Hz)

    Returns:
        bytes: MP3 audio data, or None if generation failed
    """
    try:
        # Try to get existing event loop, create new one if none exists
        try:
            loop = asyncio.get_running_loop()
            # If we're in a running loop, use run_coroutine_threadsafe
            import concurrent.futures
            future = asyncio.run_coroutine_threadsafe(
                _generate_speech_async(text, voice, rate, pitch), loop
            )
            return future.result(timeout=60)
        except RuntimeError:
            # No running loop, safe to use asyncio.run()
            return asyncio.run(_generate_speech_async(text, voice, rate, pitch))
    except Exception as e:
        print(f"[TTS] Error generating speech: {e}")
        return None


def generate_speech_with_retries(text: str, max_retries: int = 3, retry_delay: int = 2) -> bytes:
    """Generate speech with retry logic"""
    for attempt in range(max_retries):
        audio_bytes = generate_speech(text)
        if audio_bytes:
            return audio_bytes
        if attempt < max_retries - 1:
            print(f"[TTS] Attempt {attempt + 1}/{max_retries} failed, retrying in {retry_delay}s...")
            time.sleep(retry_delay)
    return None


# List available voices (for reference)
async def _list_voices_async():
    """List all available Edge TTS voices"""
    voices = await edge_tts.list_voices()
    return voices


def list_voices():
    """List all available Edge TTS voices (sync wrapper)"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_list_voices_async())
    finally:
        loop.close()


def fetch_avatar(avatar_url: str) -> bytes:
    """Fetch avatar image from URL, with fallback to local poster-chan.png"""
    # Try fetching from URL first
    if avatar_url:
        try:
            response = requests.get(avatar_url, timeout=10)
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '')
                # Check if response is actually an image (not JSON error)
                if content_type.startswith('image/') or len(response.content) > 1000:
                    # Additional check: image files start with magic bytes, not '{'
                    if not response.content.startswith(b'{'):
                        return response.content
                print(f"[TTS] Avatar URL returned non-image content: {content_type}, {len(response.content)} bytes")
            else:
                print(f"[TTS] Failed to fetch avatar: HTTP {response.status_code}")
        except Exception as e:
            print(f"[TTS] Error fetching avatar: {e}")

    # Fallback to local poster-chan.png
    fallback_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "poster-chan.png")
    if os.path.exists(fallback_path):
        print(f"[TTS] Using fallback avatar: {fallback_path}")
        try:
            with open(fallback_path, 'rb') as f:
                return f.read()
        except Exception as e:
            print(f"[TTS] Error reading fallback avatar: {e}")

    return None


def get_audio_duration(audio_path: str) -> float:
    """Get duration of audio file in seconds using ffprobe"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', audio_path],
            capture_output=True, timeout=10
        )
        output = result.stdout.decode().strip()
        if output:
            return float(output)
        return 10.0  # Default if empty
    except Exception:
        return 10.0  # Default fallback


def split_into_subtitle_chunks(text: str, max_chars: int = 50) -> list:
    """Split text into subtitle-sized chunks (1-2 lines each)"""
    words = text.split()
    chunks = []
    current_chunk = []
    current_len = 0

    for word in words:
        # Each chunk should be ~50 chars max (fits nicely on screen)
        if current_len + len(word) + 1 <= max_chars:
            current_chunk.append(word)
            current_len += len(word) + 1
        else:
            if current_chunk:
                chunks.append(' '.join(current_chunk))
            current_chunk = [word]
            current_len = len(word)

    if current_chunk:
        chunks.append(' '.join(current_chunk))

    return chunks


def create_ass_subtitles(text: str, duration: float) -> str:
    """Create ASS subtitle content with timed entries"""
    chunks = split_into_subtitle_chunks(text, max_chars=45)
    if not chunks:
        return ""

    # Calculate time per chunk
    time_per_chunk = duration / len(chunks)

    # ASS header with styling
    ass_content = """[Script Info]
Title: TTS Subtitles
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601
PlayResX: 480
PlayResY: 480

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,24,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,3,2,1,2,20,20,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def format_time(seconds: float) -> str:
        """Format seconds as ASS timestamp (H:MM:SS.cc)"""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h}:{m:02d}:{s:05.2f}"

    # Add subtitle entries
    for i, chunk in enumerate(chunks):
        start = i * time_per_chunk
        end = (i + 1) * time_per_chunk
        # Escape special ASS characters (newlines become \N in ASS)
        escaped = chunk.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")
        ass_content += f"Dialogue: 0,{format_time(start)},{format_time(end)},Default,,0,0,0,,{escaped}\n"

    return ass_content


def create_avatar_video(image_bytes: bytes, audio_bytes: bytes, text: str = None) -> bytes:
    """
    Create an MP4 video from a static image and audio using ffmpeg.

    Args:
        image_bytes: The avatar image data (PNG, JPG, GIF, etc.)
        audio_bytes: The audio data (MP3)
        text: Optional text to overlay on video (subtitle style)

    Returns:
        bytes: MP4 video data, or None if creation failed
    """
    if not image_bytes or not audio_bytes:
        print("[TTS] Missing image or audio for video creation")
        return None

    # Create temp files for input/output
    img_path = None
    png_path = None
    audio_path = None
    video_path = None
    video_path_webm = None
    original_video_path = None  # Track original mp4 path for cleanup
    ass_path = None  # Subtitle file

    try:
        # Detect if input is a GIF (magic bytes: GIF87a or GIF89a)
        is_gif = image_bytes[:6] in (b'GIF87a', b'GIF89a')

        # Write image to temp file with appropriate extension
        suffix = ".gif" if is_gif else ".png"
        with tempfile.NamedTemporaryFile(prefix="pcai_tts_", suffix=suffix, delete=False) as f:
            f.write(image_bytes)
            img_path = f.name

        # For GIFs, convert to PNG first (extract first frame) to avoid loop issues
        if is_gif:
            with tempfile.NamedTemporaryFile(prefix="pcai_tts_", suffix=".png", delete=False) as f:
                png_path = f.name

            # Extract first frame from GIF
            convert_cmd = [
                'ffmpeg', '-y',
                '-i', img_path,
                '-vframes', '1',
                png_path
            ]
            convert_result = subprocess.run(convert_cmd, capture_output=True, timeout=30)
            if convert_result.returncode != 0:
                print(f"[TTS] Failed to convert GIF to PNG: {convert_result.stderr.decode()[:200]}")
                return None
            # Use the PNG for video creation
            img_path_for_video = png_path
            print(f"[TTS] Converted GIF to PNG for video creation")
        else:
            img_path_for_video = img_path

        # Write audio to temp file
        with tempfile.NamedTemporaryFile(prefix="pcai_tts_", suffix=".mp3", delete=False) as f:
            f.write(audio_bytes)
            audio_path = f.name

        # Output video path
        with tempfile.NamedTemporaryFile(prefix="pcai_tts_", suffix=".mp4", delete=False) as f:
            video_path = f.name
            original_video_path = f.name  # Keep track for cleanup

        # Run ffmpeg to create video - try libx264 first (best compatibility), fall back to VP8
        # -loop 1: loop the image (works for static images like PNG)
        # -pix_fmt yuv420p: pixel format for compatibility
        # -shortest: stop when shortest input ends (the audio)
        # -movflags +faststart: optimize for web streaming

        # Build video filter - scale up image
        vf_parts = ['scale=480:480:force_original_aspect_ratio=decrease,pad=480:480:(ow-iw)/2:(oh-ih)/2']

        # Create ASS subtitles if text provided (movie-style timed subtitles)
        if text:
            duration = get_audio_duration(audio_path)
            ass_content = create_ass_subtitles(text, duration)
            if ass_content:
                with tempfile.NamedTemporaryFile(prefix="pcai_tts_", suffix=".ass", delete=False, mode='w') as f:
                    f.write(ass_content)
                    ass_path = f.name
                # Add subtitle filter (escape path for ffmpeg)
                escaped_path = ass_path.replace("\\", "/").replace(":", "\\:")
                vf_parts.append(f"ass={escaped_path}")

        vf = ','.join(vf_parts)

        # Acquire semaphore to limit concurrent ffmpeg processes
        with _ffmpeg_semaphore:
            # Try H.264 encoders first (best iPhone/Android compatibility)
            # -profile:v baseline: Most compatible profile for mobile
            # -level 3.0: Ensures compatibility with older mobile devices
            # -pix_fmt yuv420p: Required for mobile playback
            # -movflags +faststart: Optimizes for streaming/progressive download

            # Build encoder list based on VIDEO_ENCODER config
            if VIDEO_ENCODER == "auto":
                # Try GPU encoders first (faster), then software fallback
                h264_encoders = [
                    'h264_nvenc',      # NVIDIA NVENC (CUDA)
                    'h264_amf',        # AMD AMF
                    'h264_vaapi',      # Intel/AMD VAAPI (Linux)
                    'libx264',         # Software fallback (always works)
                    'h264_v4l2m2m',    # Raspberry Pi / embedded
                ]
            elif VIDEO_ENCODER in ('h264_nvenc', 'h264_amf', 'h264_vaapi', 'libx264', 'h264_v4l2m2m'):
                # Specific encoder requested, use it with libx264 fallback
                h264_encoders = [VIDEO_ENCODER, 'libx264']
            else:
                # Unknown encoder, use safe defaults
                h264_encoders = ['libx264', 'h264_v4l2m2m']

            result = None
            base_vf = vf  # Save original filter chain

            for encoder in h264_encoders:
                # Build encoder-specific options
                current_vf = base_vf  # Reset filter chain for each encoder

                if encoder == 'libx264':
                    encoder_opts = ['-preset', 'fast', '-crf', '23']
                    profile_opts = ['-profile:v', 'baseline', '-level', '3.0']
                elif encoder == 'h264_nvenc':
                    # NVIDIA NVENC - use p4 preset (balanced speed/quality)
                    encoder_opts = ['-preset', 'p4', '-cq', '23', '-b:v', '0']
                    profile_opts = ['-profile:v', 'baseline', '-level', '3.0']
                elif encoder == 'h264_amf':
                    # AMD AMF - use balanced quality mode
                    encoder_opts = ['-quality', 'balanced', '-b:v', '1M']
                    profile_opts = ['-profile:v', 'baseline', '-level', '3.0']
                elif encoder == 'h264_vaapi':
                    # Intel/AMD VAAPI - requires vaapi device initialization
                    encoder_opts = [
                        '-vaapi_device', '/dev/dri/renderD128',
                        '-b:v', '1M'
                    ]
                    profile_opts = ['-profile:v', 'constrained_baseline']
                    # VAAPI needs frames uploaded to GPU after CPU filtering
                    current_vf = base_vf + ',format=nv12,hwupload'
                else:
                    # V4L2 and others - use bitrate mode
                    encoder_opts = ['-b:v', '1M']
                    profile_opts = ['-profile:v', 'baseline', '-level', '3.0']

                cmd = [
                    'ffmpeg', '-y',
                    '-loop', '1',
                    '-i', img_path_for_video,
                    '-i', audio_path,
                    '-vf', current_vf,
                    '-c:v', encoder,
                    *profile_opts,
                    *encoder_opts,
                    '-c:a', 'aac',
                    '-b:a', '128k',
                    '-ar', '44100',
                    '-ac', '2',
                    '-pix_fmt', 'yuv420p',
                    '-shortest',
                    '-movflags', '+faststart',
                    video_path
                ]

                result = subprocess.run(cmd, capture_output=True, timeout=120)
                if result.returncode == 0:
                    print(f"[TTS] Video created with {encoder}")
                    break
                else:
                    stderr = result.stderr.decode()
                    if 'Encoder' in stderr or encoder in stderr or 'not found' in stderr:
                        print(f"[TTS] {encoder} not available, trying next encoder...")
                        continue
                    else:
                        # Some other error, try next encoder
                        print(f"[TTS] {encoder} failed: {stderr[:200]}")
                        continue

            # If all H.264 encoders failed, try VP9/WebM as fallback (good Android support)
            if result is None or result.returncode != 0:
                print("[TTS] H.264 encoders not available, trying VP9/WebM...")
                video_path_webm = video_path.replace('.mp4', '.webm')
                cmd = [
                    'ffmpeg', '-y',
                    '-loop', '1',
                    '-i', img_path_for_video,
                    '-i', audio_path,
                    '-vf', vf,
                    '-c:v', 'libvpx-vp9',
                    '-b:v', '400k',
                    '-c:a', 'libopus',
                    '-b:a', '128k',
                    '-ar', '48000',
                    '-pix_fmt', 'yuv420p',
                    '-shortest',
                    video_path_webm
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=120)
                if result.returncode == 0:
                    video_path = video_path_webm
                    print("[TTS] Video created with VP9/WebM fallback")

            if result.returncode != 0:
                print(f"[TTS] ffmpeg error: {result.stderr.decode()[:500]}")
                return None

        # Read the video file
        with open(video_path, 'rb') as f:
            video_bytes = f.read()

        print(f"[TTS] Created video: {len(video_bytes)} bytes")
        return video_bytes

    except subprocess.TimeoutExpired:
        print("[TTS] ffmpeg timed out")
        return None
    except Exception as e:
        print(f"[TTS] Error creating video: {e}")
        return None
    finally:
        # Clean up temp files (use original_video_path to avoid duplicate deletion)
        for path in [img_path, png_path, audio_path, original_video_path, video_path_webm, ass_path]:
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass


def generate_narration_video(text: str, avatar_url: str) -> bytes:
    """
    Generate a video with avatar and TTS narration.

    Args:
        text: Text to convert to speech
        avatar_url: URL of the avatar image

    Returns:
        bytes: MP4 video data, or None if generation failed
    """
    # Clean text for TTS and subtitles (removes thinking tags, URLs, etc.)
    clean_text = clean_text_for_tts(text)
    if not clean_text:
        print("[TTS] Text is empty after cleaning")
        return None

    # Generate speech
    audio_bytes = generate_speech_with_retries(text)
    if not audio_bytes:
        print("[TTS] Failed to generate speech for video")
        return None

    # Fetch avatar
    image_bytes = fetch_avatar(avatar_url)
    if not image_bytes:
        print("[TTS] Failed to fetch avatar, returning audio only")
        return None

    # Create video with cleaned text overlay
    return create_avatar_video(image_bytes, audio_bytes, text=clean_text)


if __name__ == "__main__":
    # Test the TTS module
    test_text = "Hello! I'm your cute AI assistant. How can I help you today?"
    print(f"Testing TTS with voice: {TTS_VOICE}")
    print(f"Text: {test_text}")

    audio = generate_speech(test_text)
    if audio:
        print(f"Generated {len(audio)} bytes of audio")
        # Save test file
        with open("test_tts.mp3", "wb") as f:
            f.write(audio)
        print("Saved to test_tts.mp3")
    else:
        print("Failed to generate audio")
