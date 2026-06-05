#!/usr/bin/env python3
"""
Posterchan CLI Installer
A sexy interactive installer for setting up Posterchan bots
"""

import os
import sys
import json
import getpass
import subprocess
import shutil
import time

# ANSI color codes
class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'

def clear_screen():
    os.system('clear' if os.name != 'nt' else 'cls')

def update_from_git(install_path):
    """Pull latest code from git"""
    print_section("Updating from Git")

    # Check if it's a git repo
    git_dir = os.path.join(install_path, '.git')
    if not os.path.exists(git_dir):
        print_warning("Not a git repository. Skipping git update.")
        return False

    try:
        print_info("Pulling latest changes...")
        result = subprocess.run(
            ['git', 'pull'],
            cwd=install_path,
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            if "Already up to date" in result.stdout:
                print_success("Already up to date")
            else:
                print_success("Updated successfully")
                print(f"{Colors.DIM}{result.stdout}{Colors.END}")
            return True
        else:
            print_error(f"Git pull failed: {result.stderr}")
            return False
    except FileNotFoundError:
        print_warning("Git not installed. Skipping update.")
        return False
    except Exception as e:
        print_error(f"Failed to update: {e}")
        return False

def setup_venv(install_path):
    """Create virtual environment and install dependencies"""
    venv_path = os.path.join(install_path, 'venv')
    requirements_path = os.path.join(install_path, 'requirements.txt')

    print_section("Setting Up Python Environment")

    # Check if venv exists
    if os.path.exists(venv_path):
        print_success(f"Virtual environment already exists at {venv_path}")
        if not prompt_yes_no("Reinstall dependencies?", default=False):
            return venv_path
    else:
        print_info("Creating virtual environment...")
        try:
            subprocess.run([sys.executable, '-m', 'venv', venv_path], check=True)
            print_success("Virtual environment created")
        except subprocess.CalledProcessError as e:
            print_error(f"Failed to create venv: {e}")
            print_info("You may need to install python3-venv: sudo apt install python3-venv")
            return None

    # Install requirements
    pip_path = os.path.join(venv_path, 'bin', 'pip')
    if os.path.exists(requirements_path):
        print_info("Installing dependencies...")
        try:
            subprocess.run([pip_path, 'install', '-r', requirements_path], check=True)
            print_success("Dependencies installed successfully")
        except subprocess.CalledProcessError as e:
            print_error(f"Failed to install dependencies: {e}")
            return None
    else:
        print_warning(f"No requirements.txt found at {requirements_path}")

    return venv_path

def print_banner():
    banner = f"""
{Colors.CYAN}{Colors.BOLD}
    ╔═══════════════════════════════════════════════════════════════╗
    ║                                                               ║
    ║   ██████╗  ██████╗ ███████╗████████╗███████╗██████╗           ║
    ║   ██╔══██╗██╔═══██╗██╔════╝╚══██╔══╝██╔════╝██╔══██╗          ║
    ║   ██████╔╝██║   ██║███████╗   ██║   █████╗  ██████╔╝          ║
    ║   ██╔═══╝ ██║   ██║╚════██║   ██║   ██╔══╝  ██╔══██╗          ║
    ║   ██║     ╚██████╔╝███████║   ██║   ███████╗██║  ██║          ║
    ║   ╚═╝      ╚═════╝ ╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝          ║
    ║                                                               ║
    ║        ██████╗██╗  ██╗ █████╗ ███╗   ██╗                      ║
    ║       ██╔════╝██║  ██║██╔══██╗████╗  ██║                      ║
    ║       ██║     ███████║███████║██╔██╗ ██║                      ║
    ║       ██║     ██╔══██║██╔══██║██║╚██╗██║                      ║
    ║       ╚██████╗██║  ██║██║  ██║██║ ╚████║                      ║
    ║        ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝                      ║
    ║                                                               ║
    ║             {Colors.YELLOW}AI Bot for the Fediverse{Colors.CYAN}                        ║
    ║                                                               ║
    ╚═══════════════════════════════════════════════════════════════╝
{Colors.END}"""
    print(banner)

def print_section(title):
    width = 50
    print(f"\n{Colors.BLUE}{Colors.BOLD}{'═' * width}{Colors.END}")
    print(f"{Colors.BLUE}{Colors.BOLD}  {title}{Colors.END}")
    print(f"{Colors.BLUE}{Colors.BOLD}{'═' * width}{Colors.END}\n")

def print_success(msg):
    print(f"{Colors.GREEN}✓ {msg}{Colors.END}")

def print_error(msg):
    print(f"{Colors.RED}✗ {msg}{Colors.END}")

def print_info(msg):
    print(f"{Colors.CYAN}ℹ {msg}{Colors.END}")

def print_warning(msg):
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.END}")

def readline_safe_color(color_code):
    """Wrap ANSI color code with readline escape markers to fix cursor positioning.

    Readline needs \001 and \002 around non-printing characters so it can
    correctly calculate the visible prompt length for cursor positioning.
    """
    return f"\001{color_code}\002"

def prompt(question, default=None, secret=False, required=True, editable=False):
    """Prompt user for input with optional default value

    If editable=True and default is provided, the default will be pre-filled
    in the input line for easy editing (requires readline support).
    """
    # Standard prompt with colors (for non-editable mode)
    prompt_text = f"{Colors.BOLD}{question}{Colors.END}: "
    # Readline-safe prompt with escape markers (for editable mode)
    rl_prompt_text = f"{readline_safe_color(Colors.BOLD)}{question}{readline_safe_color(Colors.END)}: "

    while True:
        if secret:
            value = getpass.getpass(prompt_text)
        else:
            # Use readline to pre-fill default for editing
            if default and editable:
                try:
                    import readline

                    # Ensure proper key bindings for navigation
                    try:
                        readline.parse_and_bind(r'"\e[H": beginning-of-line')  # Home key
                        readline.parse_and_bind(r'"\e[F": end-of-line')        # End key
                        readline.parse_and_bind(r'"\e[1~": beginning-of-line') # Home (alternate)
                        readline.parse_and_bind(r'"\e[4~": end-of-line')       # End (alternate)
                        readline.parse_and_bind(r'"\C-a": beginning-of-line')  # Ctrl+A
                        readline.parse_and_bind(r'"\C-e": end-of-line')        # Ctrl+E
                    except Exception:
                        pass

                    def prefill():
                        readline.insert_text(str(default))
                        readline.redisplay()

                    readline.set_pre_input_hook(prefill)
                    print(f"  {Colors.DIM}(Use Ctrl+A for start, Ctrl+E for end, arrows to navigate){Colors.END}")
                    # Use readline-safe prompt to fix cursor positioning
                    value = input(rl_prompt_text)
                    readline.set_pre_input_hook(None)
                except ImportError:
                    # Fallback if readline not available
                    print(f"  {Colors.DIM}(Current: {default[:80]}...){Colors.END}" if len(str(default)) > 80 else f"  {Colors.DIM}(Current: {default}){Colors.END}")
                    value = input(prompt_text)
            elif default:
                prompt_text = f"{Colors.BOLD}{question}{Colors.END} [{Colors.DIM}{str(default)[:50]}{'...' if len(str(default)) > 50 else ''}{Colors.END}]: "
                value = input(prompt_text)
            else:
                value = input(prompt_text)

        if not value and default:
            return default
        if value or not required:
            return value
        print_error("This field is required.")

def prompt_autocomplete(question, options, show_list=True):
    """Prompt with tab autocomplete for selecting from a list of options"""
    import readline

    # Set up autocomplete
    matches = []

    def completer(text, state):
        if state == 0:
            # Build list of matches
            matches.clear()
            text_lower = text.lower()
            for opt in options:
                if opt.lower().startswith(text_lower):
                    matches.append(opt)
        if state < len(matches):
            return matches[state]
        return None

    # Configure readline
    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()

    readline.set_completer(completer)
    readline.set_completer_delims('')  # Don't split on any character
    readline.parse_and_bind('tab: complete')

    try:
        print()
        print(f"  {Colors.CYAN}{'─' * 48}{Colors.END}")
        print(f"  {Colors.BOLD}{question}{Colors.END}")
        print(f"  {Colors.CYAN}{'─' * 48}{Colors.END}")

        if show_list:
            print()
            # Show options in columns
            opt_list = list(options)
            col_width = max(len(o) for o in opt_list) + 2
            cols = max(1, 60 // col_width)

            for i in range(0, len(opt_list), cols):
                row = opt_list[i:i+cols]
                print("    " + "".join(f"{Colors.GREEN}{o:<{col_width}}{Colors.END}" for o in row))

        print()
        print(f"  {Colors.DIM}(Type name or press TAB to autocomplete){Colors.END}")

        while True:
            value = input(f"  {Colors.BOLD}▶ {Colors.END}").strip()

            if not value:
                continue

            # Check for exact match
            if value in options:
                return value

            # Check for case-insensitive match
            for opt in options:
                if opt.lower() == value.lower():
                    return opt

            # Check for partial match (if only one matches)
            partial_matches = [o for o in options if o.lower().startswith(value.lower())]
            if len(partial_matches) == 1:
                return partial_matches[0]
            elif len(partial_matches) > 1:
                print(f"  {Colors.YELLOW}Multiple matches: {', '.join(partial_matches[:5])}{Colors.END}")
            else:
                print(f"  {Colors.RED}No match found. Try again or press TAB.{Colors.END}")

    finally:
        # Restore old completer
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)


def prompt_choice(question, options, allow_multiple=False, icons=None):
    """Prompt user to select from options"""
    width = 48
    print()
    print(f"  {Colors.CYAN}{'─' * width}{Colors.END}")
    print(f"  {Colors.BOLD}{question}{Colors.END}")
    print(f"  {Colors.CYAN}{'─' * width}{Colors.END}")
    print()

    for i, (key, desc) in enumerate(options.items(), 1):
        print(f"    {Colors.GREEN}{i}{Colors.END}. {desc}")

    print()

    if allow_multiple:
        print_info("Enter numbers separated by spaces (e.g., '1 2 3') or 'all'")
        while True:
            choice = input(f"  {Colors.BOLD}▶ Selection: {Colors.END}").strip().lower()
            if choice == 'all':
                return list(options.keys())
            try:
                indices = [int(x) for x in choice.split()]
                if all(1 <= i <= len(options) for i in indices):
                    keys = list(options.keys())
                    return [keys[i-1] for i in indices]
            except ValueError:
                pass
            print_error("Invalid selection. Try again.")
    else:
        while True:
            try:
                choice = int(input(f"  {Colors.BOLD}▶ Selection: {Colors.END}"))
                if 1 <= choice <= len(options):
                    return list(options.keys())[choice - 1]
            except ValueError:
                pass
            print_error("Invalid selection. Try again.")

