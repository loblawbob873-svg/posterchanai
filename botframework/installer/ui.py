"""
Installer UI utilities.
Terminal colors, prompts, banners, and user interaction helpers.
"""

import os
import sys
import getpass

from core import Colors


def clear_screen():
    """Clear the terminal screen."""
    os.system('clear' if os.name != 'nt' else 'cls')


def print_banner():
    """Print the Posterchan ASCII art banner."""
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
    """Print a section header."""
    width = 50
    print(f"\n{Colors.BLUE}{Colors.BOLD}{'═' * width}{Colors.END}")
    print(f"{Colors.BLUE}{Colors.BOLD}  {title}{Colors.END}")
    print(f"{Colors.BLUE}{Colors.BOLD}{'═' * width}{Colors.END}\n")


def print_success(msg):
    """Print a success message."""
    print(f"{Colors.GREEN}✓ {msg}{Colors.END}")


def print_error(msg):
    """Print an error message."""
    print(f"{Colors.RED}✗ {msg}{Colors.END}")


def print_info(msg):
    """Print an info message."""
    print(f"{Colors.CYAN}ℹ {msg}{Colors.END}")


def print_warning(msg):
    """Print a warning message."""
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.END}")


def readline_safe_color(color_code):
    """Wrap ANSI color code with readline escape markers to fix cursor positioning.

    Readline needs \\001 and \\002 around non-printing characters so it can
    correctly calculate the visible prompt length for cursor positioning.
    """
    return f"\001{color_code}\002"


def prompt(question, default=None, secret=False, required=True, editable=False):
    """Prompt user for input with optional default value.

    If editable=True and default is provided, the default will be pre-filled
    in the input line for easy editing (requires readline support).
    """
    prompt_text = f"{Colors.BOLD}{question}{Colors.END}: "
    rl_prompt_text = f"{readline_safe_color(Colors.BOLD)}{question}{readline_safe_color(Colors.END)}: "

    while True:
        if secret:
            value = getpass.getpass(prompt_text)
        else:
            if default and editable:
                try:
                    import readline

                    try:
                        readline.parse_and_bind(r'"\e[H": beginning-of-line')
                        readline.parse_and_bind(r'"\e[F": end-of-line')
                        readline.parse_and_bind(r'"\e[1~": beginning-of-line')
                        readline.parse_and_bind(r'"\e[4~": end-of-line')
                        readline.parse_and_bind(r'"\C-a": beginning-of-line')
                        readline.parse_and_bind(r'"\C-e": end-of-line')
                    except Exception:
                        pass

                    def prefill():
                        readline.insert_text(str(default))
                        readline.redisplay()

                    readline.set_pre_input_hook(prefill)
                    print(f"  {Colors.DIM}(Use Ctrl+A for start, Ctrl+E for end, arrows to navigate){Colors.END}")
                    value = input(rl_prompt_text)
                    readline.set_pre_input_hook(None)
                except ImportError:
                    if len(str(default)) > 80:
                        print(f"  {Colors.DIM}(Current: {default[:80]}...){Colors.END}")
                    else:
                        print(f"  {Colors.DIM}(Current: {default}){Colors.END}")
                    value = input(prompt_text)
            elif default:
                truncated = str(default)[:50]
                suffix = '...' if len(str(default)) > 50 else ''
                prompt_text = f"{Colors.BOLD}{question}{Colors.END} [{Colors.DIM}{truncated}{suffix}{Colors.END}]: "
                value = input(prompt_text)
            else:
                value = input(prompt_text)

        if not value and default:
            return default
        if value or not required:
            return value
        print_error("This field is required.")


def prompt_autocomplete(question, options, show_list=True):
    """Prompt with tab autocomplete for selecting from a list of options."""
    import readline

    matches = []

    def completer(text, state):
        if state == 0:
            matches.clear()
            text_lower = text.lower()
            for opt in options:
                if opt.lower().startswith(text_lower):
                    matches.append(opt)
        if state < len(matches):
            return matches[state]
        return None

    old_completer = readline.get_completer()
    old_delims = readline.get_completer_delims()

    readline.set_completer(completer)
    readline.set_completer_delims('')
    readline.parse_and_bind('tab: complete')

    try:
        print()
        print(f"  {Colors.CYAN}{'─' * 48}{Colors.END}")
        print(f"  {Colors.BOLD}{question}{Colors.END}")
        print(f"  {Colors.CYAN}{'─' * 48}{Colors.END}")

        if show_list and options:
            print()
            opt_list = list(options)
            col_width = max(len(o) for o in opt_list) + 2 if opt_list else 10
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

            if value in options:
                return value

            for opt in options:
                if opt.lower() == value.lower():
                    return opt

            partial_matches = [o for o in options if o.lower().startswith(value.lower())]
            if len(partial_matches) == 1:
                return partial_matches[0]
            elif len(partial_matches) > 1:
                print(f"  {Colors.YELLOW}Multiple matches: {', '.join(partial_matches[:5])}{Colors.END}")
            else:
                print(f"  {Colors.RED}No match found. Try again or press TAB.{Colors.END}")

    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)


def prompt_choice(question, options, allow_multiple=False, icons=None):
    """Prompt user to select from options."""
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
    """Prompt user for yes/no."""
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
