#!/usr/bin/env python3
"""
Posterchan Bot Controller
Manages bots defined in bots_config.py using a single service
"""

import os
import sys
import json
import subprocess
import signal
import time
import argparse
import logging
from pathlib import Path

# Configure logging for all modules
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Get script directory
SCRIPT_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(SCRIPT_DIR))

from bots_config import (
    IMAGE_BOTS, IMAGE_BOT_DEFAULTS, TEXT_BOTS,
    AI_API_URL, AI_API_KEY,
    SQL_USER, SQL_PASS, SQL_HOST,
    SEARXNG_URL, TIMEZONE, POSTERCHANAI_API_KEY,
)
from core import Colors


# Global process tracking
running_processes = {}  # For text bots (long-running)
image_bot_schedules = {}  # For image bots (scheduled runs)
bot_restart_counts = {}  # Track restart counts for rate limiting
shutdown_requested = False

# Restart rate limiting
MAX_RESTARTS_PER_HOUR = 10
RESTART_COUNT_RESET_TIME = 3600  # 1 hour


def get_hostname():
    """Get short hostname"""
    import socket
    return socket.gethostname().split('.')[0]


def build_env(bot_name, bot_config, is_image_bot=False):
    """Build environment variables for a bot"""
    env = os.environ.copy()

    # AI settings
    env['OPENAI_ENDPOINT'] = AI_API_URL
    env['OPENAI_API_KEY'] = AI_API_KEY
    env['TIMEZONE'] = TIMEZONE
    env['SEARXNG_URL'] = SEARXNG_URL

    if is_image_bot:
        # Image bot settings
        defaults = IMAGE_BOT_DEFAULTS
        env['MISSKEY_SERVER'] = defaults.get('server', '')
        env['MISSKEY_USERNAME'] = defaults.get('username', '')
        env['MISSKEY_ACCESS_TOKEN'] = defaults.get('access_token', '')
        env['IMAGE_POSTER_PROMPT'] = bot_config.get('prompt', '')
        env['IMAGE_POSTER_TEXT'] = defaults.get('text', '')
        # Image backend (USE_POSTERCHANAI, endpoints, creds) is read directly from
        # bots_config by config.py; only the API key is bridged via env.
        if POSTERCHANAI_API_KEY:
            env['POSTERCHANAI_API_KEY'] = POSTERCHANAI_API_KEY
        if defaults.get('random_scenes'):
            env['IMAGE_POSTER_RANDOM_SCENES'] = 'true'
    else:
        # Text bot settings
        platform = bot_config.get('platform', 'misskey')

        if platform == 'misskey':
            if bot_config.get('server'):
                env['MISSKEY_SERVER'] = bot_config['server']
            if bot_config.get('username'):
                env['MISSKEY_USERNAME'] = bot_config['username'].lstrip('@')
            if bot_config.get('access_token'):
                env['MISSKEY_ACCESS_TOKEN'] = bot_config['access_token']

        elif platform == 'pleroma':
            if bot_config.get('server'):
                env['PLEROMA_ENDPOINT'] = bot_config['server']
            if bot_config.get('username'):
                env['PLEROMA_USERNAME'] = bot_config['username'].lstrip('@')
            if bot_config.get('access_token'):
                env['PLEROMA_ACCESS_TOKEN'] = bot_config['access_token']
            if bot_config.get('pleroma_admin_token'):
                env['PLEROMA_ADMIN_TOKEN'] = bot_config['pleroma_admin_token']

        elif platform == 'matrix':
            if bot_config.get('matrix_server'):
                env['MATRIX_SERVER'] = bot_config['matrix_server']
            if bot_config.get('matrix_user_id'):
                env['MATRIX_USER_ID'] = bot_config['matrix_user_id']
            if bot_config.get('matrix_access_token'):
                env['MATRIX_ACCESS_TOKEN'] = bot_config['matrix_access_token']
            if bot_config.get('matrix_room_id'):
                env['MATRIX_ROOM_ID'] = bot_config['matrix_room_id']
            if bot_config.get('matrix_admins'):
                env['MATRIX_ADMINS'] = bot_config['matrix_admins']
            if bot_config.get('shamebot_rooms'):
                env['SHAMEBOT_ROOMS'] = ','.join(bot_config['shamebot_rooms']) if isinstance(bot_config['shamebot_rooms'], list) else str(bot_config['shamebot_rooms'])

        # Matrix settings (can be used alongside other platforms)
        if bot_config.get('matrix_server'):
            env['MATRIX_SERVER'] = bot_config['matrix_server']
        if bot_config.get('matrix_user_id'):
            env['MATRIX_USER_ID'] = bot_config['matrix_user_id']
        if bot_config.get('matrix_access_token'):
            env['MATRIX_ACCESS_TOKEN'] = bot_config['matrix_access_token']
        if bot_config.get('matrix_room_id'):
            env['MATRIX_ROOM_ID'] = bot_config['matrix_room_id']
        if bot_config.get('matrix_admins'):
            env['MATRIX_ADMINS'] = bot_config['matrix_admins']
        if bot_config.get('matrix_verify_ssl') is not None:
            env['MATRIX_VERIFY_SSL'] = str(bot_config['matrix_verify_ssl']).lower()
        if bot_config.get('shamebot_rooms'):
            env['SHAMEBOT_ROOMS'] = ','.join(bot_config['shamebot_rooms']) if isinstance(bot_config['shamebot_rooms'], list) else str(bot_config['shamebot_rooms'])

        # Nitter RSS → Matrix feeds (for the --nitter bot): array of {room, rss}
        if bot_config.get('nitter_feeds'):
            env['NITTER_FEEDS'] = json.dumps(bot_config['nitter_feeds'])
        if bot_config.get('nitter_poll_seconds'):
            env['NITTER_POLL_SECONDS'] = str(bot_config['nitter_poll_seconds'])

        # Sticker macros (Matrix): enable "!name" posting of media auto-discovered from
        # the stickers/ folder. Config is just the on/off flag — no per-sticker listing.
        if bot_config.get('stickers_enabled'):
            env['STICKERS_ENABLED'] = 'true'

        # Extra hostnames trusted for media downloads (in addition to the bot's own
        # instance), so the SSRF guard doesn't block files hosted on another LAN
        # instance. Accepts a list or a comma-separated string.
        tmh = bot_config.get('trusted_media_hosts')
        if tmh:
            env['TRUSTED_MEDIA_HOSTS'] = ','.join(tmh) if isinstance(tmh, (list, tuple)) else str(tmh)

        # Common settings
        if bot_config.get('prompt'):
            env['PROMPT'] = bot_config['prompt']

        # Posterchanai image-backend API key: per-bot override, else the global.
        # All other backend settings are read directly from bots_config by config.py.
        if bot_config.get('posterchanai_api_key'):
            env['POSTERCHANAI_API_KEY'] = bot_config['posterchanai_api_key']
        elif POSTERCHANAI_API_KEY:
            env['POSTERCHANAI_API_KEY'] = POSTERCHANAI_API_KEY

        # Database settings
        if bot_config.get('sql_database'):
            env['SQL_USER'] = SQL_USER
            env['SQL_PASS'] = SQL_PASS
            env['SQL_HOST'] = SQL_HOST
            env['SQL_DATABASE'] = bot_config['sql_database']

        # Blockbot settings
        if bot_config.get('block_image'):
            env['BLOCK_IMAGE'] = bot_config['block_image']
        if bot_config.get('block_prompt'):
            env['BLOCK_PROMPT'] = bot_config['block_prompt']

        # Welcome settings
        if bot_config.get('welcome_image'):
            env['WELCOME_IMAGE'] = bot_config['welcome_image']
        if bot_config.get('welcome_message'):
            env['WELCOME_MESSAGE'] = bot_config['welcome_message']
        if bot_config.get('welcome_lookback_minutes'):
            env['WELCOME_LOOKBACK_MINUTES'] = str(bot_config['welcome_lookback_minutes'])
        if bot_config.get('welcome_prompt'):
            env['WELCOME_PROMPT'] = bot_config['welcome_prompt']

        # Report settings
        if bot_config.get('report_image'):
            env['REPORT_IMAGE'] = bot_config['report_image']
        if bot_config.get('report_prompt'):
            env['REPORT_PROMPT'] = bot_config['report_prompt']

        # Unfollow settings
        if bot_config.get('unfollow_image'):
            env['UNFOLLOW_IMAGE'] = bot_config['unfollow_image']
        if bot_config.get('unfollow_silent_mode') is not None:
            env['UNFOLLOW_SILENT_MODE'] = str(bot_config['unfollow_silent_mode']).lower()

        # TTS settings (for /narrate command and auto_narrate)
        if bot_config.get('tts_voice'):
            env['TTS_VOICE'] = bot_config['tts_voice']
        if bot_config.get('tts_rate'):
            env['TTS_RATE'] = bot_config['tts_rate']
        if bot_config.get('tts_pitch'):
            env['TTS_PITCH'] = bot_config['tts_pitch']
        if bot_config.get('auto_narrate'):
            env['AUTO_NARRATE'] = 'true'
        if bot_config.get('video_encoder'):
            env['VIDEO_ENCODER'] = bot_config['video_encoder']
        
        # AI temperature setting (per-bot override)
        if bot_config.get('temperature') is not None:
            env['AI_TEMPERATURE'] = str(bot_config['temperature'])

    return env