def prompt_yes_no(question, default=True):
    """Prompt user for yes/no"""
    default_str = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{Colors.BOLD}{question}{Colors.END} [{default_str}]: ").strip().lower()
        if not answer:
            return default
        if answer in ('y', 'yes'):
            return True
        if answer in ('n', 'no'):
            return False
        print_error("Please answer 'y' or 'n'")

def get_install_path():
    """Get the installation path"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return script_dir


def load_bots_config(install_path):
    """Load bots_config.py and return the config dictionaries"""
    config_path = os.path.join(install_path, 'bots_config.py')
    if not os.path.exists(config_path):
        return None, None, None

    # Load the config module
    import importlib.util
    spec = importlib.util.spec_from_file_location("bots_config", config_path)
    config_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config_module)

    image_bots = getattr(config_module, 'IMAGE_BOTS', {})
    text_bots = getattr(config_module, 'TEXT_BOTS', {})
    image_defaults = getattr(config_module, 'IMAGE_BOT_DEFAULTS', {})

    return image_bots, text_bots, image_defaults


def show_bot_status(install_path):
    """Show status of all bots - running/not running with PIDs"""
    print_section("Bot Status")

    image_bots, text_bots, image_defaults = load_bots_config(install_path)

    if image_bots is None:
        print_error("bots_config.py not found!")
        return

    current_host = get_current_host()

    # Check if main service is running
    try:
        result = subprocess.run(
            ['systemctl', 'is-active', 'posterchan'],
            capture_output=True, text=True
        )
        service_active = result.stdout.strip() == 'active'
    except Exception:
        service_active = False

    if service_active:
        print(f"  {Colors.GREEN}●{Colors.END} Service: {Colors.GREEN}running{Colors.END}")
    else:
        print(f"  {Colors.RED}●{Colors.END} Service: {Colors.RED}stopped{Colors.END}")
    print()

    # Get running processes using pgrep for reliability
    running_procs = {}
    try:
        result = subprocess.run(
            ['pgrep', '-af', 'main.py'],
            capture_output=True, text=True
        )
        for line in result.stdout.strip().split('\n'):
            if line and 'main.py' in line:
                parts = line.split(None, 1)  # Split into PID and command
                if len(parts) >= 2:
                    pid = parts[0]
                    cmd = parts[1]
                    running_procs[cmd] = pid
    except Exception:
        pass

    # Get bots for current host
    host_image_bots = [(n, c) for n, c in image_bots.items() if c.get('host') == current_host]
    host_text_bots = [(n, c) for n, c in text_bots.items() if c.get('host') == current_host]

    def find_bot_pid(bot_name, modes=None, is_image=False):
        """Try to find PID for a bot based on its config"""
        for cmd, pid in running_procs.items():
            if is_image and '--image' in cmd:
                return pid
            elif modes:
                # Check if the mode flag is in the command
                for mode in modes:
                    if mode in cmd:
                        return pid
        return None

    # Count running
    running_count = 0
    total_count = len(host_image_bots) + len(host_text_bots)

    # Text bots
    if host_text_bots:
        print(f"{Colors.CYAN}{Colors.BOLD}Text Bots:{Colors.END}")
        for name, config in host_text_bots:
            modes = config.get('modes', [])
            pid = find_bot_pid(name, modes=modes)
            if pid:
                running_count += 1
                print(f"  {Colors.GREEN}●{Colors.END} {name:<20} {Colors.GREEN}running{Colors.END}  PID: {pid}")
            else:
                print(f"  {Colors.RED}●{Colors.END} {name:<20} {Colors.RED}stopped{Colors.END}")
        print()

    # Image bots
    if host_image_bots:
        # For image bots, check how many --image processes are running
        image_pids = [pid for cmd, pid in running_procs.items() if '--image' in cmd]
        schedule_hours = [0, 6, 12, 18]
        hours_str = ', '.join(f'{h}:00' for h in schedule_hours)

        # Calculate next scheduled run time
        now = time.localtime()
        current_hour = now.tm_hour
        current_min = now.tm_min
        next_run_time = None

        for h in sorted(schedule_hours):
            if h > current_hour or (h == current_hour and current_min < 5):
                # Next run is today at hour h
                next_run_time = time.mktime(time.struct_time((
                    now.tm_year, now.tm_mon, now.tm_mday,
                    h, 0, 0, now.tm_wday, now.tm_yday, now.tm_isdst
                )))
                break

        if next_run_time is None:
            # Next run is tomorrow at first scheduled hour
            tomorrow = time.time() + 86400
            tom = time.localtime(tomorrow)
            next_run_time = time.mktime(time.struct_time((
                tom.tm_year, tom.tm_mon, tom.tm_mday,
                min(schedule_hours), 0, 0, tom.tm_wday, tom.tm_yday, tom.tm_isdst
            )))

        next_run_str = time.strftime('%Y-%m-%d %H:%M', time.localtime(next_run_time))

        print(f"{Colors.CYAN}{Colors.BOLD}Image Bots:{Colors.END} {Colors.DIM}(daily at {hours_str}){Colors.END}")

        # Show currently running image processes
        if image_pids:
            print(f"  {Colors.GREEN}●{Colors.END} {len(image_pids)} image bot(s) currently generating")
            for pid in image_pids[:5]:  # Show max 5 PIDs
                print(f"      {Colors.DIM}PID: {pid}{Colors.END}")
            if len(image_pids) > 5:
                print(f"      {Colors.DIM}... and {len(image_pids) - 5} more{Colors.END}")
            running_count += len(image_pids)
        else:
            print(f"  {Colors.DIM}○{Colors.END} No image bots currently running")

        print(f"  {Colors.CYAN}~{Colors.END} Next run: {Colors.BOLD}{next_run_str}{Colors.END}")

        print(f"\n  {Colors.DIM}Configured: {len(host_image_bots)} image bots{Colors.END}")
        # Show first few image bot names
        for name, config in host_image_bots[:8]:
            prompt_preview = config.get('prompt', '')[:30]
            print(f"    {Colors.DIM}- {name}: {prompt_preview}...{Colors.END}")
        if len(host_image_bots) > 8:
            print(f"    {Colors.DIM}... and {len(host_image_bots) - 8} more{Colors.END}")
        print()

    # Summary
    print(f"{Colors.BOLD}Summary:{Colors.END}")
    print(f"  Total configured: {total_count} bots for {current_host}")
    if service_active:
        print(f"  Service status: {Colors.GREEN}running{Colors.END}")
    else:
        print(f"  Service status: {Colors.RED}stopped{Colors.END} - run 'Deploy' to start")


def view_current_config(install_path):
    """Display the current bots_config.py configuration"""
    print_section("Current Configuration (bots_config.py)")

    image_bots, text_bots, image_defaults = load_bots_config(install_path)

    if image_bots is None:
        print_error("bots_config.py not found!")
        return

    import socket
    current_host = socket.gethostname().split('.')[0]
    print_info(f"Current host: {current_host}")
    print()

    # Show image bots
    host_image_bots = [(n, c) for n, c in image_bots.items() if c.get('host') == current_host]
    schedule_hours = [0, 6, 12, 18]

    if host_image_bots:
        print(f"{Colors.CYAN}{Colors.BOLD}Image Bots ({len(host_image_bots)}):{Colors.END} {Colors.DIM}(daily at {schedule_hours}:00){Colors.END}")
        for name, config in host_image_bots:
            prompt = config.get('prompt', '')[:50]
            if len(config.get('prompt', '')) > 50:
                prompt += '...'
            print(f"  {Colors.GREEN}{name}{Colors.END}")
            print(f"    {Colors.DIM}Prompt: {prompt}{Colors.END}")
        print()

    # Show text bots
    host_text_bots = [(n, c) for n, c in text_bots.items() if c.get('host') == current_host]
    if host_text_bots:
        print(f"{Colors.CYAN}{Colors.BOLD}Text Bots ({len(host_text_bots)}):{Colors.END}")
        for name, config in host_text_bots:
            platform = config.get('platform', 'misskey')
            modes = ' '.join(config.get('modes', []))
            server = config.get('server', config.get('matrix_server', 'N/A'))
            print(f"  {Colors.GREEN}{name}{Colors.END}")
            print(f"    {Colors.DIM}Platform: {platform} | Server: {server}{Colors.END}")
            print(f"    {Colors.DIM}Modes: {modes}{Colors.END}")
        print()

    # Show all hosts summary
    all_hosts = set()
    for config in image_bots.values():
        if config.get('host'):
            all_hosts.add(config['host'])
    for config in text_bots.values():
        if config.get('host'):
            all_hosts.add(config['host'])

    if all_hosts:
        print(f"{Colors.CYAN}{Colors.BOLD}All Hosts:{Colors.END}")
        for host in sorted(all_hosts):
            img_count = len([c for c in image_bots.values() if c.get('host') == host])
            txt_count = len([c for c in text_bots.values() if c.get('host') == host])
            marker = f" {Colors.YELLOW}(current){Colors.END}" if host == current_host else ""
            print(f"  {Colors.GREEN}{host}{Colors.END}{marker}: {img_count} image, {txt_count} text bots")
        print()

    # Image bot defaults
    if image_defaults:
        schedule_hours = [0, 6, 12, 18]
        print(f"{Colors.CYAN}{Colors.BOLD}Image Bot Defaults:{Colors.END}")
        print(f"  {Colors.DIM}Server: {image_defaults.get('server', 'N/A')}{Colors.END}")
        print(f"  {Colors.DIM}Username: {image_defaults.get('username', 'N/A')}{Colors.END}")
        print(f"  {Colors.DIM}Text: {image_defaults.get('text', 'N/A')}{Colors.END}")
        print(f"  {Colors.GREEN}Schedule: daily at {schedule_hours}:00{Colors.END}")
        print(f"  {Colors.DIM}Random Scenes: {image_defaults.get('random_scenes', False)}{Colors.END}")
        print()


def env_quote(value):
    """Quote a value for a systemd EnvironmentFile as a single valid line.

    Escapes backslashes, double quotes, and newlines so that multi-line prompts
    or values containing quotes can't break the one-line-per-variable format.
    Returns the value wrapped in double quotes.
    """
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    s = s.replace("\r", "\\r").replace("\n", "\\n")
    return f'"{s}"'


def generate_services_from_config(install_path):
    """Generate systemd services from bots_config.py"""
    print_section("Generating Services from bots_config.py")

    image_bots, text_bots, image_defaults = load_bots_config(install_path)

    if image_bots is None:
        print_error("bots_config.py not found!")
        return

    import socket
    current_host = socket.gethostname().split('.')[0]
    user = os.environ.get('USER', 'root')

    venv_python = os.path.join(install_path, 'venv', 'bin', 'python')
    if not os.path.exists(venv_python):
        venv_python = sys.executable

    services_dir = os.path.join(install_path, 'services')
    os.makedirs(services_dir, exist_ok=True)

    generated = []

    # Generate image bot services
    for name, config in image_bots.items():
        if config.get('host') != current_host:
            continue

        # Build env file
        env_lines = [
            '# Auto-generated from bots_config.py',
            f'MISSKEY_SERVER="{image_defaults.get("server", "")}"',
            f'MISSKEY_USERNAME="{image_defaults.get("username", "")}"',
            f'MISSKEY_ACCESS_TOKEN="{image_defaults.get("access_token", "")}"',
            f'IMAGE_POSTER_PROMPT={env_quote(config.get("prompt", ""))}',
            f'IMAGE_POSTER_TEXT={env_quote(image_defaults.get("text", ""))}',
        ]
        if image_defaults.get('random_scenes'):
            env_lines.append('IMAGE_POSTER_RANDOM_SCENES="true"')
        if image_defaults.get('freq'):
            env_lines.append(f'IMAGE_POSTER_FREQ="{image_defaults.get("freq")}"')

        # Add global settings
        try:
            from bots_config import AI_API_URL, AI_API_KEY, TIMEZONE, POSTERCHANAI_API_KEY
            env_lines.append(f'OPENAI_ENDPOINT="{AI_API_URL}"')
            env_lines.append(f'OPENAI_API_KEY="{AI_API_KEY}"')
            env_lines.append(f'TIMEZONE="{TIMEZONE}"')
            # Image backend settings are read directly from bots_config by config.py;
            # only the API key is bridged via env.
            if POSTERCHANAI_API_KEY:
                env_lines.append(f'POSTERCHANAI_API_KEY={env_quote(POSTERCHANAI_API_KEY)}')
        except ImportError:
            pass

        env_path = os.path.join(install_path, f'.env.{name}')
        with open(env_path, 'w') as f:
            f.write('\n'.join(env_lines))
        os.chmod(env_path, 0o600)

        # Build service file
        service_content = f"""[Unit]
