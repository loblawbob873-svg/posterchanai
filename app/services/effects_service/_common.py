"""Auto-split from the original effects_service.py monolith. No behavior change."""
"""Creative image effects — the "Effects" group: meme captions, the dildo / poo
scatter gags, the BLACKED wordmark and the KOSHER seal.

Split out of media_service so the byte-level transforms (compress/clip/convert/PDF)
stay separate from these Pillow-drawn novelty overlays. All three expose the same
``(output_files, summary)`` shape as the media_service ``*_attachments`` processors,
so the web UI, Telegram and the fedi bots deliver them through one path. The
dildo/poo tiles are drawn entirely in Pillow (no shipped image assets), reusing the
``_shade`` / ``_gradient_*`` shading primitives below.
"""
import io
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Tuple
from app.services.media_service import OutputFile, _human_size, is_image
logger = logging.getLogger(__name__)
_MEME_FONT_CANDIDATES = [
    "/usr/share/fonts/impact/impact.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
]
_SCATTER_ANIM_FRAMES = 36
_SCATTER_ANIM_FPS = 14
_SCATTER_ANIM_MAXDIM = 1280   # cap the working long edge so re-render + encode stay cheap
_SCATTER_ANIM_MAXTILES = 30   # cap tile count for animation (the still allows up to 60)
_DILDO_COLORS = [
    (240, 200, 175, 255),  # light flesh
    (212, 162, 130, 255),  # medium flesh
    (168, 120, 95, 255),   # dark flesh
    (246, 150, 182, 255),  # pink
    (152, 92, 200, 255),   # purple
]
_POO_COLORS = [
    (110, 70, 36, 255),    # classic brown
    (92, 58, 30, 255),     # medium brown
    (74, 47, 26, 255),     # dark chocolate
    (128, 84, 44, 255),    # light brown
]
_CUM_COLORS = [
    (246, 244, 236, 255),  # cream white
    (238, 236, 228, 255),  # off white
    (250, 249, 244, 255),  # bright white
    (240, 238, 224, 255),  # warm ivory
]
_BLOOD_COLORS = [
    (140, 8, 8, 255),     # crimson
    (110, 3, 3, 255),     # dark red
    (92, 6, 6, 255),      # dried red
    (165, 16, 12, 255),   # bright arterial
]
_FIRE_ANIM_FRAMES = 20
_FIRE_ANIM_FPS = 14
_FIRE_ANIM_LOOPS = 3
# `nakedman` — a fat cartoon man dancing over the input image, set to an 8s audio clip.
# One dance cycle is _NAKEDMAN_ANIM_FRAMES frames (wraps seamlessly on phase 0→tau); the
# whole pass is repeated _NAKEDMAN_ANIM_LOOPS times on disk so total = 40*4 = 160 frames
# at 20fps = 8.0s of animation, matched to the 8s (looped) audio track.
_NAKEDMAN_ANIM_FRAMES = 40
_NAKEDMAN_ANIM_FPS = 20
_NAKEDMAN_ANIM_LOOPS = 4
_NAKEDMAN_DURATION = 8.0
_BLACKED_FONT_CANDIDATES = [
    "/usr/share/fonts/archivo-black/ArchivoBlack-Regular.ttf",
    "/usr/share/fonts/truetype/archivo-black/ArchivoBlack-Regular.ttf",
    "/usr/share/fonts/roboto/Roboto-Black.ttf",
    "/usr/share/fonts/truetype/roboto/Roboto-Black.ttf",
    "/usr/share/fonts/montserrat/Montserrat-Black.ttf",
    "/usr/share/fonts/msttcorefonts/Arial_Black.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Black.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
# This module lives at app/services/effects_service/_common.py, so the repo root is FOUR
# levels up (_common.py -> effects_service -> services -> app -> repo). (Was three before the
# effects_service.py monolith became this package.)
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_NAKEDMAN_AUDIO_CANDIDATES = [
    os.environ.get("NAKEDMAN_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "nakedman.mp3"),
    "/var/lib/posterchanai/assets/nakedman.mp3",
]
_HAVA_AUDIO_CANDIDATES = [
    os.environ.get("HAVA_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "hava.mp3"),
    "/var/lib/posterchanai/assets/hava.mp3",
]
_HAVA_DURATION = 6.0
_INDIAN_AUDIO_CANDIDATES = [
    os.environ.get("INDIAN_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "indian.mp3"),
    "/var/lib/posterchanai/assets/indian.mp3",
]
_INDIAN_DURATION = 6.0
_YAKETY_AUDIO_CANDIDATES = [
    os.environ.get("YAKETY_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "yakety.mp3"),
    "/var/lib/posterchanai/assets/yakety.mp3",
]
_YAKETY_DURATION = 9.0
_YAMETE_AUDIO_CANDIDATES = [
    os.environ.get("YAMETE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "yamete.mp3"),
    "/var/lib/posterchanai/assets/yamete.mp3",
]
_YAMETE_DURATION = 6.0
_CURB_AUDIO_CANDIDATES = [
    os.environ.get("CURB_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "curb.mp3"),
    "/var/lib/posterchanai/assets/curb.mp3",
]
_CURB_DURATION = 14.0
_DEPRESSING_AUDIO_CANDIDATES = [
    os.environ.get("DEPRESSING_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "depressing.mp3"),
    "/var/lib/posterchanai/assets/depressing.mp3",
]
_DEPRESSING_DURATION = 10.0
_FAHH_AUDIO_CANDIDATES = [
    os.environ.get("FAHH_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "fahh.mp3"),
    "/var/lib/posterchanai/assets/fahh.mp3",
]
# Measured from assets/fahh.mp3: dead air until ~0.45s, the "fahh" peaks 1.0-1.5s (-17.5 dB), then a
# decaying tail to 3.24s. Padding that to 5.0s meant well over half the clip was silence. Skip the
# lead-in and cut shortly after the sound is spent.
_FAHH_DURATION = 2.2
_FAHH_AUDIO_START = 0.45
_HELPME_AUDIO_CANDIDATES = [
    os.environ.get("HELPME_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "helpme.mp3"),
    "/var/lib/posterchanai/assets/helpme.mp3",
]
_HELPME_DURATION = 5.0
_GONG_AUDIO_CANDIDATES = [
    os.environ.get("GONG_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "gong.mp3"),
    "/var/lib/posterchanai/assets/gong.mp3",
]
_GONG_DURATION = 5.0
_FBI_AUDIO_CANDIDATES = [
    os.environ.get("FBI_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "fbi.mp3"),
    "/var/lib/posterchanai/assets/fbi.mp3",
]
_FBI_DURATION = 8.0
_REDEEM_AUDIO_CANDIDATES = [
    os.environ.get("REDEEM_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "redeem.mp3"),
    "/var/lib/posterchanai/assets/redeem.mp3",
]
_REDEEM_DURATION = 9.0
_GIGITY_AUDIO_CANDIDATES = [
    os.environ.get("GIGITY_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "gigity.mp3"),
    "/var/lib/posterchanai/assets/gigity.mp3",
]
_GIGITY_DURATION = 3.0
_BEAVIS_AUDIO_CANDIDATES = [
    os.environ.get("BEAVIS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "beavis.mp3"),
    "/var/lib/posterchanai/assets/beavis.mp3",
]
_BEAVIS_DURATION = 12.0
_SMELL_AUDIO_CANDIDATES = [
    os.environ.get("SMELL_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "smell.mp3"),
    "/var/lib/posterchanai/assets/smell.mp3",
]
_SMELL_DURATION = 5.0
_HOOD_AUDIO_CANDIDATES = [
    os.environ.get("HOOD_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "hood.mp3"),
    "/var/lib/posterchanai/assets/hood.mp3",
]
_HOOD_DURATION = 10.0
_AKBAR_AUDIO_CANDIDATES = [
    os.environ.get("AKBAR_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "akbar.mp3"),
    "/var/lib/posterchanai/assets/akbar.mp3",
]
_AKBAR_DURATION = 5.0
_RETARD_AUDIO_CANDIDATES = [
    os.environ.get("RETARD_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "retard.mp3"),
    "/var/lib/posterchanai/assets/retard.mp3",
]
_RETARD_DURATION = 7.5
_WHOABUDDY_AUDIO_CANDIDATES = [
    os.environ.get("WHOABUDDY_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "whoabuddy.mp3"),
    "/var/lib/posterchanai/assets/whoabuddy.mp3",
]
_WHOABUDDY_DURATION = 6.0
_DIARRHEA_AUDIO_CANDIDATES = [
    os.environ.get("DIARRHEA_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "diarrhea.mp3"),
    "/var/lib/posterchanai/assets/diarrhea.mp3",
]
_DIARRHEA_DURATION = 8.5
# `shrug` — the pointing-up "Whaddya gonna do?" meme still, set to a short "what are you
# gonna do big mouth" audio clip (~1.7s). image_audio_to_video's -shortest ends the clip
# with the audio; _SHRUG_DURATION just caps it a hair above the track length.
_SHRUG_AUDIO_CANDIDATES = [
    os.environ.get("SHRUG_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "shrug.mp3"),
    "/var/lib/posterchanai/assets/shrug.mp3",
]
_SHRUG_DURATION = 2.7
# `soyjack` — the two pointing soyjaks, set to the "Soyjak Crying" meme sound (~8.0s).
# image_audio_to_video's -shortest ends the clip with the audio; the duration just caps it a hair
# above the track so a re-encode can't truncate the tail.
_SOYJACK_AUDIO_CANDIDATES = [
    os.environ.get("SOYJACK_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "soyjack.mp3"),
    "/var/lib/posterchanai/assets/soyjack.mp3",
]
_SOYJACK_DURATION = 8.2
_SETH_AUDIO_CANDIDATES = [
    os.environ.get("SETH_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "seth.mp3"),
    "/var/lib/posterchanai/assets/seth.mp3",
]
_SETH_DURATION = 4.0
_ROBOCOP_AUDIO_CANDIDATES = [
    os.environ.get("ROBOCOP_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "robocop.mp3"),
    "/var/lib/posterchanai/assets/robocop.mp3",
]
_ROBOCOP_DURATION = 13.0
_TITAN_AUDIO_CANDIDATES = [
    os.environ.get("TITAN_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "titan.mp3"),
    "/var/lib/posterchanai/assets/titan.mp3",
]
_TITAN_DURATION = 12.0
_TERMINATOR_AUDIO_CANDIDATES = [
    os.environ.get("TERMINATOR_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "terminator.mp3"),
    "/var/lib/posterchanai/assets/terminator.mp3",
]
_TERMINATOR_DURATION = 15.0
_REZE_AUDIO_CANDIDATES = [
    os.environ.get("REZE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "reze.mp3"),
    "/var/lib/posterchanai/assets/reze.mp3",
]
_REZE_DURATION = 13.0
_REZE_DANCE_CANDIDATES = [
    os.environ.get("REZE_DANCE_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "reze_dance.mov"),
    "/var/lib/posterchanai/assets/reze_dance.mov",
]
_MAKIMA_AUDIO_CANDIDATES = [
    os.environ.get("MAKIMA_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "makima.mp3"),
    "/var/lib/posterchanai/assets/makima.mp3",
]
_MAKIMA_DURATION = 8.0
_MAKIMA_SHOOT_CANDIDATES = [
    os.environ.get("MAKIMA_SHOOT_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "makima_shoot.mov"),
    "/var/lib/posterchanai/assets/makima_shoot.mov",
]
_REBECCA_AUDIO_CANDIDATES = [
    os.environ.get("REBECCA_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "rebecca.mp3"),
    "/var/lib/posterchanai/assets/rebecca.mp3",
]
_REBECCA_DURATION = 8.0
_REBECCA_DANCE_CANDIDATES = [
    os.environ.get("REBECCA_DANCE_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "rebecca_dance.mov"),
    "/var/lib/posterchanai/assets/rebecca_dance.mov",
]
_VIBE_AUDIO_CANDIDATES = [
    os.environ.get("VIBE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "vibe.mp3"),
    "/var/lib/posterchanai/assets/vibe.mp3",
]
_VIBE_DURATION = 8.0
_VIBE_DANCE_CANDIDATES = [
    os.environ.get("VIBE_DANCE_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "vibe_dance.mov"),
    "/var/lib/posterchanai/assets/vibe_dance.mov",
]
_FELIZ_AUDIO_CANDIDATES = [
    os.environ.get("FELIZ_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "feliz.mp3"),
    "/var/lib/posterchanai/assets/feliz.mp3",
]
_FELIZ_DURATION = 9.0
_HORSE_AUDIO_CANDIDATES = [
    os.environ.get("HORSE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "horse.mp3"),
    "/var/lib/posterchanai/assets/horse.mp3",
]
_HORSE_DURATION = 4.0   # clip is 3.03s (0:01-0:04) — ~1s of headroom
_KNIGHTRIDER_AUDIO_CANDIDATES = [
    os.environ.get("KNIGHTRIDER_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "knightrider.mp3"),
    "/var/lib/posterchanai/assets/knightrider.mp3",
]
_KNIGHTRIDER_DURATION = 9.0   # clip is 8.05s (Knight Rider theme, 0:33-0:41) — ~1s of headroom

_SLEEPWELL_AUDIO_CANDIDATES = [
    os.environ.get("SLEEPWELL_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "sleepwell.mp3"),
    "/var/lib/posterchanai/assets/sleepwell.mp3",
]
_SLEEPWELL_DURATION = 24.0   # clip is 23.04s (CG5 - Sleep Well, 2:28-2:51) — ~1s of headroom
_PRAYER_AUDIO_CANDIDATES = [
    os.environ.get("PRAYER_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "prayer.mp3"),
    "/var/lib/posterchanai/assets/prayer.mp3",
]
_PRAYER_DURATION = 11.0
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U00002190-\U000021FF\U0000FE00-\U0000FE0F\U0000200D\U000023E9-\U000023FA]+",
    flags=re.UNICODE,
)
_FELTEDTABLES_AUDIO_CANDIDATES = [
    os.environ.get("FELTEDTABLES_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "feltedtables.mp3"),
    "/var/lib/posterchanai/assets/feltedtables.mp3",
]
_FELTEDTABLES_DURATION = 18.0
_CHEERS_AUDIO_CANDIDATES = [
    os.environ.get("CHEERS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "cheers.mp3"),
    "/var/lib/posterchanai/assets/cheers.mp3",
]
_CHEERS_DURATION = 11.0
_MUNSTERS_AUDIO_CANDIDATES = [
    os.environ.get("MUNSTERS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "munsters.mp3"),
    "/var/lib/posterchanai/assets/munsters.mp3",
]
_MUNSTERS_DURATION = 11.0
_HAPPYDAYS_AUDIO_CANDIDATES = [
    os.environ.get("HAPPYDAYS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "happydays.mp3"),
    "/var/lib/posterchanai/assets/happydays.mp3",
]
_HAPPYDAYS_DURATION = 14.0
_DONTWANTTOWAIT_AUDIO_CANDIDATES = [
    os.environ.get("DONTWANTTOWAIT_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "dontwanttowait.mp3"),
    "/var/lib/posterchanai/assets/dontwanttowait.mp3",
]
_DONTWANTTOWAIT_DURATION = 11.0
_STRANGERTHINGS_AUDIO_CANDIDATES = [
    os.environ.get("STRANGERTHINGS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "strangerthings.mp3"),
    "/var/lib/posterchanai/assets/strangerthings.mp3",
]
_STRANGERTHINGS_DURATION = 14.0
_ADAMSFAMILY_AUDIO_CANDIDATES = [
    os.environ.get("ADAMSFAMILY_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "adamsfamily.mp3"),
    "/var/lib/posterchanai/assets/adamsfamily.mp3",
]
_ADAMSFAMILY_DURATION = 14.0
_XMEN_AUDIO_CANDIDATES = [
    os.environ.get("XMEN_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "xmen.mp3"),
    "/var/lib/posterchanai/assets/xmen.mp3",
]
_XMEN_DURATION = 16.0
_FUTURAMA_AUDIO_CANDIDATES = [
    os.environ.get("FUTURAMA_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "futurama.mp3"),
    "/var/lib/posterchanai/assets/futurama.mp3",
]
_FUTURAMA_DURATION = 12.0
_CHARLIESANGLES_AUDIO_CANDIDATES = [
    os.environ.get("CHARLIESANGLES_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "charliesangles.mp3"),
    "/var/lib/posterchanai/assets/charliesangles.mp3",
]
_CHARLIESANGLES_DURATION = 13.0
_DIFFERENTSTROKE_AUDIO_CANDIDATES = [
    os.environ.get("DIFFERENTSTROKE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "differentstroke.mp3"),
    "/var/lib/posterchanai/assets/differentstroke.mp3",
]
_DIFFERENTSTROKE_DURATION = 9.0
_SEINFELD_AUDIO_CANDIDATES = [
    os.environ.get("SEINFELD_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "seinfeld.mp3"),
    "/var/lib/posterchanai/assets/seinfeld.mp3",
]
_SEINFELD_DURATION = 14.0
_ONEPIECE_AUDIO_CANDIDATES = [
    os.environ.get("ONEPIECE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "onepiece.mp3"),
    "/var/lib/posterchanai/assets/onepiece.mp3",
]
_ONEPIECE_DURATION = 11.0
_OVERTAKEN_AUDIO_CANDIDATES = [
    os.environ.get("OVERTAKEN_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "overtaken.mp3"),
    "/var/lib/posterchanai/assets/overtaken.mp3",
]
_OVERTAKEN_DURATION = 11.0
_SOPRANOS_AUDIO_CANDIDATES = [
    os.environ.get("SOPRANOS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "sopranos.mp3"),
    "/var/lib/posterchanai/assets/sopranos.mp3",
]
_SOPRANOS_DURATION = 14.0
_FREEBIRD_AUDIO_CANDIDATES = [
    os.environ.get("FREEBIRD_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "freebird.mp3"),
    "/var/lib/posterchanai/assets/freebird.mp3",
]
_FREEBIRD_DURATION = 14.0
_KANYE_AUDIO_CANDIDATES = [
    os.environ.get("KANYE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "kanye.mp3"),
    "/var/lib/posterchanai/assets/kanye.mp3",
]
_KANYE_DURATION = 10.0
_DARKNESS_AUDIO_CANDIDATES = [
    os.environ.get("DARKNESS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "darkness.mp3"),
    "/var/lib/posterchanai/assets/darkness.mp3",
]
_DARKNESS_DURATION = 17.0
_BIKE_AUDIO_CANDIDATES = [
    os.environ.get("BIKE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "bike.mp3"),
    "/var/lib/posterchanai/assets/bike.mp3",
]
_BIKE_DURATION = 11.0
_JOBS_AUDIO_CANDIDATES = [
    os.environ.get("JOBS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "jobs.mp3"),
    "/var/lib/posterchanai/assets/jobs.mp3",
]
_JOBS_DURATION = 14.0
_REE_AUDIO_CANDIDATES = [
    os.environ.get("REE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "ree.mp3"),
    "/var/lib/posterchanai/assets/ree.mp3",
]
_REE_DURATION = 7.0
_LIBERAL_AUDIO_CANDIDATES = [
    os.environ.get("LIBERAL_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "liberal.mp3"),
    "/var/lib/posterchanai/assets/liberal.mp3",
]
_LIBERAL_DURATION = 12.0
_MOVING_AUDIO_CANDIDATES = [
    os.environ.get("MOVING_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "moving.mp3"),
    "/var/lib/posterchanai/assets/moving.mp3",
]
_MOVING_DURATION = 12.0
_CONSIDER_PNG_CANDIDATES = [
    os.environ.get("CONSIDER_PNG_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "consider.png"),
    "/var/lib/posterchanai/assets/consider.png",
]
_CLAY_OVERLAY_CANDIDATES = [
    os.environ.get("CLAY_OVERLAY_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "clay.mov"),
    "/var/lib/posterchanai/assets/clay.mov",
]
_CLAY_AUDIO_CANDIDATES = [
    os.environ.get("CLAY_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "clay.mp3"),
    "/var/lib/posterchanai/assets/clay.mp3",
]
_CLAY_DURATION = 2.6
_HARLEM_AUDIO_CANDIDATES = [
    os.environ.get("HARLEM_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "harlem.mp3"),
    "/var/lib/posterchanai/assets/harlem.mp3",
]
_HARLEM_DURATION = 13.0
_CHIMP_GIF_CANDIDATES = [
    os.environ.get("CHIMP_GIF_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "chimp.gif"),
    "/var/lib/posterchanai/assets/chimp.gif",
]
_CHIMP_AUDIO_CANDIDATES = [
    os.environ.get("CHIMP_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "chimp.mp3"),
    "/var/lib/posterchanai/assets/chimp.mp3",
]
_CHIMP_DURATION = 6.0
_WASTELAND_AUDIO_CANDIDATES = [
    os.environ.get("WASTELAND_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "wasteland.mp3"),
    "/var/lib/posterchanai/assets/wasteland.mp3",
]
_WASTELAND_DURATION = 13.0
_MIXALOT_AUDIO_CANDIDATES = [
    os.environ.get("MIXALOT_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "mixalot.mp3"),
    "/var/lib/posterchanai/assets/mixalot.mp3",
]
_MIXALOT_DURATION = 10.0
_THUG_AUDIO_CANDIDATES = [
    os.environ.get("THUG_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "thug.mp3"),
    "/var/lib/posterchanai/assets/thug.mp3",
]
_THUG_DURATION = 18.0
_ANIME_CASCADE_CANDIDATES = [
    os.environ.get("ANIME_CASCADE_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "lbpcascade_animeface.xml"),
    "/var/lib/posterchanai/assets/lbpcascade_animeface.xml",
]
_MOTION_IMAGE_DURATION = 4.0
_CHARACTERS = {
    # The original sticker set (animegirl/pepe/trump/cow/boobs/panties) is GONE — the art was poor and
    # nothing here should outlive being told so. Their assets are deleted too; adding a name back means
    # adding art back.
    "theraped": "theraped.png", "pointup": "theraped.png", "pointing": "theraped.png",
    # `would` — the old man of the pointing-up meme. Same drop-in rule as theraped.
    "would": "would.png", "oldman": "would.png", "jiisan": "would.png",
    "shrug": "shrug.png", "rabbi": "shrug.png", "whaddya": "shrug.png",
    # Reaction overlays (see _add_reaction_overlay): cutouts with no background and no caption —
    # the pose IS the joke, so they stand bottom-centre over the image and say nothing.
    "carl": "carl.png", "brutananadilewski": "carl.png",
    # NOT "pointing" — that alias belongs to theraped above, and a duplicate key here would silently
    # steal it (last one wins in a dict literal).
    "soyjack": "soyjack.png", "soyjak": "soyjack.png", "soy": "soyjack.png", "soyjaks": "soyjack.png",
    # The "looking away" monkey-puppet is a TWO-panel meme (look away -> look at camera). Both panels
    # are cut from one source against a SHARED bbox, so the figure sits identically in each and only
    # the eyes move — a per-panel crop makes the head jump on the cut. `anyways` is the original
    # command name and stays an alias; `lookingaway` is what the meme is actually called.
    "lookingaway": "lookingaway_b.png", "lookaway": "lookingaway_b.png",
    "anyways": "anyways.png", "anyway": "anyways.png", "puppet": "anyways.png", "monkey": "anyways.png",
}
CHARACTER_NAMES = ["theraped", "would", "shrug", "carl", "soyjack", "lookingaway"]
_CHARS_DIR_CANDIDATES = [
    os.path.join(_REPO_ROOT, "assets", "characters"),
    "/var/lib/posterchanai/assets/characters",
]


def _alive_or_still(still_bytes: bytes, stem: str, suffix: str) -> OutputFile:
    """Return a stamp/overlay gag (meme/dildo/poo/bullethole/gay/blacked/kosher/barked/
    consider) as a STILL image. Auto-animating every stamp with parallax was a bad
    default — motion is now opt-in via the `alive` modifier (e.g. `dildo alive`) or the
    other motion modifiers (`dildo zoom`), so these return a flat image unless asked."""
    return {"filename": f"{stem}_{suffix}.jpg", "data": still_bytes, "content_type": "image/jpeg"}


def _load_meme_font(size: int):
    """Load a bold TTF at `size`, falling back to Pillow's default."""
    from PIL import ImageFont
    for path in _MEME_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    # Last resort: scalable default (Pillow >= 10 supports a size arg).
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap_text_to_width(draw, text: str, font, max_width: int) -> List[str]:
    """Greedy word-wrap `text` so each line fits within `max_width` pixels.

    A single word longer than the line is hard-broken character-by-character so
    it can never overflow the image.
    """
    def width_of(s: str) -> int:
        return int(draw.textbbox((0, 0), s, font=font)[2])

    lines: List[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if width_of(candidate) <= max_width or not current:
                # Hard-break an over-long single word.
                if not current and width_of(word) > max_width:
                    piece = ""
                    for ch in word:
                        if width_of(piece + ch) <= max_width or not piece:
                            piece += ch
                        else:
                            lines.append(piece)
                            piece = ch
                    current = piece
                else:
                    current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


def _shade(c, f: float):
    """Lighten (f>1) or darken (f<1) an RGB(A) colour, clamped, alpha forced opaque."""
    return (min(255, int(c[0] * f)), min(255, int(c[1] * f)),
            min(255, int(c[2] * f)), 255)


def _gradient_sphere(base, size: int = 64):
    """A diffuse-lit sphere (light from upper-left) as an RGBA image of `size`px.

    Rendered small once and resized by the caller — used for the glans and balls so
    they read as rounded volumes rather than flat discs.
    """
    import math
    from PIL import Image
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    r = size / 2.0
    lx, ly, lz = -0.45, -0.5, 0.74  # light direction (upper-left, toward viewer)
    for y in range(size):
        for x in range(size):
            dx = (x - r + 0.5) / r
            dy = (y - r + 0.5) / r
            d2 = dx * dx + dy * dy
            if d2 > 1.0:
                continue
            nz = math.sqrt(1.0 - d2)
            shade = 0.42 + 0.9 * max(0.0, dx * lx + dy * ly + nz * lz)
            shade = min(1.4, shade)
            px[x, y] = (min(255, int(base[0] * shade)),
                        min(255, int(base[1] * shade)),
                        min(255, int(base[2] * shade)), 255)
    return img


def _gradient_cylinder(w: int, h: int, base):
    """A vertical cylinder gradient (bright stripe left-of-centre, darkening to the
    edges) as an RGB image — gives the shaft a rounded, lit look."""
    import math
    from PIL import Image
    w = max(int(w), 2)
    h = max(int(h), 2)
    strip = Image.new("RGB", (w, 1))
    px = strip.load()
    hl = 0.38  # highlight position across the width
    for x in range(w):
        t = x / (w - 1)
        shade = 0.5 + 0.78 * max(0.0, math.cos((t - hl) * math.pi))
        shade = min(1.32, shade)
        px[x, 0] = (min(255, int(base[0] * shade)),
                    min(255, int(base[1] * shade)),
                    min(255, int(base[2] * shade)))
    return strip.resize((w, h))


def _scatter_overlay(data: bytes, make_tile, count: int = 0,
                     max_rotation: float = 180.0) -> bytes:
    """Scatter randomly sized/rotated overlay tiles over an image.

    `make_tile(size)` renders one RGBA tile (e.g. `_make_dildo`/`_make_poo`);
    `count` <= 0 auto-scales with the image area; `max_rotation` bounds the random
    spin per tile (±deg). Returns JPEG bytes. Shared by the dildo and poo gags so
    the scatter/flatten/save logic lives in one place.
    """
    import random
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        # Flatten transparency/palette onto white (matches add_meme_text) so the
        # final RGB save never turns transparent areas black.
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        W, H = img.size
        img = img.convert("RGBA")  # composite layer
        if count <= 0:
            count = max(14, min(60, (W * H) // 38000))
        base = min(W, H)
        lo, hi = max(int(base * 0.12), 12), max(int(base * 0.28), 24)
        for _ in range(count):
            size = random.randint(lo, hi)
            tile = make_tile(size)
            tile = tile.rotate(random.uniform(-max_rotation, max_rotation),
                               expand=True, resample=Image.BICUBIC)
            # Allow partial overhang off every edge so the scatter reaches the borders.
            x = random.randint(-tile.width // 3, max(W - tile.width * 2 // 3, 1))
            y = random.randint(-tile.height // 3, max(H - tile.height * 2 // 3, 1))
            img.alpha_composite(tile, (x, y))

        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def _scatter_frames(data: bytes, make_tile_seeded, count: int = 0,
                    max_rotation: float = 180.0, n_frames: int = _SCATTER_ANIM_FRAMES,
                    ramp_frac: float = 0.55, start_grow: float = 0.12):
    """Build the animation frames for a scatter gag. `make_tile_seeded(size, seed,
    grow)` must render one RGBA tile deterministically for a given seed (stable shape)
    with `grow` in [0,1] scaling its drip/streak extent."""
    import random
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        # Bound the working resolution so re-rendering N frames stays affordable.
        if max(img.size) > _SCATTER_ANIM_MAXDIM:
            img.thumbnail((_SCATTER_ANIM_MAXDIM, _SCATTER_ANIM_MAXDIM), Image.LANCZOS)
        base_rgb = img.convert("RGB")

    W, H = base_rgb.size
    if count <= 0:
        count = max(14, min(60, (W * H) // 38000))
    count = min(count, _SCATTER_ANIM_MAXTILES)
    base = min(W, H)
    lo, hi = max(int(base * 0.12), 12), max(int(base * 0.28), 24)

    # Fix the layout once. Position is picked from a full-grow render's rotated size;
    # the tile canvas is grow-independent, so every frame's tile lands identically.
    layout = []
    for _ in range(count):
        size = random.randint(lo, hi)
        angle = random.uniform(-max_rotation, max_rotation)
        seed = random.randrange(1 << 30)
        sample = make_tile_seeded(size, seed, 1.0).rotate(
            angle, expand=True, resample=Image.BICUBIC)
        x = random.randint(-sample.width // 3, max(W - sample.width * 2 // 3, 1))
        y = random.randint(-sample.height // 3, max(H - sample.height * 2 // 3, 1))
        layout.append((size, angle, seed, x, y))

    frames = []
    for fi in range(n_frames):
        # grow ramps start_grow→1 (smoothstep) over the first `ramp_frac`, then holds.
        p = min(fi / max(n_frames * ramp_frac, 1.0), 1.0)
        grow = start_grow + (1.0 - start_grow) * (p * p * (3 - 2 * p))
        layer = base_rgb.convert("RGBA")
        for size, angle, seed, x, y in layout:
            tile = make_tile_seeded(size, seed, grow).rotate(
                angle, expand=True, resample=Image.BICUBIC)
            layer.alpha_composite(tile, (x, y))
        frames.append(layer.convert("RGB"))
    return frames


def _effects_animate() -> bool:
    """Whether the animatable effects (fire/blood/cum) render as a moving MP4 rather
    than a flat still. On by default; set EFFECTS_ANIMATE=0 to fall back to stills
    (no redeploy needed — flip the env on the service and restart)."""
    return os.environ.get("EFFECTS_ANIMATE", "1").strip().lower() not in ("0", "false", "no")


def _load_blacked_font(size: int):
    """Load a heavy, wide grotesque for the BLACKED wordmark (never Impact)."""
    from PIL import ImageFont
    for path in _BLACKED_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return _load_meme_font(size)


def _tracked_width(draw, text: str, font, tracking: float) -> float:
    """Total pixel width of `text` rendered with `tracking` px between glyphs."""
    if not text:
        return 0.0
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * (len(text) - 1)


def _draw_tracked(draw, x: float, y: float, text: str, font, tracking: float, **kw):
    """Draw `text` glyph-by-glyph with `tracking` px added between letters.

    Pillow has no letter-spacing, so we advance manually by each glyph's own
    width + `tracking`. `kw` is passed straight to ``draw.text`` (fill/stroke)."""
    for ch in text:
        draw.text((x, y), ch, font=font, **kw)
        x += draw.textlength(ch, font=font) + tracking
    return x


def _pad_audio_to_duration(audio_path: str, seconds: float, start: float = 0.0) -> str:
    """Return a temp mp3 of `audio_path` cut to `seconds`, padded with trailing silence if it's
    shorter, so a video muxed with ffmpeg's -shortest lasts the full duration instead of ending with
    the short clip. `start` drops that many seconds off the FRONT first — for clips that open with
    dead air before the sound actually hits. Caller deletes the temp file; falls back to the original
    path on any failure."""
    from app.services.media_service import resolve_ffmpeg, ffmpeg_available
    if not ffmpeg_available():
        return audio_path
    fd, out_path = tempfile.mkstemp(prefix="fahh_pad_", suffix=".mp3")
    os.close(fd)
    af = "apad" if start <= 0 else f"atrim=start={start:.3f},asetpts=PTS-STARTPTS,apad"
    cmd = [resolve_ffmpeg(), "-i", audio_path, "-af", af, "-t", f"{seconds:.3f}",
           "-c:a", "libmp3lame", "-y", out_path]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120, text=True)
        if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path
        logger.warning(f"fahh audio pad failed: {(r.stderr or '')[-200:]}")
    except Exception as e:
        logger.warning(f"fahh audio pad error: {e}")
    if os.path.exists(out_path):
        os.unlink(out_path)
    return audio_path


def _apply_motion(outputs: List[OutputFile], suffix: str, image_fn, video_fn) -> List[OutputFile]:
    """Replace each effect output with a motion version. Images turn into a short
    motion MP4 (via `image_fn`); existing videos are re-rendered with the same
    motion keeping their audio (via `video_fn`). Original kept on failure."""
    result_files: List[OutputFile] = []
    for out in outputs or []:
        ct = (out.get("content_type") or "").lower()
        fn = out.get("filename") or "image"
        stem = Path(fn).stem or "image"
        try:
            if ct.startswith("image/"):
                video = image_fn(out["data"], fn, duration=_MOTION_IMAGE_DURATION)
            elif ct.startswith("video/"):
                video = video_fn(out["data"], fn)
            else:
                result_files.append(out)
                continue
            result_files.append({
                "filename": f"{stem}_{suffix}.mp4",
                "data": video,
                "content_type": "video/mp4",
            })
        except Exception as e:
            logger.error(f"{suffix} failed for {fn}: {e}", exc_info=True)
            result_files.append(out)
    return result_files


def _meme_font_path() -> str:
    """First existing meme font file ("" if none → ffmpeg uses its default)."""
    for p in _MEME_FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""