def get_bots_for_host(host=None):
    """Get list of bots configured for this host"""
    if host is None:
        host = get_hostname()

    bots = []

    # Image bots
    for name, config in IMAGE_BOTS.items():
        if config.get('host') == host:
            bots.append(('image', name, config))

    # Text bots
    for name, config in TEXT_BOTS.items():
        if config.get('host') == host:
            bots.append(('text', name, config))

    return bots


def list_bots(args):
    """List all bots"""
    host = args.host or get_hostname()
    print(f"\n{Colors.BOLD}Bots for host: {host}{Colors.END}\n")

    bots = get_bots_for_host(host)

    if not bots:
        print(f"  {Colors.DIM}No bots configured for this host{Colors.END}")
        return

    # Image bots
    image_bots = [(n, c) for t, n, c in bots if t == 'image']
    if image_bots:
        schedule_hours = [0, 6, 12, 18]
        print(f"{Colors.CYAN}Image Bots ({len(image_bots)}):{Colors.END} {Colors.DIM}(daily at {schedule_hours}:00){Colors.END}")
        for name, config in image_bots:
            prompt = config['prompt'][:40] + '...' if len(config['prompt']) > 40 else config['prompt']
            print(f"  {Colors.GREEN}{name}{Colors.END}: {Colors.DIM}{prompt}{Colors.END}")
        print()

    # Text bots
    text_bots = [(n, c) for t, n, c in bots if t == 'text']
    if text_bots:
        print(f"{Colors.CYAN}Text Bots ({len(text_bots)}):{Colors.END}")
        for name, config in text_bots:
            modes = ' '.join(config.get('modes', []))
            platform = config.get('platform', 'misskey')
            print(f"  {Colors.GREEN}{name}{Colors.END}: {Colors.DIM}{platform} {modes}{Colors.END}")
        print()