Description=Posterchan {name} Image Bot
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={install_path}
EnvironmentFile={env_path}
ExecStart={venv_python} {install_path}/main.py --image
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
        service_path = os.path.join(services_dir, f'{name}.service')
        with open(service_path, 'w') as f:
            f.write(service_content)

        generated.append(name)
        print_success(f"Generated {name}.service (image bot)")

    # Generate text bot services
    for name, config in text_bots.items():
        if config.get('host') != current_host:
            continue

        platform = config.get('platform', 'misskey')
        modes = ' '.join(config.get('modes', [f'--{platform}']))

        # Build env file
        env_lines = ['# Auto-generated from bots_config.py']

        if platform == 'misskey':
            env_lines.append(f'MISSKEY_SERVER="{config.get("server", "")}"')
            env_lines.append(f'MISSKEY_USERNAME="{config.get("username", "")}"')
            env_lines.append(f'MISSKEY_ACCESS_TOKEN="{config.get("access_token", "")}"')
        elif platform == 'pleroma':
            env_lines.append(f'PLEROMA_ENDPOINT="{config.get("server", "")}"')
            env_lines.append(f'PLEROMA_USERNAME="{config.get("username", "")}"')
            env_lines.append(f'PLEROMA_ACCESS_TOKEN="{config.get("access_token", "")}"')
            if config.get('pleroma_admin_token'):
                env_lines.append(f'PLEROMA_ADMIN_TOKEN="{config.get("pleroma_admin_token")}"')

        # Matrix settings (any platform — a bot may listen on Matrix while also
        # posting elsewhere, e.g. --matrix combined with --nitter). Mirrors botctl.
        if config.get('matrix_server'):
            env_lines.append(f'MATRIX_SERVER="{config.get("matrix_server")}"')
            env_lines.append(f'MATRIX_USER_ID="{config.get("matrix_user_id", "")}"')
            env_lines.append(f'MATRIX_ACCESS_TOKEN="{config.get("matrix_access_token", "")}"')
            if config.get('matrix_room_id'):
                env_lines.append(f'MATRIX_ROOM_ID="{config.get("matrix_room_id")}"')
            if config.get('matrix_admins'):
                env_lines.append(f'MATRIX_ADMINS="{config.get("matrix_admins")}"')
            if config.get('matrix_verify_ssl') is not None:
                env_lines.append(f'MATRIX_VERIFY_SSL="{str(config.get("matrix_verify_ssl")).lower()}"')
            if config.get('shamebot_rooms'):
                _rooms = config['shamebot_rooms']
                if isinstance(_rooms, list):
                    env_lines.append(f'SHAMEBOT_ROOMS="{",".join(_rooms)}"')
                else:
                    env_lines.append(f'SHAMEBOT_ROOMS="{_rooms}"')

        # Nitter RSS → room/fediverse feeds (for --nitter mode, any platform)
        if config.get('nitter_feeds'):
            # Single-quote the JSON so systemd's EnvironmentFile parser takes it
            # verbatim (JSON has no single quotes, so no escaping needed).
            _feeds_json = json.dumps(config.get('nitter_feeds'))
            env_lines.append(f"NITTER_FEEDS='{_feeds_json}'")
        if config.get('nitter_poll_seconds'):
            env_lines.append(f'NITTER_POLL_SECONDS="{config.get("nitter_poll_seconds")}"')

        # Sticker macros (Matrix): enable "!name" posting of media auto-discovered from
        # the stickers/ folder (no per-sticker listing). Mirrors botctl.
        if config.get('stickers_enabled'):
            env_lines.append('STICKERS_ENABLED="true"')

        # Extra hostnames trusted for media downloads (SSRF allowlist) — mirrors botctl.
        if config.get('trusted_media_hosts'):
            _tmh = config.get('trusted_media_hosts')
            _tmh = ','.join(_tmh) if isinstance(_tmh, (list, tuple)) else str(_tmh)
            env_lines.append(f'TRUSTED_MEDIA_HOSTS="{_tmh}"')

        if config.get('prompt'):
            env_lines.append(f'PROMPT={env_quote(config.get("prompt"))}')

        # Database settings
        if config.get('sql_database'):
            try:
                from bots_config import SQL_USER, SQL_PASS, SQL_HOST
                env_lines.append(f'SQL_USER="{SQL_USER}"')
                env_lines.append(f'SQL_PASS="{SQL_PASS}"')
                env_lines.append(f'SQL_HOST="{SQL_HOST}"')
                env_lines.append(f'SQL_DATABASE="{config.get("sql_database")}"')
            except ImportError:
                pass

        # Blockbot settings
        if config.get('block_image'):
            env_lines.append(f'BLOCK_IMAGE="{config.get("block_image")}"')
        if config.get('block_prompt'):
            env_lines.append(f'BLOCK_PROMPT={env_quote(config.get("block_prompt"))}')

        # Welcome settings
        if config.get('welcome_image'):
            env_lines.append(f'WELCOME_IMAGE="{config.get("welcome_image")}"')
        if config.get('welcome_message'):
            env_lines.append(f'WELCOME_MESSAGE={env_quote(config.get("welcome_message"))}')
        if config.get('welcome_lookback_minutes'):
            env_lines.append(f'WELCOME_LOOKBACK_MINUTES="{config.get("welcome_lookback_minutes")}"')
        if config.get('welcome_prompt'):
            env_lines.append(f'WELCOME_PROMPT={env_quote(config.get("welcome_prompt"))}')

        # Report settings
        if config.get('report_image'):
            env_lines.append(f'REPORT_IMAGE="{config.get("report_image")}"')
        if config.get('report_prompt'):
            env_lines.append(f'REPORT_PROMPT={env_quote(config.get("report_prompt"))}')

        # Unfollow settings
        if config.get('unfollow_image'):
            env_lines.append(f'UNFOLLOW_IMAGE="{config.get("unfollow_image")}"')
        if config.get('unfollow_silent_mode') is not None:
            env_lines.append(f'UNFOLLOW_SILENT_MODE="{str(config.get("unfollow_silent_mode")).lower()}"')

        # TTS settings (for /narrate command and auto_narrate)
        if config.get('tts_voice'):
            env_lines.append(f'TTS_VOICE="{config.get("tts_voice")}"')
        if config.get('tts_rate'):
            env_lines.append(f'TTS_RATE="{config.get("tts_rate")}"')
        if config.get('tts_pitch'):
            env_lines.append(f'TTS_PITCH="{config.get("tts_pitch")}"')
        if config.get('auto_narrate'):
            env_lines.append('AUTO_NARRATE="true"')
        if config.get('video_encoder'):
            env_lines.append(f'VIDEO_ENCODER="{config.get("video_encoder")}"')
        if config.get('sleep'):
            env_lines.append(f'RESPONSE_DELAY="{config.get("sleep")}"')

        # Global AI settings
        try:
            from bots_config import AI_API_URL, AI_API_KEY, TIMEZONE, SEARXNG_URL, POSTERCHANAI_API_KEY
            if not config.get('skip_ai'):
                env_lines.append(f'OPENAI_ENDPOINT="{AI_API_URL}"')
                env_lines.append(f'OPENAI_API_KEY="{AI_API_KEY}"')
            env_lines.append(f'TIMEZONE="{TIMEZONE}"')
            env_lines.append(f'SEARXNG_URL="{SEARXNG_URL}"')
            # Image backend settings are read directly from bots_config by config.py;
            # only the API key (per-bot override, else global) is bridged via env.
            pc_key = config.get('posterchanai_api_key') or POSTERCHANAI_API_KEY
            if pc_key:
                env_lines.append(f'POSTERCHANAI_API_KEY={env_quote(pc_key)}')
        except ImportError:
            pass

        env_path = os.path.join(install_path, f'.env.{name}')
        with open(env_path, 'w') as f:
            f.write('\n'.join(env_lines))
        os.chmod(env_path, 0o600)

        # Build service file
        service_content = f"""[Unit]
Description=Posterchan {name} ({platform})
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={install_path}
EnvironmentFile={env_path}
ExecStart={venv_python} {install_path}/main.py {modes}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
        service_path = os.path.join(services_dir, f'{name}.service')
        with open(service_path, 'w') as f:
            f.write(service_content)

        generated.append(name)
        print_success(f"Generated {name}.service ({platform})")

    if generated:
        print()
        print_info(f"Generated {len(generated)} services in {services_dir}/")
        print()
        print(f"{Colors.BOLD}To install:{Colors.END}")
        print(f"  {Colors.CYAN}sudo cp {services_dir}/*.service /etc/systemd/system/{Colors.END}")
        print(f"  {Colors.CYAN}sudo systemctl daemon-reload{Colors.END}")
        print()

        if prompt_yes_no("Install services now?", default=True):
            for name in generated:
                src = os.path.join(services_dir, f'{name}.service')
                dst = f'/etc/systemd/system/{name}.service'
                try:
                    subprocess.run(['sudo', 'cp', src, dst], check=True)
                    print_success(f"Installed {name}.service")
                except subprocess.CalledProcessError:
                    print_error(f"Failed to install {name}.service")

            subprocess.run(['sudo', 'systemctl', 'daemon-reload'], check=False)
            print_success("Reloaded systemd")

            if prompt_yes_no("Enable and start all services?", default=False):
                for name in generated:
                    try:
                        subprocess.run(['sudo', 'systemctl', 'enable', name], check=False)
                        subprocess.run(['sudo', 'systemctl', 'start', name], check=False)
                        print_success(f"Started {name}")
                    except Exception:
                        print_warning(f"Could not start {name}")
    else:
        print_warning(f"No bots configured for host '{current_host}'")


def get_current_host():
    """Get the current hostname"""
    import socket
    return socket.gethostname().split('.')[0]


GLOBAL_SETTINGS_DEFAULTS = {
    'AI_API_URL': "https://ai.poster.place/api/chat/completions",
    'AI_API_KEY': "",
    'AI_MODEL': "",
    'COMFYUI_API_ENDPOINT': "http://192.168.0.85:8188",
    'STABLE_DIFFUSION_ENDPOINT': "",
    'USE_POSTERCHANAI': False,
    'POSTERCHANAI_API_ENDPOINT': "http://192.168.0.1:3051",
    'POSTERCHANAI_API_KEY': "",
    'POSTERCHANAI_USERNAME': "admin",
    'POSTERCHANAI_PASSWORD': "admin",
    'SQL_USER': "root",
    'SQL_PASS': "sql",
    'SQL_HOST': "",
    'SEARXNG_URL': "https://search.poster.place",
    'TIMEZONE': "MST",
}


def load_global_settings(install_path):
    """Return the global settings dict from bots_config.py (defaults fill gaps)."""
    g = dict(GLOBAL_SETTINGS_DEFAULTS)
    config_path = os.path.join(install_path, 'bots_config.py')
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("bots_config", config_path)
        config_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config_module)
        for k in g:
            g[k] = getattr(config_module, k, g[k])
    except Exception:
        pass
    return g


def save_bots_config(install_path, image_bots, text_bots, image_defaults, globals_override=None):
    """Save the bots config back to bots_config.py with atomic write for safety.

    globals_override: optional dict of global-setting name -> new value; other
    globals are preserved from the existing file.
    """
    import tempfile

    config_path = os.path.join(install_path, 'bots_config.py')
    backup_path = config_path + '.backup'

    # Global settings: file values over defaults, then any explicit overrides.
    g = load_global_settings(install_path)
    if globals_override:
        g.update(globals_override)

    # Build the config content
    lines = [
        '"""',
        'Posterchan Bot Configuration',
        '"""',
        '',
        '# =============================================================================',
        '# GLOBAL SETTINGS',
        '# =============================================================================',
        '',
        '# AI API Configuration',
        f'AI_API_URL = {repr(g["AI_API_URL"])}',
        f'AI_API_KEY = {repr(g["AI_API_KEY"])}',
        f'AI_MODEL = {repr(g["AI_MODEL"])}',
        '',
        '# Image Generation',
        f'COMFYUI_API_ENDPOINT = {repr(g["COMFYUI_API_ENDPOINT"])}',
        f'STABLE_DIFFUSION_ENDPOINT = {repr(g["STABLE_DIFFUSION_ENDPOINT"])}  # Alternative to ComfyUI',
        '',
        '# Posterchanai backend (for native diffusers - recommended)',
        f'USE_POSTERCHANAI = {repr(g["USE_POSTERCHANAI"])}  # Set to True to use posterchanai native backend',
        f'POSTERCHANAI_API_ENDPOINT = {repr(g["POSTERCHANAI_API_ENDPOINT"])}  # Posterchanai server URL',
        f'POSTERCHANAI_API_KEY = {repr(g["POSTERCHANAI_API_KEY"])}  # API key for image generation',
        f'POSTERCHANAI_USERNAME = {repr(g["POSTERCHANAI_USERNAME"])}  # Fallback if no API key',
        f'POSTERCHANAI_PASSWORD = {repr(g["POSTERCHANAI_PASSWORD"])}  # Fallback if no API key',
        '',
        '# Database (for blockbot, welcome, report, etc.)',
        f'SQL_USER = {repr(g["SQL_USER"])}',
        f'SQL_PASS = {repr(g["SQL_PASS"])}',
        f'SQL_HOST = {repr(g["SQL_HOST"])}  # Empty for Unix socket',
        '',
        '# Search',
        f'SEARXNG_URL = {repr(g["SEARXNG_URL"])}',
        '',
        '# Timezone',
        f'TIMEZONE = {repr(g["TIMEZONE"])}',
        '',
        '# =============================================================================',
        '# IMAGE BOTS - Posts AI-generated images on schedule',
        '# =============================================================================',
        '',
        'IMAGE_BOTS = {',
    ]

    for name, config in image_bots.items():
        lines.append(f'    "{name}": {{')
        for key, value in config.items():
            # repr() produces a valid Python literal for any value type, including
            # multi-line strings (newlines/quotes escaped) — manual quoting can't.
            lines.append(f'        "{key}": {repr(value)},')
        lines.append('    },')
    lines.append('}')
    lines.append('')

    # IMAGE_BOT_DEFAULTS
    lines.append('# Common settings for all image bots')
    lines.append('IMAGE_BOT_DEFAULTS = {')
    for key, value in image_defaults.items():
        lines.append(f'    "{key}": {repr(value)},')
    lines.append('}')
    lines.append('')

    lines.append('# =============================================================================')
    lines.append('# TEXT BOTS - Responds to mentions with AI')
    lines.append('# =============================================================================')
    lines.append('')
    lines.append('TEXT_BOTS = {')

    for name, config in text_bots.items():
        lines.append(f'    "{name}": {{')
        for key, value in config.items():
            lines.append(f'        "{key}": {repr(value)},')
        lines.append('    },')
    lines.append('}')
    lines.append('')

    content = '\n'.join(lines)

    # Validate the content is valid Python before saving
    try:
        compile(content, '<string>', 'exec')
    except SyntaxError as e:
        print_error(f"Generated config has syntax error: {e}")
        print_error("Config NOT saved to prevent corruption.")
        return False

    # Create backup of current file
    try:
        if os.path.exists(config_path):
            shutil.copy2(config_path, backup_path)
    except Exception as e:
        print_warning(f"Could not create backup: {e}")

    # Write to temp file first, then rename (atomic on same filesystem)
    try:
        fd, temp_path = tempfile.mkstemp(dir=install_path, suffix='.py')
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(content)
            # Atomic rename
            os.replace(temp_path, config_path)
            print_success("Config saved successfully")
            return True
        except Exception:
            # Clean up temp file on error
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise
    except Exception as e:
        print_error(f"Failed to save config: {e}")
        if os.path.exists(backup_path):
            print_info(f"Backup available at: {backup_path}")
        return False


