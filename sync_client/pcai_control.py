#!/usr/bin/env python3
"""
╔═══════════════════════════════════════════════════════════════════════════════╗
║  POSTERCHAN.AI CONTROL CENTER - Cyberpunk Edition                            ║
║  >>> Neural link established. Welcome to the grid, netrunner. <<<            ║
╚═══════════════════════════════════════════════════════════════════════════════╝
"""

import os
import sys
import json
import time
import subprocess
import threading
import select
from pathlib import Path
from datetime import datetime, timedelta
import shutil

# Check for rich library
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.live import Live
    from rich.layout import Layout
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich.style import Style
    from rich.align import Align
    from rich import box
except ImportError:
    print("\n[!] Installing required packages...")
    subprocess.run([sys.executable, "-m", "pip", "install", "rich", "-q"])
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.live import Live
    from rich.layout import Layout
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich.style import Style
    from rich.align import Align
    from rich import box

# Cyberpunk color scheme
NEON_CYAN = "bright_cyan"
NEON_MAGENTA = "bright_magenta"
NEON_GREEN = "bright_green"
NEON_YELLOW = "bright_yellow"
NEON_RED = "bright_red"
NEON_BLUE = "bright_blue"
DARK_BG = "on grey11"

console = Console()

# ASCII Art Banner - compact version for small terminals
BANNER = """[bright_cyan]╔═══════════════════════════════════════════════════════╗
║[/bright_cyan] [bold bright_magenta]◢◤[/bold bright_magenta] [bold bright_cyan]POSTERCHAN.AI CONTROL CENTER[/bold bright_cyan] [bold bright_magenta]◢◤[/bold bright_magenta] [dim]Neural link active[/dim] [bright_cyan]║
╚═══════════════════════════════════════════════════════╝[/bright_cyan]"""

SMALL_BANNER = """[bright_cyan]◢◤ PCAI CONTROL ◢◤[/bright_cyan] [dim]Online[/dim]"""


