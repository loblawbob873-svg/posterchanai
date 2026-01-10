"""YouTube Service - Fetch and summarize video transcripts"""

import re
import logging
from typing import Optional, Tuple
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

logger = logging.getLogger(__name__)


def extract_video_id(url: str) -> Optional[str]:
    """Extract YouTube video ID from various URL formats"""
    patterns = [
        r'(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/embed\/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com\/shorts\/)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def is_youtube_url(text: str) -> bool:
    """Check if text contains a YouTube URL"""
    return bool(re.search(r'(youtube\.com|youtu\.be)', text))


def get_transcript(video_id: str) -> Optional[str]:
    """Fetch transcript for a YouTube video"""
    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        # Combine all transcript segments
        full_text = ' '.join([entry['text'] for entry in transcript_list])
        return full_text
    except TranscriptsDisabled:
        logger.warning(f"Transcripts disabled for video {video_id}")
        return None
    except NoTranscriptFound:
        logger.warning(f"No transcript found for video {video_id}")
        return None
    except Exception as e:
        logger.error(f"Error fetching transcript for {video_id}: {e}")
        return None


def extract_youtube_urls(text: str) -> list[str]:
    """Extract all YouTube URLs from text"""
    pattern = r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[a-zA-Z0-9_-]+[^\s]*)'
    return re.findall(pattern, text)


async def summarize_youtube(url: str, chat_service) -> Tuple[bool, str]:
    """
    Fetch transcript and summarize a YouTube video.
    Returns (success, result_message)
    """
    video_id = extract_video_id(url)
    if not video_id:
        return False, "Could not extract video ID from URL"

    transcript = get_transcript(video_id)
    if not transcript:
        return False, "Could not fetch transcript. The video may not have captions available."

    # Truncate transcript if too long (keep ~10k chars for context)
    if len(transcript) > 10000:
        transcript = transcript[:10000] + "..."

    # Use chat service to summarize
    messages = [
        {"role": "system", "content": "You are a helpful assistant that summarizes video transcripts. Provide a clear, concise summary highlighting the main points, key takeaways, and any important details."},
        {"role": "user", "content": f"Please summarize this YouTube video transcript:\n\n{transcript}"}
    ]

    try:
        summary = await chat_service.chat(messages)
        return True, f"## Video Summary\n\n{summary}"
    except Exception as e:
        logger.error(f"Error summarizing transcript: {e}")
        return False, f"Error summarizing video: {str(e)}"