def edit_bot_config(install_path, image_bots, text_bots):
    """Edit an existing bot's configuration"""
    print_section("Edit Bot Configuration")

    # Get all bots (no host filter - can edit any bot)
    all_bots = {}
    for name, config in image_bots.items():
        all_bots[name] = ('image', config)
    for name, config in text_bots.items():
        all_bots[name] = ('text', config)

    if not all_bots:
        print_warning("No bots configured")
        return

    # Let user select bot with autocomplete
    bot_names = list(all_bots.keys()) + ['back']

    # Show bot list with info
    print(f"\n{Colors.CYAN}Available bots:{Colors.END}")
    for name, (typ, config) in all_bots.items():
        host = config.get('host', '?')
        print(f"  {Colors.GREEN}{name:<20}{Colors.END} {Colors.DIM}({typ}) [{host}]{Colors.END}")
    print(f"  {Colors.DIM}back{' ' * 16}← Back{Colors.END}")

    selected = prompt_autocomplete("Type bot name (TAB to complete):", bot_names, show_list=False)

    if selected in ('_back', 'back'):
        return

    if selected not in all_bots:
        print_error(f"Bot '{selected}' not found")
        return

    bot_type, config = all_bots[selected]

    print_section(f"Editing {selected}")

    # Show current config
    print(f"{Colors.BOLD}Current settings:{Colors.END}")
    for key, value in config.items():
        if 'token' in key.lower() or 'pass' in key.lower():
            display = '********'
        elif isinstance(value, str) and len(value) > 50:
            display = value[:50] + '...'
        else:
            display = value
        print(f"  {Colors.DIM}{key}{Colors.END}: {display}")
    print()

    # Edit options
    while True:
        edit_action = prompt_choice(
            "What would you like to change?",
            {
                'prompt': 'Edit prompt',
                'modes': 'Edit modes/features',
                'tts': 'TTS/Auto-narrate settings',
                'server': 'Edit server/credentials',
                'matrix': 'Matrix settings (room, admins, SSL)',
                'advanced': 'Other settings (any key: block/report/welcome prompts, etc.)',
                'host': 'Change host',
                'save': 'Save and go back',
                'cancel': 'Cancel (discard changes)',
            }
        )

        if edit_action == 'cancel':
            print_info("Changes discarded")
            return

        if edit_action == 'save':
            break

        if edit_action == 'prompt':
            current_prompt = config.get('prompt', '')
            print(f"\n{Colors.DIM}Current prompt:{Colors.END}")
            print(f"  {current_prompt[:200]}{'...' if len(current_prompt) > 200 else ''}")
            print()

            # For long prompts, offer editor option
            if len(current_prompt) > 100:
                edit_method = prompt_choice(
                    "How do you want to edit?",
                    {
                        'inline': 'Edit inline (Ctrl+A/E to navigate)',
                        'editor': f'Open in editor ({os.environ.get("EDITOR", "nano")})',
                        'back': '← Cancel',
                    }
                )

                if edit_method == 'back':
                    continue
                elif edit_method == 'editor':
                    import tempfile
                    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
                        f.write(current_prompt)
                        temp_path = f.name
                    editor = os.environ.get('EDITOR', 'nano')
                    subprocess.run([editor, temp_path])
                    with open(temp_path, 'r') as f:
                        new_prompt = f.read().strip()
                    os.remove(temp_path)
                else:
                    new_prompt = prompt("Prompt", default=current_prompt, required=False, editable=True)
            else:
                new_prompt = prompt("Prompt", default=current_prompt, required=False, editable=True)

            if new_prompt and new_prompt != current_prompt:
                config['prompt'] = new_prompt
                print_success("Prompt updated")
            else:
                print_info("Prompt unchanged")

        elif edit_action == 'modes':
            if bot_type == 'text':
                current_modes = config.get('modes', [])
                all_modes = ['--misskey', '--pleroma', '--matrix', '--blockbot', '--unfollowbot',
                           '--welcome', '--report', '--hashtagbot', '--nitter', '--ping', '--image']
                new_modes = list(current_modes)

                while True:
                    # Display current modes with toggle options
                    print(f"\n{Colors.BOLD}Current Modes:{Colors.END}")
                    for i, m in enumerate(all_modes, 1):
                        if m in new_modes:
                            status = f"{Colors.GREEN}[ON]{Colors.END}"
                        else:
                            status = f"{Colors.DIM}[off]{Colors.END}"
                        print(f"  {Colors.CYAN}{i:2}{Colors.END}. {m:<15} {status}")

                    print(f"\n  {Colors.DIM}Enter number to toggle, 'done' to save, 'cancel' to abort{Colors.END}")
                    choice = input(f"  {Colors.BOLD}▶ Toggle: {Colors.END}").strip().lower()

                    if choice == 'done':
                        if not new_modes:
                            print_error("At least one mode is required")
                            continue
                        break
                    elif choice == 'cancel':
                        print_warning("Cancelled - modes unchanged")
                        new_modes = current_modes
                        break
                    elif choice.isdigit():
                        idx = int(choice) - 1
                        if 0 <= idx < len(all_modes):
                            mode = all_modes[idx]
                            if mode in new_modes:
                                new_modes.remove(mode)
                                print(f"  {Colors.RED}✗{Colors.END} Removed {mode}")
                            else:
                                new_modes.append(mode)
                                print(f"  {Colors.GREEN}✓{Colors.END} Added {mode}")
                        else:
                            print_error(f"Invalid number. Enter 1-{len(all_modes)}")
                    else:
                        print_error("Enter a number, 'done', or 'cancel'")

                config['modes'] = new_modes
                print_success(f"Modes updated: {' '.join(new_modes)}")
            else:
                print_warning("Image bots don't have modes")

        elif edit_action == 'server':
            if bot_type == 'text':
                platform = config.get('platform', 'misskey')
                if platform == 'misskey':
                    config['server'] = prompt("Misskey server", default=config.get('server', ''))
                    config['username'] = prompt("Username", default=config.get('username', ''))
                    new_token = prompt("Access token (leave empty to keep)", required=False, secret=True)
                    if new_token:
                        config['access_token'] = new_token
                elif platform == 'pleroma':
                    config['server'] = prompt("Pleroma server", default=config.get('server', ''))
                    config['username'] = prompt("Username", default=config.get('username', ''))
                    new_token = prompt("Access token (leave empty to keep)", required=False, secret=True)
                    if new_token:
                        config['access_token'] = new_token
                print_success("Server settings updated")

        elif edit_action == 'matrix':
            if bot_type == 'text':
                config['matrix_server'] = prompt("Matrix homeserver URL",
                                                 default=config.get('matrix_server', ''), required=False)
                config['matrix_user_id'] = prompt("Bot user ID (e.g. @bot:server)",
                                                  default=config.get('matrix_user_id', ''), required=False)
                new_mtoken = prompt("Matrix access token (leave empty to keep)", required=False, secret=True)
                if new_mtoken:
                    config['matrix_access_token'] = new_mtoken
                room = prompt("Default room ID (e.g. !id:server)",
                              default=config.get('matrix_room_id', ''), required=False)
                if room:
                    config['matrix_room_id'] = room
                admins = prompt("Admin Matrix IDs for DM commands (comma-separated)",
                                default=config.get('matrix_admins', ''), required=False)
                if admins:
                    config['matrix_admins'] = admins
                if not config.get('matrix_admins'):
                    print_warning("No admins set — the bot will decline ALL room invites, and admin commands (join/leave/block) won't work. Add at least one Matrix ID (e.g. @you:server).")
                rooms_s = prompt("Shamebot rooms (comma-separated, for roasting matrix.org joiners)",
                                 default=','.join(config.get('shamebot_rooms', [])), required=False)
                if rooms_s:
                    config['shamebot_rooms'] = [r.strip() for r in rooms_s.split(',') if r.strip()]
                elif 'shamebot_rooms' in config:
                    del config['shamebot_rooms']
                if prompt_yes_no("Verify Matrix server TLS certificate?",
                                 default=config.get('matrix_verify_ssl', True)):
                    config.pop('matrix_verify_ssl', None)  # True is the default; keep config clean
                else:
                    config['matrix_verify_ssl'] = False
                if prompt_yes_no("Enable !name sticker macros (auto-post media from the stickers/ folder)?",
                                 default=config.get('stickers_enabled', False)):
                    config['stickers_enabled'] = True
                    _sdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stickers")
                    os.makedirs(_sdir, exist_ok=True)
                    print_success(f"Stickers enabled — drop media files in {_sdir} (filename → !name; list with !stickers)")
                else:
                    config.pop('stickers_enabled', None)
                print_success("Matrix settings updated")

        elif edit_action == 'advanced':
            # Generic editor for any remaining per-bot key (block_prompt,
            # report_prompt, welcome_*, nitter_feeds, posterchanai_api_key,
            # temperature, etc.). Lists are edited as JSON.
            import json as _json
            while True:
                print(f"\n{Colors.BOLD}All settings for this bot:{Colors.END}")
                for k, v in config.items():
                    disp = '********' if ('token' in k.lower() or 'pass' in k.lower() or 'key' in k.lower()) and v else v
                    if isinstance(disp, str) and len(disp) > 60:
                        disp = disp[:60] + '...'
                    print(f"  {Colors.DIM}{k}{Colors.END} = {disp}")
                adv = prompt_choice("Advanced edit:", {
                    'set': 'Set/change a key',
                    'remove': 'Remove a key',
                    'done': '← Done',
                })
                if adv == 'done':
                    break
                elif adv == 'set':
                    k = prompt("Key name (e.g. block_prompt, temperature, nitter_feeds)")
                    if not k:
                        continue
                    cur = config.get(k, '')
                    raw = prompt(f"Value for {k} (JSON for lists/numbers/bools)",
                                 default=str(cur) if cur != '' else '', required=False)
                    if raw == '':
                        continue
                    try:
                        config[k] = _json.loads(raw)  # parse lists/numbers/bools/null
                    except (ValueError, TypeError):
                        config[k] = raw                # plain string
                    print_success(f"Set {k}")
                elif adv == 'remove':
                    k = prompt("Key name to remove")
                    if k in config:
                        del config[k]
                        print_success(f"Removed {k}")
                    else:
                        print_error(f"Key '{k}' not found")

        elif edit_action == 'host':
            config['host'] = prompt("New host", default=config.get('host', get_current_host()))
            print_success(f"Host changed to {config['host']}")

        elif edit_action == 'tts':
            # Show current TTS settings
            print(f"\n{Colors.BOLD}Current TTS settings:{Colors.END}")
            print(f"  auto_narrate: {config.get('auto_narrate', False)}")
            print(f"  tts_voice: {config.get('tts_voice', 'not set')}")
            print(f"  tts_rate: {config.get('tts_rate', 'not set')}")
            print(f"  tts_pitch: {config.get('tts_pitch', 'not set')}")
            print()

            # Toggle auto_narrate
            current_auto = config.get('auto_narrate', False)
            if prompt_yes_no(f"Enable auto-narrate?", default=current_auto):
                config['auto_narrate'] = True
                print()
                print(f"{Colors.DIM}Popular voices:{Colors.END}")
                print(f"  en-US-AnaNeural       - Young female (cute, default)")
                print(f"  en-US-AriaNeural      - Adult female (natural)")
                print(f"  en-US-GuyNeural       - Adult male")
                print(f"  en-GB-SoniaNeural     - British female")
                print(f"  ja-JP-NanamiNeural    - Japanese female")
                print()
                voice = prompt("TTS voice", default=config.get('tts_voice', 'en-US-AnaNeural'))
                if voice:
                    config['tts_voice'] = voice
                rate = prompt("TTS rate (e.g., +10%, -5%)", default=config.get('tts_rate', ''), required=False)
                if rate:
                    config['tts_rate'] = rate
                elif 'tts_rate' in config:
                    del config['tts_rate']
                pitch = prompt("TTS pitch (e.g., +5Hz, -10Hz)", default=config.get('tts_pitch', ''), required=False)
                if pitch:
                    config['tts_pitch'] = pitch
                elif 'tts_pitch' in config:
                    del config['tts_pitch']
                print_success("TTS settings updated")
            else:
                if 'auto_narrate' in config:
                    del config['auto_narrate']
                print_info("Auto-narrate disabled")

    # Save changes
    if bot_type == 'image':
        image_bots[selected] = config
    else:
        text_bots[selected] = config

    # Reload and save
    _, _, image_defaults = load_bots_config(install_path)
    save_bots_config(install_path, image_bots, text_bots, image_defaults)
    print_success(f"Saved changes to {selected}")