def start_bot_process(bot_type, bot_name, bot_config):
    """Start a single bot as a subprocess, return the process"""
    is_image = bot_type == 'image'
    env = build_env(bot_name, bot_config, is_image_bot=is_image)

    # Verify PROMPT is set for text bots (for debugging)
    # Skip check for bots that don't use AI prompts
    if not is_image:
        modes = bot_config.get('modes', [])
        is_welcome_bot = '--welcome' in modes
        
        # Modes that don't need prompts (they don't use AI for responses)
        no_prompt_modes = ['--hashtagbot', '--blockbot', '--report', '--unfollowbot', '--nitter']
        # Modes that DO need prompts (AI-powered)
        ai_modes = ['--misskey', '--pleroma', '--matrix']
        
        # Check if bot has any AI modes that require prompts
        has_ai_mode = any(mode in ai_modes for mode in modes)
        # Check if bot ONLY has non-AI modes
        only_non_ai = all(mode in no_prompt_modes for mode in modes) if modes else False
        
        if is_welcome_bot:
            # Welcome bot uses WELCOME_PROMPT, not PROMPT
            welcome_prompt = env.get('WELCOME_PROMPT', '')
            if welcome_prompt:
                logger.info(f"Starting {bot_name}: WELCOME_PROMPT set ({len(welcome_prompt)} chars)")
            else:
                logger.info(f"Starting {bot_name}: Using default WELCOME_PROMPT from config.py")
        elif only_non_ai:
            # Bots that ONLY have non-AI modes don't need prompts
            logger.info(f"Starting {bot_name}: No prompt needed (non-AI bot)")
        elif has_ai_mode:
            # Bots with AI modes need PROMPT
            prompt = env.get('PROMPT', '')
            if prompt:
                logger.info(f"Starting {bot_name}: PROMPT set ({len(prompt)} chars)")
            else:
                logger.warning(f"Starting {bot_name}: PROMPT NOT SET! Bot will not have personality.")
                logger.warning(f"  Check that bots_config.py['{bot_name}']['prompt'] is set")
        else:
            # Unknown mode combination - check for prompt to be safe
            prompt = env.get('PROMPT', '')
            if prompt:
                logger.info(f"Starting {bot_name}: PROMPT set ({len(prompt)} chars)")
            else:
                logger.warning(f"Starting {bot_name}: PROMPT NOT SET! Bot will not have personality.")
                logger.warning(f"  Check that bots_config.py['{bot_name}']['prompt'] is set")

    # Build command
    venv_python = SCRIPT_DIR / 'venv' / 'bin' / 'python'
    if not venv_python.exists():
        venv_python = sys.executable

    if is_image:
        cmd = [str(venv_python), str(SCRIPT_DIR / 'main.py'), '--image']
    else:
        modes = bot_config.get('modes', ['--misskey'])
        cmd = [str(venv_python), str(SCRIPT_DIR / 'main.py')] + modes

    # Forward stdout/stderr to parent so logs appear in journald
    proc = subprocess.Popen(cmd, env=env, cwd=str(SCRIPT_DIR),
                           stdout=None, stderr=None)  # Inherit parent's stdout/stderr
    return proc


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    global shutdown_requested
    shutdown_requested = True
    print(f"\n{Colors.YELLOW}Shutdown requested, stopping all bots...{Colors.END}")

    # Stop text bots
    for name, proc in running_processes.items():
        if proc.poll() is None:
            print(f"  Stopping {name}...")
            proc.terminate()

    # Stop image bots
    for name, sched in image_bot_schedules.items():
        if sched.get('process') and sched['process'].poll() is None:
            print(f"  Stopping {name}...")
            sched['process'].terminate()

    # Wait for processes to terminate
    for name, proc in running_processes.items():
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            print(f"  Force killing {name}...")
            proc.kill()

    for name, sched in image_bot_schedules.items():
        if sched.get('process'):
            try:
                sched['process'].wait(timeout=5)
            except subprocess.TimeoutExpired:
                print(f"  Force killing {name}...")
                sched['process'].kill()

    sys.exit(0)