class ServiceManager:
    """Manage PosterchanAI services"""

    SERVICES = {
        'sync': {
            'name': 'PosterchanAI Sync Daemon',
            'unit': 'posterchanai-sync.service',
            'user': True,
            'icon': '🔄',
        },
        'webdav': {
            'name': 'PosterchanAI WebDAV Server',
            'unit': 'posterchanai-ipex.service',
            'user': False,
            'icon': '🌐',
        },
    }

    def __init__(self):
        self.config_path = Path.home() / '.config' / 'posterchanai-sync' / 'config.json'
        self.config = self._load_config()

    def _load_config(self) -> dict:
        """Load configuration file"""
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text())
            except:
                return {}
        return {}

    def _save_config(self):
        """Save configuration file"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.config, indent=2))

    def _run_systemctl(self, action: str, service: str, user: bool = True) -> tuple:
        """Run systemctl command"""
        cmd = ['systemctl']
        if user:
            cmd.append('--user')
        cmd.extend([action, service])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return result.returncode == 0, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)

    def get_service_status(self, service_key: str) -> dict:
        """Get detailed service status"""
        if service_key not in self.SERVICES:
            return {'status': 'unknown', 'error': 'Invalid service'}

        svc = self.SERVICES[service_key]
        cmd = ['systemctl']
        if svc['user']:
            cmd.append('--user')
        cmd.extend(['status', svc['unit']])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            output = result.stdout + result.stderr

            # Parse status
            status = 'unknown'
            if 'Active: active (running)' in output:
                status = 'running'
            elif 'Active: inactive' in output or 'Active: dead' in output:
                status = 'stopped'
            elif 'Active: failed' in output:
                status = 'failed'
            elif 'could not be found' in output.lower():
                status = 'not_installed'

            # Parse memory/CPU if available
            memory = None
            cpu = None
            uptime = None

            for line in output.split('\n'):
                if 'Memory:' in line:
                    memory = line.split('Memory:')[1].strip().split()[0]
                if 'CPU:' in line:
                    cpu = line.split('CPU:')[1].strip()
                if 'Active: active' in line and 'since' in line:
                    try:
                        since_part = line.split('since')[1].strip()
                        # Extract the time part
                        uptime = since_part.split(';')[1].strip() if ';' in since_part else None
                    except:
                        pass

            return {
                'status': status,
                'memory': memory,
                'cpu': cpu,
                'uptime': uptime,
                'raw': output
            }
        except Exception as e:
            return {'status': 'error', 'error': str(e)}

    def start_service(self, service_key: str) -> tuple:
        """Start a service"""
        svc = self.SERVICES.get(service_key)
        if not svc:
            return False, "Invalid service"
        return self._run_systemctl('start', svc['unit'], svc['user'])

    def stop_service(self, service_key: str) -> tuple:
        """Stop a service"""
        svc = self.SERVICES.get(service_key)
        if not svc:
            return False, "Invalid service"
        return self._run_systemctl('stop', svc['unit'], svc['user'])

    def restart_service(self, service_key: str) -> tuple:
        """Restart a service"""
        svc = self.SERVICES.get(service_key)
        if not svc:
            return False, "Invalid service"
        return self._run_systemctl('restart', svc['unit'], svc['user'])

    def get_logs(self, service_key: str, lines: int = 50) -> str:
        """Get service logs"""
        svc = self.SERVICES.get(service_key)
        if not svc:
            return "Invalid service"

        cmd = ['journalctl']
        if svc['user']:
            cmd.append('--user')
        cmd.extend(['-u', svc['unit'], '-n', str(lines), '--no-pager', '-o', 'short-iso'])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            return result.stdout or result.stderr or "No logs available"
        except Exception as e:
            return f"Error getting logs: {e}"

    def get_sync_stats(self) -> dict:
        """Get sync statistics from config and cache"""
        stats = {
            'mount_point': self.config.get('mount_point', 'Not configured'),
            'webdav_url': self.config.get('webdav_url', 'Not configured'),
            'username': self.config.get('username', 'Not configured'),
            'sync_interval': self.config.get('sync_interval', 30),
            'max_workers': self.config.get('max_workers', 8),
            'upload_chunk_size_mb': self.config.get('upload_chunk_size_mb', 10),
            'excluded_folders': self.config.get('excluded_folders', []),
        }

        # Check mount point status
        mount_path = Path(self.config.get('mount_point', ''))
        if mount_path.exists():
            try:
                # Calculate actual sync folder usage (not partition usage)
                folder_size = 0
                file_count = 0
                for f in mount_path.rglob('*'):
                    if f.is_file():
                        try:
                            folder_size += f.stat().st_size
                            file_count += 1
                        except (OSError, PermissionError):
                            pass
                stats['folder_size'] = folder_size
                stats['file_count'] = file_count

                # Also get partition info for context
                total, used, free = shutil.disk_usage(mount_path)
                stats['disk_total'] = total
                stats['disk_free'] = free
            except:
                pass

        return stats


class CyberpunkUI:
    """Cyberpunk-styled terminal UI"""

    def __init__(self):
        self.manager = ServiceManager()
        self.running = True
        self.current_view = 'dashboard'
        self.selected_service = None

    def clear_screen(self):
        """Clear terminal screen"""
        console.clear()

    def print_banner(self, small: bool = False):
        """Print the banner"""
        if small:
            console.print(SMALL_BANNER)
        else:
            console.print(BANNER)

    def create_status_panel(self) -> Panel:
        """Create compact service status panel with disk usage"""
        from rich.console import Group

        # Services table - compact
        table = Table(
            show_header=True,
            header_style=f"bold {NEON_CYAN}",
            box=box.SIMPLE,
            expand=True,
            padding=(0, 1)
        )

        table.add_column("SERVICE", style=NEON_CYAN)
        table.add_column("STATUS", justify="center")
        table.add_column("MEM", justify="right", style=NEON_YELLOW, width=6)

        for key, svc in self.manager.SERVICES.items():
            status_info = self.manager.get_service_status(key)
            status = status_info.get('status', 'unknown')

            # Compact status indicator
            if status == 'running':
                status_text = Text("● ON", style=f"bold {NEON_GREEN}")
            elif status == 'stopped':
                status_text = Text("○ OFF", style="dim white")
            elif status == 'failed':
                status_text = Text("✖ FAIL", style=f"bold {NEON_RED}")
            else:
                status_text = Text("? N/A", style="dim")

            memory = status_info.get('memory', '-')
            # Shorten service name
            short_name = "Sync" if key == "sync" else "WebDAV"

            table.add_row(short_name, status_text, memory or '-')

        # Disk usage - compact single line
        stats = self.manager.get_sync_stats()
        disk_text = Text()

        if stats.get('folder_size') is not None:
            folder_bytes = stats['folder_size']
            if folder_bytes >= 1024**3:
                folder_str = f"{folder_bytes / (1024**3):.2f}G"
            elif folder_bytes >= 1024**2:
                folder_str = f"{folder_bytes / (1024**2):.0f}M"
            else:
                folder_str = f"{folder_bytes / 1024:.0f}K"

            disk_text.append(f"📁 ", style=NEON_MAGENTA)
            disk_text.append(f"{folder_str}", style=NEON_YELLOW)
            disk_text.append(f" ({stats.get('file_count', 0):,} files)", style="dim")

            if stats.get('disk_free'):
                free_gb = stats['disk_free'] / (1024**3)
                disk_text.append(f"  💾 ", style=NEON_MAGENTA)
                disk_text.append(f"{free_gb:.0f}G free", style=NEON_GREEN)
        else:
            disk_text.append(f"📁 N/A", style="dim")

        return Panel(
            Group(table, disk_text),
            title=f"[{NEON_CYAN}]STATUS[/]",
            border_style=NEON_MAGENTA,
            box=box.ROUNDED,
            padding=(0, 1)
        )

    def create_config_panel(self) -> Panel:
        """Create configuration panel"""
        stats = self.manager.get_sync_stats()

        config_text = Text()
        config_text.append("╔══ SYNC CONFIGURATION ══╗\n", style=f"bold {NEON_CYAN}")
        config_text.append(f"  ◈ Mount Point: ", style=NEON_MAGENTA)
        config_text.append(f"{stats['mount_point']}\n", style="white")
        config_text.append(f"  ◈ WebDAV URL: ", style=NEON_MAGENTA)
        config_text.append(f"{stats['webdav_url']}\n", style="white")
        config_text.append(f"  ◈ Username: ", style=NEON_MAGENTA)
        config_text.append(f"{stats['username']}\n", style="white")
        config_text.append(f"  ◈ Sync Interval: ", style=NEON_MAGENTA)
        config_text.append(f"{stats['sync_interval']}s\n", style=NEON_GREEN)
        config_text.append(f"  ◈ Workers: ", style=NEON_MAGENTA)
        config_text.append(f"{stats['max_workers']}\n", style=NEON_GREEN)
        config_text.append(f"  ◈ Chunk Size: ", style=NEON_MAGENTA)
        config_text.append(f"{stats['upload_chunk_size_mb']}MB\n", style=NEON_GREEN)

        # Always show sync folder section
        config_text.append("\n╔══ SYNC FOLDER ══╗\n", style=f"bold {NEON_CYAN}")
        if stats.get('folder_size') is not None:
            folder_bytes = stats['folder_size']
            # Format folder size nicely
            if folder_bytes >= 1024**3:
                folder_str = f"{folder_bytes / (1024**3):.2f} GB"
            elif folder_bytes >= 1024**2:
                folder_str = f"{folder_bytes / (1024**2):.1f} MB"
            elif folder_bytes >= 1024:
                folder_str = f"{folder_bytes / 1024:.1f} KB"
            else:
                folder_str = f"{folder_bytes} B"
            config_text.append(f"  ◈ Size: ", style=NEON_MAGENTA)
            config_text.append(f"{folder_str}\n", style=NEON_YELLOW)
            config_text.append(f"  ◈ Files: ", style=NEON_MAGENTA)
            config_text.append(f"{stats.get('file_count', 0):,}\n", style="white")
        else:
            config_text.append(f"  ◈ Size: ", style=NEON_MAGENTA)
            config_text.append(f"N/A (folder not found)\n", style="dim")

        if stats.get('disk_total'):
            free_gb = stats['disk_free'] / (1024**3)
            config_text.append(f"  ◈ Disk Free: ", style=NEON_MAGENTA)
            config_text.append(f"{free_gb:.1f} GB\n", style=NEON_GREEN)

        if stats.get('excluded_folders'):
            config_text.append("\n╔══ EXCLUDED ══╗\n", style=f"bold {NEON_CYAN}")
            for folder in stats['excluded_folders'][:5]:
                config_text.append(f"  ⊘ {folder}\n", style="dim")
            if len(stats['excluded_folders']) > 5:
                config_text.append(f"  ... and {len(stats['excluded_folders']) - 5} more\n", style="dim")

        return Panel(
            config_text,
            title=f"[bold {NEON_CYAN}]◢◤ CONFIGURATION ◢◤[/]",
            border_style=NEON_BLUE,
            box=box.DOUBLE
        )

    def create_menu_panel(self) -> Panel:
        """Create compact menu panel - 2 columns"""
        # Two-column layout for compactness
        table = Table(show_header=False, box=None, expand=True, padding=(0, 2))
        table.add_column("col1")
        table.add_column("col2")

        left_cmds = [
            ("[1]", "Start Sync", NEON_GREEN),
            ("[2]", "Stop Sync", NEON_RED),
            ("[3]", "Restart Sync", NEON_YELLOW),
            ("[4]", "Sync Logs", NEON_BLUE),
            ("[e]", "Exclusions", NEON_YELLOW),
            ("[q]", "Quit", "dim"),
        ]
        right_cmds = [
            ("[5]", "Start WebDAV", NEON_GREEN),
            ("[6]", "Stop WebDAV", NEON_RED),
            ("[7]", "Restart WebDAV", NEON_YELLOW),
            ("[8]", "WebDAV Logs", NEON_BLUE),
            ("[c]", "Config", NEON_MAGENTA),
            ("[r]", "Refresh", NEON_CYAN),
        ]

        for (lk, ld, lc), (rk, rd, rc) in zip(left_cmds, right_cmds):
            left_text = Text()
            left_text.append(lk, style=f"bold {NEON_CYAN}")
            left_text.append(f" {ld}", style=lc)

            right_text = Text()
            right_text.append(rk, style=f"bold {NEON_CYAN}")
            right_text.append(f" {rd}", style=rc)

            table.add_row(left_text, right_text)

        return Panel(
            table,
            title=f"[{NEON_CYAN}]COMMANDS[/]",
            border_style=NEON_MAGENTA,
            box=box.ROUNDED,
            padding=(0, 1)
        )

    def create_time_panel(self) -> Panel:
        """Create time/date panel"""
        now = datetime.now()
        time_str = now.strftime("%H:%M:%S")
        date_str = now.strftime("%Y-%m-%d")

        time_text = Text()
        time_text.append("◢◤ ", style=NEON_MAGENTA)
        time_text.append(time_str, style=f"bold {NEON_CYAN}")
        time_text.append(" ◢◤ ", style=NEON_MAGENTA)
        time_text.append(date_str, style=NEON_BLUE)
        time_text.append(" ◢◤", style=NEON_MAGENTA)

        return Panel(
            Align.center(time_text),
            border_style=NEON_MAGENTA,
            box=box.ROUNDED
        )

    def show_logs(self, service_key: str, lines: int = 50):
        """Display logs for a service"""
        self.clear_screen()
        self.print_banner(small=True)

        svc = self.manager.SERVICES.get(service_key, {})
        console.print(f"\n[bold {NEON_CYAN}]◢◤ LOGS: {svc.get('name', service_key)} ◢◤[/]\n")

        logs = self.manager.get_logs(service_key, lines)

        # Colorize log levels
        for line in logs.split('\n'):
            if 'ERROR' in line or 'error' in line:
                console.print(f"  [bold {NEON_RED}]{line}[/]")
            elif 'WARNING' in line or 'warning' in line:
                console.print(f"  [{NEON_YELLOW}]{line}[/]")
            elif 'INFO' in line:
                console.print(f"  [{NEON_GREEN}]{line}[/]")
            elif 'DEBUG' in line:
                console.print(f"  [dim]{line}[/]")
            else:
                console.print(f"  {line}")

        console.print(f"\n[dim]Press Enter to return...[/]")
        input()

    def show_dashboard(self):
        """Show the main dashboard - compact vertical layout"""
        self.clear_screen()

        # Check terminal width for responsive layout
        term_width = shutil.get_terminal_size().columns

        if term_width < 60:
            # Very small terminal - minimal output
            self.print_banner(small=True)
            console.print(self.create_status_panel())
            console.print(self.create_menu_panel())
            return

        self.print_banner(small=(term_width < 80))

        # Simple vertical stack - no complex layouts
        console.print(self.create_status_panel())
        console.print(self.create_menu_panel())

    def execute_command(self, cmd: str) -> bool:
        """Execute a command and return whether to continue"""
        cmd = cmd.lower().strip()

        if cmd == 'q':
            return False
        elif cmd == 'r':
            return True
        elif cmd == '1':
            self.service_action('sync', 'start')
        elif cmd == '2':
            self.service_action('sync', 'stop')
        elif cmd == '3':
            self.service_action('sync', 'restart')
        elif cmd == '4':
            self.show_logs('sync')
        elif cmd == '5':
            self.service_action('webdav', 'start')
        elif cmd == '6':
            self.service_action('webdav', 'stop')
        elif cmd == '7':
            self.service_action('webdav', 'restart')
        elif cmd == '8':
            self.show_logs('webdav')
        elif cmd == 'e':
            self.manage_exclusions()
        elif cmd == 'c':
            self.edit_config()

        return True

    def service_action(self, service: str, action: str):
        """Perform a service action with visual feedback"""
        svc = self.manager.SERVICES.get(service, {})
        svc_name = svc.get('name', service)

        console.print(f"\n[{NEON_CYAN}]◢◤ {action.upper()}ING {svc_name}...[/]")

        with Progress(
            SpinnerColumn(style=NEON_MAGENTA),
            TextColumn(f"[{NEON_CYAN}]Executing neural command...[/]"),
            transient=True
        ) as progress:
            progress.add_task("", total=None)

            if action == 'start':
                success, msg = self.manager.start_service(service)
            elif action == 'stop':
                success, msg = self.manager.stop_service(service)
            elif action == 'restart':
                success, msg = self.manager.restart_service(service)
            else:
                success, msg = False, "Unknown action"

            time.sleep(0.5)  # Small delay for visual effect

        if success:
            console.print(f"[bold {NEON_GREEN}]✓ Success![/] {svc_name} {action}ed.")
        else:
            console.print(f"[bold {NEON_RED}]✖ Failed![/] {msg}")

        time.sleep(1.5)

    def edit_config(self):
        """Open config in editor"""
        config_path = self.manager.config_path
        editor = os.environ.get('EDITOR', 'nano')

        console.print(f"\n[{NEON_CYAN}]◢◤ Opening configuration in {editor}...[/]")
        time.sleep(0.5)

        try:
            subprocess.run([editor, str(config_path)])
            self.manager.config = self.manager._load_config()
            console.print(f"[{NEON_GREEN}]✓ Configuration reloaded[/]")
        except Exception as e:
            console.print(f"[{NEON_RED}]✖ Error: {e}[/]")

        time.sleep(1)

    def manage_exclusions(self):
        """Interactive folder exclusion manager"""
        self.clear_screen()
        self.print_banner(small=True)

        mount_point = Path(self.manager.config.get('mount_point', ''))
        if not mount_point.exists():
            console.print(f"\n[{NEON_RED}]✖ Mount point not found: {mount_point}[/]")
            console.print(f"[dim]Press Enter to return...[/]")
            input()
            return

        # Get current exclusions
        current_exclusions = set(self.manager.config.get('excluded_folders', []))

        # Get all top-level directories
        try:
            all_dirs = sorted([
                d.name for d in mount_point.iterdir()
                if d.is_dir() and not d.name.startswith('.')
            ])
        except Exception as e:
            console.print(f"\n[{NEON_RED}]✖ Error reading directories: {e}[/]")
            console.print(f"[dim]Press Enter to return...[/]")
            input()
            return

        if not all_dirs:
            console.print(f"\n[{NEON_YELLOW}]No directories found in {mount_point}[/]")
            console.print(f"[dim]Press Enter to return...[/]")
            input()
            return

        while True:
            self.clear_screen()
            self.print_banner(small=True)

            console.print(f"\n[bold {NEON_CYAN}]◢◤ FOLDER EXCLUSION MANAGER ◢◤[/]\n")
            console.print(f"[dim]Mount point: {mount_point}[/]\n")

            # Create table
            table = Table(
                show_header=True,
                header_style=f"bold {NEON_CYAN}",
                box=box.ROUNDED,
                border_style=NEON_MAGENTA
            )
            table.add_column("#", style="bold", width=4)
            table.add_column("STATUS", justify="center", width=10)
            table.add_column("FOLDER", style="white")
            table.add_column("SIZE", justify="right", style=NEON_YELLOW)

            for i, dir_name in enumerate(all_dirs, 1):
                is_excluded = dir_name in current_exclusions

                if is_excluded:
                    status = Text("⊘ SKIP", style=f"bold {NEON_RED}")
                else:
                    status = Text("✓ SYNC", style=f"bold {NEON_GREEN}")

                # Get directory size (quick estimate)
                dir_path = mount_point / dir_name
                try:
                    # Count files and estimate size
                    size_bytes = sum(f.stat().st_size for f in dir_path.rglob('*') if f.is_file())
                    if size_bytes > 1024**3:
                        size_str = f"{size_bytes / 1024**3:.1f} GB"
                    elif size_bytes > 1024**2:
                        size_str = f"{size_bytes / 1024**2:.1f} MB"
                    elif size_bytes > 1024:
                        size_str = f"{size_bytes / 1024:.1f} KB"
                    else:
                        size_str = f"{size_bytes} B"
                except:
                    size_str = "?"

                table.add_row(str(i), status, dir_name, size_str)

            console.print(table)

            console.print(f"\n[{NEON_MAGENTA}]╔══ COMMANDS ══╗[/]")
            console.print(f"[{NEON_CYAN}]  Enter number to toggle exclusion[/]")
            console.print(f"[{NEON_CYAN}]  [a] Exclude ALL    [n] Exclude NONE[/]")
            console.print(f"[{NEON_CYAN}]  [s] Save & Exit    [q] Cancel[/]")

            console.print(f"\n[{NEON_MAGENTA}]◢◤[/] [{NEON_CYAN}]Command[/]: ", end="")

            try:
                cmd = input().strip().lower()
            except (KeyboardInterrupt, EOFError):
                return

            if cmd == 'q':
                return
            elif cmd == 's':
                # Save changes
                self.manager.config['excluded_folders'] = sorted(list(current_exclusions))
                self.manager._save_config()
                console.print(f"\n[bold {NEON_GREEN}]✓ Exclusions saved![/]")

                # Ask to restart sync service
                console.print(f"\n[{NEON_CYAN}]Restart sync service to apply changes? [Y/n]: [/]", end="")
                try:
                    restart = input().strip().lower()
                    if restart != 'n':
                        self.service_action('sync', 'restart')
                except:
                    pass
                return
            elif cmd == 'a':
                # Exclude all
                current_exclusions = set(all_dirs)
            elif cmd == 'n':
                # Exclude none
                current_exclusions = set()
            elif cmd.isdigit():
                idx = int(cmd) - 1
                if 0 <= idx < len(all_dirs):
                    dir_name = all_dirs[idx]
                    if dir_name in current_exclusions:
                        current_exclusions.remove(dir_name)
                    else:
                        current_exclusions.add(dir_name)

    def get_input_with_timeout(self, timeout: float = 30.0) -> str:
        """Get user input with timeout, returns empty string on timeout"""
        # Use select for non-blocking input on Unix systems
        try:
            rlist, _, _ = select.select([sys.stdin], [], [], timeout)
            if rlist:
                return sys.stdin.readline().strip()
            return ''  # Timeout - return empty to trigger refresh
        except (select.error, OSError):
            # Fallback for systems where select doesn't work on stdin
            return input()

    def run(self):
        """Main run loop with auto-refresh every 30 seconds"""
        refresh_interval = 30  # seconds
        last_refresh = time.time()

        try:
            while self.running:
                self.show_dashboard()

                # Calculate time until next refresh
                elapsed = time.time() - last_refresh
                time_until_refresh = max(0.1, refresh_interval - elapsed)

                console.print(f"\n[{NEON_MAGENTA}]◢◤[/] [{NEON_CYAN}]Enter command[/] [dim](auto-refresh in {int(time_until_refresh)}s, 'q' to exit)[/]: ", end="")
                sys.stdout.flush()

                try:
                    cmd = self.get_input_with_timeout(time_until_refresh)

                    if cmd:  # User entered something
                        self.running = self.execute_command(cmd)

                    # Update last refresh time
                    last_refresh = time.time()

                except KeyboardInterrupt:
                    self.running = False
                except EOFError:
                    self.running = False

        finally:
            self.shutdown()

    def shutdown(self):
        """Graceful shutdown"""
        self.clear_screen()
        console.print(f"""
[{NEON_MAGENTA}]
╔═══════════════════════════════════════════════════════════════════════════════╗
║                                                                               ║
║  [bold {NEON_CYAN}]◢◤ NEURAL LINK DISCONNECTED ◢◤[/{NEON_CYAN}]                                          ║
║                                                                               ║
║  [dim]Session terminated. Stay safe in the net, netrunner.[/dim]                        ║
║                                                                               ║
╚═══════════════════════════════════════════════════════════════════════════════╝
[/{NEON_MAGENTA}]
""")


def main():
    """Entry point"""
    # Check terminal size
    term_size = shutil.get_terminal_size()
    if term_size.columns < 80:
        console.print(f"[{NEON_YELLOW}]Warning: Terminal width ({term_size.columns}) is less than recommended (80)[/]")

    ui = CyberpunkUI()
    ui.run()


if __name__ == '__main__':
    main()