def run_image_bot_once(install_path):
    """Run an image bot once to post a single image"""
    print_section("Run Image Bot Once")

    image_bots, text_bots, image_defaults = load_bots_config(install_path)

    if not image_bots:
        print_error("No image bots configured")
        return

    current_host = get_current_host()

    # Filter to current host's image bots
    host_bots = {n: c for n, c in image_bots.items() if c.get('host') == current_host}

    if not host_bots:
        print_warning(f"No image bots configured for host '{current_host}'")
        print_info("Available hosts: " + ", ".join(set(c.get('host', '?') for c in image_bots.values())))
        return

    # Show bot list
    print(f"\n{Colors.CYAN}Image bots on {current_host}:{Colors.END}")
    for name, config in host_bots.items():
        prompt_preview = config.get('prompt', '')[:50] + '...' if len(config.get('prompt', '')) > 50 else config.get('prompt', '')
        print(f"  {Colors.GREEN}{name:<20}{Colors.END} {Colors.DIM}{prompt_preview}{Colors.END}")
    print(f"  {Colors.DIM}_back{' ' * 15}← Back{Colors.END}")

    bot_names = list(host_bots.keys()) + ['_back']
    selected = prompt_autocomplete("Select image bot (TAB to complete):", bot_names, show_list=False)

    if selected in ('_back', 'back'):
        return

    if selected not in host_bots:
        print_error(f"Bot '{selected}' not found")
        return

    config = host_bots[selected]
    print()
    print(f"{Colors.BOLD}Running {selected}...{Colors.END}")
    print(f"  Prompt: {Colors.DIM}{config.get('prompt', 'N/A')[:80]}...{Colors.END}")

    # Build environment variables for image bot
    env = os.environ.copy()

    # Add image bot settings
    env['MISSKEY_SERVER'] = image_defaults.get('server', '')
    env['MISSKEY_USERNAME'] = image_defaults.get('username', '')
    env['MISSKEY_ACCESS_TOKEN'] = image_defaults.get('access_token', '')
    env['IMAGE_POSTER_PROMPT'] = config.get('prompt', '')
    env['IMAGE_POSTER_TEXT'] = image_defaults.get('text', '')

    if image_defaults.get('random_scenes'):
        env['IMAGE_POSTER_RANDOM_SCENES'] = 'true'

    # Add global settings
    try:
        from bots_config import AI_API_URL, AI_API_KEY, AI_MODEL, COMFYUI_API_ENDPOINT, TIMEZONE, USE_POSTERCHANAI, POSTERCHANAI_API_ENDPOINT, POSTERCHANAI_API_KEY, POSTERCHANAI_USERNAME, POSTERCHANAI_PASSWORD
        env['OPENAI_ENDPOINT'] = AI_API_URL
        env['OPENAI_API_KEY'] = AI_API_KEY
        env['TIMEZONE'] = TIMEZONE
        # Image backend - prefer posterchanai if enabled
        if USE_POSTERCHANAI:
            env['USE_POSTERCHANAI'] = 'true'
            env['POSTERCHANAI_API_ENDPOINT'] = POSTERCHANAI_API_ENDPOINT
            if POSTERCHANAI_API_KEY:
                env['POSTERCHANAI_API_KEY'] = POSTERCHANAI_API_KEY
            else:
                env['POSTERCHANAI_USERNAME'] = POSTERCHANAI_USERNAME
                env['POSTERCHANAI_PASSWORD'] = POSTERCHANAI_PASSWORD
        else:
            env['COMFYUI_API_ENDPOINT'] = COMFYUI_API_ENDPOINT
    except ImportError as e:
        print_warning(f"Could not import global settings: {e}")

    # Run imageposter directly (one-shot mode, botctl handles scheduling)
    print()
    print(f"{Colors.CYAN}Generating and posting image...{Colors.END}")

    # Set environment variables for this process
    for key, value in env.items():
        os.environ[key] = value

    try:
        # Import and generate/post image directly (bypassing delay)
        sys.path.insert(0, install_path)

        # Force reload of config module to pick up new env vars
        import importlib
        if 'config' in sys.modules:
            importlib.reload(sys.modules['config'])

        # Generate image directly
        from image_backend import generate_image_bytes
        from config import IMAGE_POSTER_PROMPT, IMAGE_POSTER_TEXT, IMAGE_POSTER_RANDOM_SCENES, MISSKEY_SERVER, PLEROMA_ENDPOINT
        import random
        
        prompt = IMAGE_POSTER_PROMPT
        if IMAGE_POSTER_RANDOM_SCENES:
            from random_scenes import RANDOM_SCENE_ELEMENTS
            random_scene = random.choice(RANDOM_SCENE_ELEMENTS)
            prompt = f"{IMAGE_POSTER_PROMPT}, {random_scene}"
            print(f"Using random scene: {random_scene}")
        
        print("Generating Image...................")
        image_bytes = generate_image_bytes(prompt)
        
        if image_bytes:
            print(f"Image Generation Complete ({len(image_bytes)} bytes)")
            # Post immediately without delay
            if MISSKEY_SERVER:
                from misskey import post_image_to_fediverse
                post_image_to_fediverse(IMAGE_POSTER_TEXT, image_bytes=image_bytes)
            elif PLEROMA_ENDPOINT:
                from pleroma import post_image_to_fediverse
                post_image_to_fediverse(IMAGE_POSTER_TEXT, image_bytes=image_bytes)
            else:
                print_error("Neither MISSKEY_SERVER nor PLEROMA_ENDPOINT is configured")
                return
            print_success(f"Image posted successfully by {selected}!")
        else:
            print_error("Image generation returned None")
    except KeyboardInterrupt:
        print_warning("Cancelled")
    except Exception as e:
        print_error(f"Error running image bot: {e}")
        import traceback
        traceback.print_exc()

    input(f"\n{Colors.DIM}Press Enter to continue...{Colors.END}")