def run_all_bots(args):
    """Run all bots for this host in a single process manager"""
    global running_processes, image_bot_schedules, shutdown_requested

    host = args.host or get_hostname()
    bots = get_bots_for_host(host)

    if not bots:
        print(f"{Colors.YELLOW}No bots configured for host: {host}{Colors.END}")
        return

    # Set up signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Separate text and image bots
    text_bots = [(t, n, c) for t, n, c in bots if t == 'text']
    image_bots = [(t, n, c) for t, n, c in bots if t == 'image']

    print(f"\n{Colors.BOLD}Starting bots for host: {host}{Colors.END}")
    print(f"  Text bots: {len(text_bots)} (continuous)")
    print(f"  Image bots: {len(image_bots)} (scheduled)\n")

    # Start text bots (run continuously)
    for bot_type, name, config in text_bots:
        proc = start_bot_process(bot_type, name, config)
        running_processes[name] = proc
        print(f"  {Colors.GREEN}+{Colors.END} Started {name} (PID: {proc.pid})")
        time.sleep(0.5)

    # Initialize image bot schedules at specific hours (00:00, 06:00, 18:00)
    schedule_hours = [0, 6, 12, 18]  # Hours of day to run image bots

    def get_next_scheduled_time(schedule_hours):
        """Get the next scheduled time based on hours list"""
        now = time.localtime()
        current_hour = now.tm_hour
        current_min = now.tm_min

        # Find next scheduled hour
        for h in sorted(schedule_hours):
            if h > current_hour or (h == current_hour and current_min < 5):
                # Next run is today at hour h
                next_time = time.mktime(time.struct_time((
                    now.tm_year, now.tm_mon, now.tm_mday,
                    h, 0, 0, now.tm_wday, now.tm_yday, now.tm_isdst
                )))
                return next_time

        # All hours passed today, next run is tomorrow at first hour
        tomorrow = time.time() + 86400  # Add 24 hours
        tom = time.localtime(tomorrow)
        next_time = time.mktime(time.struct_time((
            tom.tm_year, tom.tm_mon, tom.tm_mday,
            min(schedule_hours), 0, 0, tom.tm_wday, tom.tm_yday, tom.tm_isdst
        )))
        return next_time

    next_run = get_next_scheduled_time(schedule_hours)
    next_run_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(next_run))

    # Stagger image bots by 5 minutes each to avoid queue flooding
    STAGGER_DELAY = 300  # 5 minutes between each bot

    for i, (bot_type, name, config) in enumerate(image_bots):
        staggered_time = next_run + (i * STAGGER_DELAY)
        image_bot_schedules[name] = {
            'next_run': staggered_time,
            'schedule_hours': schedule_hours,
            'config': config,
            'process': None,
            'stagger_offset': i * STAGGER_DELAY  # Remember offset for future scheduling
        }

    total_stagger_mins = (len(image_bots) - 1) * STAGGER_DELAY // 60
    print(f"  {Colors.CYAN}~{Colors.END} {len(image_bots)} image bots scheduled at {schedule_hours} hours")
    print(f"  {Colors.CYAN}~{Colors.END} First bot: {next_run_str}, staggered over {total_stagger_mins} minutes")

    print(f"\n{Colors.GREEN}All bots initialized. Press Ctrl+C to stop.{Colors.END}\n")

    # Monitor loop
    while not shutdown_requested:
        now = time.time()

        # Check text bots - restart if crashed (with rate limiting)
        for bot_type, name, config in text_bots:
            proc = running_processes.get(name)
            if proc and proc.poll() is not None:
                # Initialize restart tracking if needed
                if name not in bot_restart_counts:
                    bot_restart_counts[name] = {'count': 0, 'first_restart': now}

                restart_info = bot_restart_counts[name]

                # Reset count if an hour has passed
                if now - restart_info['first_restart'] > RESTART_COUNT_RESET_TIME:
                    restart_info['count'] = 0
                    restart_info['first_restart'] = now

                # Check rate limit
                if restart_info['count'] >= MAX_RESTARTS_PER_HOUR:
                    print(f"{Colors.RED}Bot {name} exceeded restart limit ({MAX_RESTARTS_PER_HOUR}/hour), skipping...{Colors.END}")
                    running_processes[name] = None  # Mark as not running
                    continue

                print(f"{Colors.YELLOW}Bot {name} died (exit code: {proc.returncode}), restarting...{Colors.END}")
                time.sleep(2)
                new_proc = start_bot_process(bot_type, name, config)
                running_processes[name] = new_proc
                restart_info['count'] += 1
                print(f"  {Colors.GREEN}+{Colors.END} Restarted {name} (PID: {new_proc.pid}) [{restart_info['count']}/{MAX_RESTARTS_PER_HOUR}]")

        # Check image bots - run on schedule with stagger
        # Only start one image bot per STAGGER_DELAY seconds to avoid queue flooding
        for bot_type, name, config in image_bots:
            sched = image_bot_schedules[name]

            # Check if currently running process finished
            if sched['process'] and sched['process'].poll() is not None:
                exit_code = sched['process'].returncode
                if exit_code == 0:
                    print(f"  {Colors.GREEN}+{Colors.END} {name} completed successfully")
                else:
                    print(f"  {Colors.YELLOW}!{Colors.END} {name} exited with code {exit_code}")
                sched['process'] = None

            # Start if due and not currently running
            # Also check that no other image bot was started recently (stagger enforcement)
            if now >= sched['next_run'] and sched['process'] is None:
                # Check if we need to wait due to stagger
                last_start = image_bot_schedules.get('_last_start_time', 0)
                if now - last_start < STAGGER_DELAY:
                    continue  # Wait for stagger delay before starting next bot

                proc = start_bot_process(bot_type, name, config)
                sched['process'] = proc
                image_bot_schedules['_last_start_time'] = now  # Track when we last started a bot
                # Schedule next run at next scheduled hour + stagger offset
                base_next_run = get_next_scheduled_time(sched['schedule_hours'])
                sched['next_run'] = base_next_run + sched.get('stagger_offset', 0)
                next_run_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(sched['next_run']))
                print(f"  {Colors.CYAN}>{Colors.END} Running {name} (next: {next_run_str})")

        time.sleep(5)  # Check every 5 seconds