def copy_bot_config(install_path):
    """Copy configuration from an existing bot to create a new bot"""
    print_section("Copy Bot Configuration")

    # Load current config
    image_bots, text_bots, image_defaults = load_bots_config(install_path)

    if image_bots is None:
        print_error("bots_config.py not found!")
        return

    # Get all bots
    all_bots = {}
    for name, config in image_bots.items():
        all_bots[name] = ('image', config)
    for name, config in text_bots.items():
        all_bots[name] = ('text', config)

    if not all_bots:
        print_warning("No bots configured to copy from")
        return

    # Show bot list
    print(f"\n{Colors.CYAN}Available bots to copy from:{Colors.END}")
    for name, (typ, config) in all_bots.items():
        host = config.get('host', '?')
        if typ == 'image':
            prompt_preview = config.get('prompt', '')[:40]
            if len(config.get('prompt', '')) > 40:
                prompt_preview += '...'
            print(f"  {Colors.GREEN}{name:<20}{Colors.END} {Colors.DIM}({typ}) [{host}] {prompt_preview}{Colors.END}")
        else:
            platform = config.get('platform', 'misskey')
            print(f"  {Colors.GREEN}{name:<20}{Colors.END} {Colors.DIM}({typ}) [{host}] {platform}{Colors.END}")
    print(f"  {Colors.DIM}back{' ' * 16}Cancel{Colors.END}")

    bot_names = list(all_bots.keys()) + ['back']
    source_name = prompt_autocomplete("Select bot to copy from (TAB to complete):", bot_names, show_list=False)

    if source_name in ('back', '_back'):
        return

    if source_name not in all_bots:
        print_error(f"Bot '{source_name}' not found")
        return

    bot_type, source_config = all_bots[source_name]

    # Show what will be copied
    print()
    print(f"{Colors.BOLD}Copying from {source_name} ({bot_type} bot):{Colors.END}")
    for key, value in source_config.items():
        if 'token' in key.lower() or 'pass' in key.lower():
            display = '********'
        elif isinstance(value, str) and len(value) > 50:
            display = value[:50] + '...'
        elif isinstance(value, list):
            display = ', '.join(value)
        else:
            display = value
        print(f"  {Colors.DIM}{key}{Colors.END}: {display}")
    print()

    # Get new bot name
    current_host = get_current_host()
    new_name = prompt("New bot name").lower().replace(' ', '_')

    # Check if name already exists
    if new_name in image_bots or new_name in text_bots:
        if not prompt_yes_no(f"Bot '{new_name}' already exists. Overwrite?", default=False):
            print_info("Cancelled")
            return

    # Deep copy the config
    import copy
    new_config = copy.deepcopy(source_config)

    # Ask if user wants to modify key settings
    print()
    print_info("You can now customize the new bot's settings.")
    print_info("Press Enter to keep the copied value.")
    print()

    if bot_type == 'image':
        # Image bot - ask about prompt and host
        current_prompt = new_config.get('prompt', '')
        print(f"{Colors.DIM}Current prompt: {current_prompt[:100]}{'...' if len(current_prompt) > 100 else ''}{Colors.END}")
        new_prompt = prompt("Image prompt", default=current_prompt, required=False, editable=True)
        if new_prompt:
            new_config['prompt'] = new_prompt

        new_host = prompt("Host", default=new_config.get('host', current_host))
        new_config['host'] = new_host

        # Add to image_bots
        image_bots[new_name] = new_config
        print_success(f"Created image bot '{new_name}' (copied from {source_name})")

    else:
        # Text bot - ask about key settings
        new_host = prompt("Host", default=new_config.get('host', current_host))
        new_config['host'] = new_host

        platform = new_config.get('platform', 'misskey')

        # Server and credentials
        if platform in ('misskey', 'pleroma'):
            new_server = prompt(f"{platform.title()} server", default=new_config.get('server', ''))
            if new_server:
                new_config['server'] = new_server

            new_username = prompt("Username", default=new_config.get('username', ''))
            if new_username:
                new_config['username'] = new_username

            if prompt_yes_no("Enter new access token?", default=False):
                new_token = prompt("Access token", secret=True)
                if new_token:
                    new_config['access_token'] = new_token

        elif platform == 'matrix':
            new_uid = prompt("Matrix user ID", default=new_config.get('matrix_user_id', ''), required=False)
            if new_uid:
                new_config['matrix_user_id'] = new_uid
            if prompt_yes_no("Enter new Matrix access token?", default=False):
                new_mtoken = prompt("Matrix access token", secret=True)
                if new_mtoken:
                    new_config['matrix_access_token'] = new_mtoken
            new_room = prompt("Default room ID", default=new_config.get('matrix_room_id', ''), required=False)
            if new_room:
                new_config['matrix_room_id'] = new_room
            new_admins = prompt("Admin Matrix IDs (comma-separated)", default=new_config.get('matrix_admins', ''), required=False)
            if new_admins:
                new_config['matrix_admins'] = new_admins
            if not new_config.get('matrix_admins'):
                print_warning("No admins set — the bot will decline ALL room invites, and admin commands (join/leave/block) won't work. Add at least one Matrix ID (e.g. @you:server).")
            new_shame = prompt("Shamebot rooms (comma-separated, for roasting matrix.org joiners)", default=','.join(new_config.get('shamebot_rooms', [])), required=False)
            if new_shame:
                new_config['shamebot_rooms'] = [r.strip() for r in new_shame.split(',') if r.strip()]
            elif 'shamebot_rooms' in new_config:
                del new_config['shamebot_rooms']

        # Prompt
        if 'prompt' in new_config:
            current_prompt = new_config.get('prompt', '')
            print(f"\n{Colors.DIM}Current prompt: {current_prompt[:100]}{'...' if len(current_prompt) > 100 else ''}{Colors.END}")
            if prompt_yes_no("Edit prompt?", default=False):
                new_prompt = prompt("Prompt", default=current_prompt, required=False, editable=True)
                if new_prompt:
                    new_config['prompt'] = new_prompt

        # Modes
        if 'modes' in new_config:
            current_modes = new_config.get('modes', [])
            print(f"\n{Colors.DIM}Current modes: {' '.join(current_modes)}{Colors.END}")
            if prompt_yes_no("Edit modes?", default=False):
                all_modes = ['--misskey', '--pleroma', '--matrix', '--blockbot', '--unfollowbot',
                           '--welcome', '--report', '--hashtagbot', '--nitter', '--ping', '--image']
                new_modes = list(current_modes)

                while True:
                    print(f"\n{Colors.BOLD}Current Modes:{Colors.END}")
                    for i, m in enumerate(all_modes, 1):
                        if m in new_modes:
                            status = f"{Colors.GREEN}[ON]{Colors.END}"
                        else:
                            status = f"{Colors.DIM}[off]{Colors.END}"
                        print(f"  {Colors.CYAN}{i:2}{Colors.END}. {m:<15} {status}")

                    print(f"\n  {Colors.DIM}Enter number to toggle, 'done' to save{Colors.END}")
                    choice = input(f"  {Colors.BOLD}> Toggle: {Colors.END}").strip().lower()

                    if choice == 'done':
                        break
                    elif choice.isdigit():
                        idx = int(choice) - 1
                        if 0 <= idx < len(all_modes):
                            mode = all_modes[idx]
                            if mode in new_modes:
                                new_modes.remove(mode)
                            else:
                                new_modes.append(mode)

                new_config['modes'] = new_modes

        # Add to text_bots
        text_bots[new_name] = new_config
        print_success(f"Created text bot '{new_name}' (copied from {source_name})")

    # Save config
    save_bots_config(install_path, image_bots, text_bots, image_defaults)

    print()
    print(f"{Colors.BOLD}Next steps:{Colors.END}")
    print(f"  1. Run 'Deploy' to generate service files")
    print(f"  2. Start the bot with: sudo systemctl start {new_name}")


def add_image_bot(install_path):
    """Add a new image bot"""
    print_section("Add Image Bot")

    current_host = get_current_host()

    name = prompt("Bot name (e.g., 'miku', 'saber')").lower().replace(' ', '_')
    image_prompt = prompt("Image generation prompt")
    host = prompt("Host to run on", default=current_host)

    # Load current config
    image_bots, text_bots, image_defaults = load_bots_config(install_path)

    if name in image_bots:
        if not prompt_yes_no(f"Bot '{name}' already exists. Overwrite?", default=False):
            print_info("Cancelled")
            return

    # TTS/Auto-narrate settings
    bot_config = {
        'prompt': image_prompt,
        'host': host,
    }

    if prompt_yes_no("Enable auto-narrate? (generates TTS video for all posts)", default=False):
        bot_config['auto_narrate'] = True
        print()
        print(f"{Colors.DIM}Popular voices:{Colors.END}")
        print(f"  en-US-AnaNeural       - Young female (cute, default)")
        print(f"  en-US-AriaNeural      - Adult female (natural)")
        print(f"  en-US-GuyNeural       - Adult male")
        print(f"  en-GB-SoniaNeural     - British female")
        print(f"  ja-JP-NanamiNeural    - Japanese female")
        print()
        voice = prompt("TTS voice", default="en-US-AnaNeural")
        if voice:
            bot_config['tts_voice'] = voice
        rate = prompt("TTS rate (e.g., +10%, -5%)", default="", required=False)
        if rate:
            bot_config['tts_rate'] = rate
        pitch = prompt("TTS pitch (e.g., +5Hz, -10Hz)", default="", required=False)
        if pitch:
            bot_config['tts_pitch'] = pitch

    image_bots[name] = bot_config

    save_bots_config(install_path, image_bots, text_bots, image_defaults)
    print_success(f"Added image bot '{name}'")

    # Image bots can't post without shared defaults (server/token); offer to set them.
    if not (image_defaults.get('server') and image_defaults.get('access_token')):
        print_warning("Image-bot defaults (server/token) aren't set — image bots can't post until they are.")
        if prompt_yes_no("Set image-bot defaults now?", default=True):
            edit_image_defaults(install_path)

    print()
    print(f"{Colors.BOLD}Image bot defaults (from IMAGE_BOT_DEFAULTS):{Colors.END}")
    print(f"  Server: {image_defaults.get('server', 'N/A')}")
    print(f"  Username: {image_defaults.get('username', 'N/A')}")
    print(f"  Text: {image_defaults.get('text', 'N/A')}")


def deploy_to_all_hosts(install_path):
    """Deploy to all hosts via SSH"""
    print_section("Deploy to All Hosts")

    # Get all unique hosts from config
    image_bots, text_bots, _ = load_bots_config(install_path)
    all_hosts = set()
    for config in image_bots.values():
        if config.get('host'):
            all_hosts.add(config['host'])
    for config in text_bots.values():
        if config.get('host'):
            all_hosts.add(config['host'])

    if not all_hosts:
        print_warning("No hosts found in config")
        return

    current_host = get_current_host()
    print_info(f"Found hosts: {', '.join(sorted(all_hosts))}")
    print_info(f"Current host: {current_host}")
    print()

    for host in sorted(all_hosts):
        if host == current_host:
            print(f"\n{Colors.CYAN}[{host}] (local){Colors.END}")
            botctl_path = os.path.join(install_path, 'botctl.py')
            subprocess.run([sys.executable, botctl_path, 'deploy'])
        else:
            print(f"\n{Colors.CYAN}[{host}] (remote via SSH){Colors.END}")
            # First push changes to git
            print_info("Pushing changes to git...")
            subprocess.run(['git', 'add', '-A'], cwd=install_path, capture_output=True)
            subprocess.run(['git', 'commit', '-m', 'Config update'], cwd=install_path, capture_output=True)
            subprocess.run(['git', 'push'], cwd=install_path, capture_output=True)

            # SSH to remote host and deploy
            ssh_cmd = f"cd ~/posterchan && git pull && ./botctl.py deploy"
            result = subprocess.run(
                ['ssh', host, ssh_cmd],
                capture_output=False
            )
            if result.returncode == 0:
                print_success(f"Deployed to {host}")
            else:
                print_error(f"Failed to deploy to {host}")

    print()
    print_success("Deploy to all hosts complete!")


def remove_bot_from_config(install_path, image_bots, text_bots):
    """Remove a bot from configuration"""
    print_section("Remove Bot")

    # Get all bots (no host filter)
    all_bots = {}
    for name, config in image_bots.items():
        all_bots[name] = ('image', config)
    for name, config in text_bots.items():
        all_bots[name] = ('text', config)

    if not all_bots:
        print_warning("No bots configured")
        return

    # Let user select bot with autocomplete
    bot_names = list(all_bots.keys()) + ['_back']

    # Show bot list with info
    print(f"\n{Colors.CYAN}Available bots:{Colors.END}")
    for name, (typ, config) in all_bots.items():
        host = config.get('host', '?')
        print(f"  {Colors.GREEN}{name:<20}{Colors.END} {Colors.DIM}({typ}) [{host}]{Colors.END}")
    print(f"  {Colors.DIM}_back{' ' * 15}← Back{Colors.END}")

    selected = prompt_autocomplete("Type bot name to remove (TAB to complete):", bot_names, show_list=False)

    if selected in ('_back', 'back'):
        return

    if selected not in all_bots:
        print_error(f"Bot '{selected}' not found")
        return

    bot_type, config = all_bots[selected]

    print()
    print(f"{Colors.RED}About to remove:{Colors.END}")
    print(f"  Name: {selected}")
    print(f"  Type: {bot_type}")
    print(f"  Host: {config.get('host', 'N/A')}")
    print()

    if not prompt_yes_no(f"Are you sure you want to remove '{selected}'?", default=False):
        print_info("Cancelled")
        return

    # Remove from config
    if bot_type == 'image':
        del image_bots[selected]
    else:
        del text_bots[selected]

    # Save
    _, _, image_defaults = load_bots_config(install_path)
    save_bots_config(install_path, image_bots, text_bots, image_defaults)
    print_success(f"Removed '{selected}' from config")


def edit_global_settings(install_path):
    """Guided editor for the global settings block in bots_config.py."""
    print_section("Global Settings")
    g = load_global_settings(install_path)

    fields = [
        ('AI_API_URL', 'AI API URL (OpenAI-compatible chat completions endpoint)', False),
        ('AI_API_KEY', 'AI API key', True),
        ('AI_MODEL', 'AI model name', False),
        ('USE_POSTERCHANAI', 'Use posterchanai image backend?', None),
        ('POSTERCHANAI_API_ENDPOINT', 'Posterchanai server URL', False),
        ('POSTERCHANAI_API_KEY', 'Posterchanai API key (blank = use user/pass)', True),
        ('POSTERCHANAI_USERNAME', 'Posterchanai username (fallback)', False),
        ('POSTERCHANAI_PASSWORD', 'Posterchanai password (fallback)', True),
        ('COMFYUI_API_ENDPOINT', 'ComfyUI endpoint (fallback backend)', False),
        ('STABLE_DIFFUSION_ENDPOINT', 'Stable Diffusion endpoint (optional)', False),
        ('SQL_USER', 'Database user', False),
        ('SQL_PASS', 'Database password', True),
        ('SQL_HOST', 'Database host (blank = Unix socket)', False),
        ('SEARXNG_URL', 'SearXNG search URL', False),
        ('TIMEZONE', 'Timezone (e.g. MST, UTC)', False),
    ]

    print(f"{Colors.BOLD}Current global settings:{Colors.END}")
    for key, _desc, secret in fields:
        val = g.get(key, '')
        shown = '********' if secret and val else val
        print(f"  {Colors.DIM}{key}{Colors.END} = {shown}")
    print()

    if not prompt_yes_no("Edit these settings?", default=False):
        return

    override = {}
    for key, desc, secret in fields:
        cur = g.get(key, '')
        if secret is None:  # boolean field
            override[key] = prompt_yes_no(desc, default=bool(cur))
        elif secret:
            new = prompt(f"{desc} (blank = keep current)", secret=True, required=False)
            if new:
                override[key] = new
        else:
            override[key] = prompt(desc, default=str(cur), required=False)

    if save_bots_config(install_path, *load_bots_config(install_path), globals_override=override):
        print_success("Global settings saved")