def generate_service(args):
    """Generate a single systemd service file"""
    host = args.host or get_hostname()
    bots = get_bots_for_host(host)

    if not bots:
        print(f"{Colors.YELLOW}No bots configured for host: {host}{Colors.END}")
        return None

    user = os.environ.get('USER', 'root')
    venv_python = SCRIPT_DIR / 'venv' / 'bin' / 'python'

    service_content = f"""[Unit]
Description=Posterchan Bot Manager
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={SCRIPT_DIR}
ExecStart={venv_python} {SCRIPT_DIR}/botctl.py run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""

    service_path = SCRIPT_DIR / 'posterchan.service'
    with open(service_path, 'w') as f:
        f.write(service_content)

    print(f"\n{Colors.GREEN}Generated: {service_path}{Colors.END}")
    print(f"\nThis single service will manage {len(bots)} bots:")
    for bot_type, name, config in bots:
        print(f"  - {name} ({bot_type})")

    print(f"\nTo install:")
    print(f"  {Colors.CYAN}sudo cp {service_path} /etc/systemd/system/{Colors.END}")
    print(f"  {Colors.CYAN}sudo systemctl daemon-reload{Colors.END}")
    print(f"  {Colors.CYAN}sudo systemctl enable posterchan{Colors.END}")
    print(f"  {Colors.CYAN}sudo systemctl start posterchan{Colors.END}")

    return service_path


def deploy_service(args):
    """Generate, install, and start the single posterchan service"""
    host = args.host or get_hostname()
    bots = get_bots_for_host(host)

    if not bots:
        print(f"{Colors.YELLOW}No bots configured for host: {host}{Colors.END}")
        return

    print(f"\n{Colors.BOLD}Deploying posterchan service ({len(bots)} bots){Colors.END}\n")

    # Step 1: Generate service
    print(f"{Colors.CYAN}[1/4] Generating service...{Colors.END}")
    service_path = generate_service(args)
    if not service_path:
        return

    # Step 2: Copy to systemd
    print(f"\n{Colors.CYAN}[2/4] Installing service...{Colors.END}")
    result = subprocess.run(
        ['sudo', 'cp', str(service_path), '/etc/systemd/system/posterchan.service'],
        capture_output=True
    )
    if result.returncode == 0:
        print(f"  {Colors.GREEN}+{Colors.END} posterchan.service installed")
    else:
        print(f"  {Colors.RED}x{Colors.END} Failed to install service")
        return

    # Step 3: Reload systemd
    print(f"\n{Colors.CYAN}[3/4] Reloading systemd...{Colors.END}")
    subprocess.run(['sudo', 'systemctl', 'daemon-reload'], capture_output=True)
    print(f"  {Colors.GREEN}+{Colors.END} daemon-reload")

    # Step 4: Enable and restart service
    print(f"\n{Colors.CYAN}[4/4] Starting service...{Colors.END}")
    subprocess.run(['sudo', 'systemctl', 'enable', 'posterchan'], capture_output=True)
    result = subprocess.run(['sudo', 'systemctl', 'restart', 'posterchan'], capture_output=True)
    if result.returncode == 0:
        print(f"  {Colors.GREEN}+{Colors.END} posterchan started")
    else:
        print(f"  {Colors.RED}x{Colors.END} Failed to start service")
        return

    print(f"\n{Colors.GREEN}{Colors.BOLD}Deploy complete!{Colors.END}")
    print(f"\nView logs: {Colors.CYAN}sudo journalctl -u posterchan -f{Colors.END}")


def stop_service(args):
    """Stop the posterchan service"""
    print(f"\n{Colors.BOLD}Stopping posterchan service...{Colors.END}")
    result = subprocess.run(['sudo', 'systemctl', 'stop', 'posterchan'], capture_output=True)
    if result.returncode == 0:
        print(f"  {Colors.GREEN}+{Colors.END} Stopped posterchan")
    else:
        print(f"  {Colors.YELLOW}~{Colors.END} posterchan (not running)")


def restart_service(args):
    """Restart the posterchan service"""
    print(f"\n{Colors.BOLD}Restarting posterchan service...{Colors.END}")
    result = subprocess.run(['sudo', 'systemctl', 'restart', 'posterchan'], capture_output=True)
    if result.returncode == 0:
        print(f"  {Colors.GREEN}+{Colors.END} Restarted posterchan")
    else:
        print(f"  {Colors.RED}x{Colors.END} Failed to restart (is it installed?)")


def status_service(args):
    """Show status of the posterchan service"""
    host = args.host or get_hostname()
    bots = get_bots_for_host(host)

    print(f"\n{Colors.BOLD}Posterchan Status (host: {host}){Colors.END}\n")

    # Check systemd service status
    result = subprocess.run(
        ['systemctl', 'is-active', 'posterchan'],
        capture_output=True, text=True
    )
    status = result.stdout.strip()

    if status == 'active':
        print(f"  {Colors.GREEN}*{Colors.END} Service: {Colors.GREEN}running{Colors.END}")
    elif status == 'inactive':
        print(f"  {Colors.DIM}o{Colors.END} Service: {Colors.DIM}stopped{Colors.END}")
    else:
        print(f"  {Colors.RED}*{Colors.END} Service: {Colors.RED}{status}{Colors.END}")

    # Separate text and image bots
    text_bots = [(n, c) for t, n, c in bots if t == 'text']
    image_bots = [(n, c) for t, n, c in bots if t == 'image']

    if text_bots:
        print(f"\n  {Colors.CYAN}Text Bots ({len(text_bots)}):{Colors.END} continuous")
        for name, config in text_bots:
            modes = ' '.join(config.get('modes', []))
            print(f"    - {name}: {Colors.DIM}{modes}{Colors.END}")

    if image_bots:
        schedule_hours = [0, 6, 12, 18]
        print(f"\n  {Colors.CYAN}Image Bots ({len(image_bots)}):{Colors.END} scheduled at {schedule_hours}:00")
        for name, config in image_bots[:10]:  # Show first 10
            print(f"    - {name}")
        if len(image_bots) > 10:
            print(f"    {Colors.DIM}... and {len(image_bots) - 10} more{Colors.END}")


def main():
    parser = argparse.ArgumentParser(
        description='Posterchan Bot Controller',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  botctl.py list                    List all bots for this host
  botctl.py status                  Show service status
  botctl.py deploy                  Generate + install + start service
  botctl.py restart                 Restart the service
  botctl.py stop                    Stop the service
  botctl.py run                     Run all bots (used by systemd)
  botctl.py generate                Generate systemd service file only
"""
    )

    parser.add_argument('--host', help='Filter by host (default: current hostname)')

    subparsers = parser.add_subparsers(dest='command', help='Command')

    # List command
    subparsers.add_parser('list', help='List configured bots')

    # Status command
    subparsers.add_parser('status', help='Show status of service')

    # Deploy command
    subparsers.add_parser('deploy', help='Generate, install, and start service')

    # Stop command
    subparsers.add_parser('stop', help='Stop the service')

    # Restart command
    subparsers.add_parser('restart', help='Restart the service')

    # Run command (used by systemd)
    subparsers.add_parser('run', help='Run all bots (used by systemd)')

    # Generate command
    subparsers.add_parser('generate', help='Generate systemd service file only')

    args = parser.parse_args()

    if args.command == 'list':
        list_bots(args)
    elif args.command == 'status':
        status_service(args)
    elif args.command == 'deploy':
        deploy_service(args)
    elif args.command == 'stop':
        stop_service(args)
    elif args.command == 'restart':
        restart_service(args)
    elif args.command == 'run':
        run_all_bots(args)
    elif args.command == 'generate':
        generate_service(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