def add_text_bot(install_path):
    """Create a new text bot from scratch and write it to bots_config.py."""
    print_section("Add Text Bot")
    image_bots, text_bots, image_defaults = load_bots_config(install_path)
    image_bots = image_bots or {}
    text_bots = text_bots or {}
    image_defaults = image_defaults or {}

    name = prompt("Bot name (e.g. mybot)").lower().replace(' ', '-')
    if not name:
        print_error("A name is required.")
        return
    if name in text_bots or name in image_bots:
        if not prompt_yes_no(f"'{name}' already exists. Overwrite?", default=False):
            return

    platform = prompt_choice("Platform:", {
        'pleroma': 'Pleroma / Akkoma',
        'misskey': 'Misskey',
        'matrix': 'Matrix',
    })
    cfg = {'platform': platform}

    if platform in ('pleroma', 'misskey'):
        cfg['server'] = prompt(f"{platform.title()} server URL (e.g. https://instance.tld)")
        cfg['username'] = prompt("Bot username (without @)").lstrip('@')
        cfg['access_token'] = prompt("Access token", secret=True)

    if platform == 'matrix':
        cfg['matrix_server'] = prompt("Matrix homeserver URL (e.g. https://matrix.tld)")
        cfg['matrix_user_id'] = prompt("Bot user ID (e.g. @bot:matrix.tld)")
        cfg['matrix_access_token'] = prompt("Matrix access token", secret=True)
        room = prompt("Default room ID (optional, e.g. !id:server)", required=False)
        if room:
            cfg['matrix_room_id'] = room
        # Required: without an admin the bot declines all invites and has no controller
        # (join/leave/block are admin-only). This is the operator's own Matrix ID, not the
        # bot's. Loops until provided.
        print_warning("Set yourself as admin — your OWN Matrix ID (e.g. @you:matrix.tld), not the bot's. Without it the bot declines every invite and can't be controlled.")
        cfg['matrix_admins'] = prompt("Your admin Matrix ID(s) (comma-separated, e.g. @you:matrix.tld)", required=True)
        shame_rooms = prompt("Shamebot rooms (optional, comma-separated room IDs for roasting matrix.org joiners)", required=False)
        if shame_rooms:
            cfg['shamebot_rooms'] = [r.strip() for r in shame_rooms.split(',') if r.strip()]

    # Modes
    default_mode = '--matrix' if platform == 'matrix' else f'--{platform}'
    all_modes = ['--misskey', '--pleroma', '--matrix', '--nitter', '--blockbot',
                 '--unfollowbot', '--welcome', '--report', '--hashtagbot', '--image', '--ping']
    print_info(f"Available modes: {', '.join(all_modes)}")
    raw = prompt("Enter modes (space-separated)", default=default_mode, required=False)
    chosen = [m for m in raw.replace(',', ' ').split() if m in all_modes]
    cfg['modes'] = chosen or [default_mode]

    # Personality prompt for AI listener modes
    if any(m in ('--misskey', '--pleroma', '--matrix') for m in cfg['modes']):
        p = prompt("Bot prompt / personality (optional)", required=False, editable=True)
        if p:
            cfg['prompt'] = p

    # Database for DB-backed daemons
    if any(m in ('--blockbot', '--welcome', '--report', '--unfollowbot') for m in cfg['modes']):
        cfg['sql_database'] = prompt("SQL database name", default="pleroma")

    cfg['host'] = prompt("Host (machine that runs this bot)", default=get_current_host())

    text_bots[name] = cfg
    if save_bots_config(install_path, image_bots, text_bots, image_defaults):
        print_success(f"Added text bot '{name}'. Use Edit to fine-tune (prompts, TTS, Matrix, etc.).")


def edit_image_defaults(install_path):
    """Edit IMAGE_BOT_DEFAULTS — the server/credentials/caption shared by all image bots."""
    print_section("Image Bot Defaults")
    image_bots, text_bots, image_defaults = load_bots_config(install_path)
    image_bots = image_bots or {}
    text_bots = text_bots or {}
    image_defaults = image_defaults or {}

    fields = [
        ('server', 'Server URL image bots post to (e.g. https://instance.tld)', False),
        ('username', 'Bot username (without @)', False),
        ('access_token', 'Access token', True),
        ('text', 'Default caption/hashtags for image posts (e.g. #anime)', False),
    ]
    print(f"{Colors.BOLD}Current image-bot defaults:{Colors.END}")
    for k, _desc, secret in fields:
        v = image_defaults.get(k, '')
        print(f"  {Colors.DIM}{k}{Colors.END} = {'********' if secret and v else v}")
    print(f"  {Colors.DIM}random_scenes{Colors.END} = {image_defaults.get('random_scenes', False)}")
    print()

    if not prompt_yes_no("Edit image-bot defaults?", default=False):
        return

    for k, desc, secret in fields:
        cur = image_defaults.get(k, '')
        if secret:
            new = prompt(f"{desc} (blank = keep current)", secret=True, required=False)
            if new:
                image_defaults[k] = new
        else:
            image_defaults[k] = prompt(desc, default=str(cur), required=False)
    image_defaults['random_scenes'] = prompt_yes_no(
        "Add random scene elements to image prompts?",
        default=bool(image_defaults.get('random_scenes')))

    if save_bots_config(install_path, image_bots, text_bots, image_defaults):
        print_success("Image-bot defaults saved")


def main():
    clear_screen()
    print_banner()

    print_section("Welcome to Posterchan Installer")
    print_info("This installer will guide you through setting up a Posterchan bot.")
    print_info("Press Ctrl+C at any time to cancel.")
    print()

    try:
        # Get installation path
        install_path = get_install_path()
        print_success(f"Installation directory: {install_path}")

        # Get current user
        user = os.environ.get('USER', 'root')
        print_success(f"Running as user: {user}")
        print()

        # Check for bots_config.py first (centralized config)
        config_path = os.path.join(install_path, 'bots_config.py')
        has_central_config = os.path.exists(config_path)

        if not has_central_config:
            print_warning("No bots_config.py found in this directory.")
            if prompt_yes_no("Create a new configuration now?", default=True):
                save_bots_config(install_path, {}, {}, {})  # full global block, no bots yet
                print_success(f"Created {config_path}")
                print_info("First, set your global settings (AI endpoint, image backend, database):")
                edit_global_settings(install_path)
                if prompt_yes_no("Add your first text bot now?", default=True):
                    add_text_bot(install_path)
                has_central_config = True
            else:
                return

        if has_central_config:
            # Show centralized config menu in a loop
            while True:
                # Reload config each time in case it changed
                image_bots, text_bots, _ = load_bots_config(install_path)
                total_bots = len(image_bots or {}) + len(text_bots or {})

                print()
                print_info(f"Config: {total_bots} bots | Host: {get_current_host()}")

                action = prompt_choice(
                    "What would you like to do?",
                    {
                        'status': 'Bot status',
                        'service': 'Service control (start/stop)',
                        'manage': 'Manage bots',
                        'globals': 'Global settings (AI, image backend, database)',
                        'view': 'View current config',
                        'deploy': 'Deploy',
                        'update': 'Update code from git',
                        'edit_file': 'Edit bots_config.py directly',
                        'quit': 'Exit',
                    }
                )

                if action == 'quit':
                    print_info("Goodbye!")
                    return

                if action == 'status':
                    show_bot_status(install_path)
                    input(f"\n{Colors.DIM}Press Enter to continue...{Colors.END}")

                elif action == 'service':
                    service_action = prompt_choice(
                        "Service control:",
                        {
                            'start': 'Start service',
                            'stop': 'Stop service',
                            'restart': 'Restart service',
                            'back': '← Back',
                        }
                    )
                    if service_action != 'back':
                        try:
                            subprocess.run(['sudo', 'systemctl', service_action, 'posterchan'], check=True)
                            print_success(f"Service {service_action}ed successfully")
                        except subprocess.CalledProcessError:
                            print_error(f"Failed to {service_action} service")
                        input(f"\n{Colors.DIM}Press Enter to continue...{Colors.END}")

                elif action == 'manage':
                    manage_action = prompt_choice(
                        "Manage bots:",
                        {
                            'edit': 'Edit a bot',
                            'add': 'Add a bot',
                            'copy': 'Copy from existing bot',
                            'remove': 'Remove a bot',
                            'image_defaults': 'Image bot defaults (shared server/token/caption)',
                            'run_once': 'Run image bot once',
                            'back': '← Back',
                        }
                    )
                    if manage_action == 'edit':
                        edit_bot_config(install_path, image_bots, text_bots)
                    elif manage_action == 'add':
                        bot_kind = prompt_choice("What kind of bot?", {
                            'text': 'Text bot (responds to mentions / posts / daemons)',
                            'image': 'Image bot (scheduled AI images)',
                            'back': '← Back',
                        })
                        if bot_kind == 'text':
                            add_text_bot(install_path)
                        elif bot_kind == 'image':
                            add_image_bot(install_path)
                    elif manage_action == 'copy':
                        copy_bot_config(install_path)
                    elif manage_action == 'remove':
                        remove_bot_from_config(install_path, image_bots, text_bots)
                    elif manage_action == 'image_defaults':
                        edit_image_defaults(install_path)
                    elif manage_action == 'run_once':
                        run_image_bot_once(install_path)

                elif action == 'globals':
                    edit_global_settings(install_path)
                    input(f"\n{Colors.DIM}Press Enter to continue...{Colors.END}")

                elif action == 'view':
                    view_current_config(install_path)
                    input(f"\n{Colors.DIM}Press Enter to continue...{Colors.END}")

                elif action == 'deploy':
                    deploy_choice = prompt_choice(
                        "Deploy to:",
                        {
                            'local': f'This host only ({get_current_host()})',
                            'all': 'All configured hosts',
                            'back': '← Back',
                        }
                    )
                    if deploy_choice == 'local':
                        print_section(f"Deploying to {get_current_host()}")
                        botctl_path = os.path.join(install_path, 'botctl.py')
                        subprocess.run([sys.executable, botctl_path, 'deploy'])
                    elif deploy_choice == 'all':
                        deploy_to_all_hosts(install_path)

                elif action == 'update':
                    update_from_git(install_path)
                    setup_venv(install_path)
                    print_success("Update complete!")

                elif action == 'edit_file':
                    config_file = os.path.join(install_path, 'bots_config.py')
                    editor = os.environ.get('EDITOR', 'nano')
                    print_info(f"Opening {config_file} with {editor}...")
                    subprocess.run([editor, config_file])

            return  # Won't reach here but for clarity

    except KeyboardInterrupt:
        print()
        print()
        print_info("Installation cancelled by user.")
        sys.exit(0)
    except Exception as e:
        print()
        print_error(f"Installation failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
