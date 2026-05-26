from commands import get_cpu_usage
from commands import get_block_count
import shutil

class Style:
    BOLD = '\033[1m'
    DIM = '\033[2m'
    RESET = '\033[0m'
    RED = '\033[38;5;196m'
    GREEN = '\033[38;5;82m'
    YELLOW = '\033[38;5;220m'
    BLUE = '\033[38;5;75m'
    MAGENTA = '\033[38;5;141m'
    CYAN = '\033[38;5;51m'
    GRAY = '\033[38;5;244m'
    WHITE = '\033[38;5;255m'
    BG_DARK = '\033[48;5;236m'
    BG_HEADER = '\033[48;5;24m'
    ORANGE = '\033[38;5;208m'
    NEON_CYAN = '\033[36m'
    NEON_MAGENTA = '\033[35m'

def c(text, style):
    return f"{style}{text}{Style.RESET}"

def buildCLI(activity, timestamp):
    cols = shutil.get_terminal_size((80, 24)).columns

    blocked = []
    queries = []
    counters = []

    for line in activity:
        if "🚨 Blocked IP:" in line or "🚨 Blocked Subnet:" in line:
            blocked.append(line)
        elif "📍" in line:
            counters.append(line)
        elif "🕵️" in line:
            queries.append(line)

    cpu = get_cpu_usage().replace("💻", "").replace("😀", "").replace("😡", "").strip()
    blocked_count = get_block_count().strip()

    header_text = " PYTHON FIREWALL "
    pad = (cols - len(header_text)) // 2
    print()
    print(" " * pad + c("╔" + "═" * (len(header_text) + 2) + "╗", Style.NEON_CYAN))
    print(" " * pad + c("║ " + header_text + " ║", Style.BOLD + Style.NEON_CYAN))
    print(" " * pad + c("╚" + "═" * (len(header_text) + 2) + "╝", Style.NEON_CYAN))

    print()
    stats_line = f"  {c('⚡', Style.YELLOW)} {c('CPU:', Style.GRAY)} {c(cpu, Style.WHITE)}    {c('🚫', Style.NEON_MAGENTA)} {c('Blocked:', Style.GRAY)} {c(blocked_count, Style.NEON_MAGENTA)}"
    print(stats_line)
    print()
    print(c("  ─" + "─" * (cols - 4), Style.DIM))

    ts_display = timestamp if timestamp else "waiting..."
    print(f"  {c('📡', Style.NEON_CYAN)} {c('Traffic as of:', Style.GRAY)} {c(ts_display, Style.WHITE)}")
    print(c("  ─" + "─" * (cols - 4), Style.DIM))
    print()

    if activity:
        half = cols // 2
        third = cols // 3

        if blocked:
            print(f"  {c('┌', Style.NEON_MAGENTA)}{c('─' * (third - 2), Style.NEON_MAGENTA)}{c('┐', Style.NEON_MAGENTA)}")
            print(f"  {c('│', Style.NEON_MAGENTA)} {c('🛑 BLOCKED TRAFFIC', Style.BOLD + Style.NEON_MAGENTA)}{' ' * max(0, third - 18 - 2)}{c('│', Style.NEON_MAGENTA)}")
            print(f"  {c('└', Style.NEON_MAGENTA)}{c('─' * (third - 2), Style.NEON_MAGENTA)}{c('┘', Style.NEON_MAGENTA)}")
            for line in blocked[:10]:
                parts = line.split(" ")
                ip = parts[3] if len(parts) > 3 else "?"
                rest = " ".join(parts[4:6]) if len(parts) > 5 else ""
                print(f"    {c('🛑', Style.NEON_MAGENTA)} {c(ip, Style.ORANGE)} {c(rest, Style.GRAY)}")
            if len(blocked) > 10:
                print(f"    {c(f'... and {len(blocked) - 10} more', Style.DIM)}")
            print()

        if queries:
            print(f"  {c('┌', Style.YELLOW)}{c('─' * (third - 2), Style.YELLOW)}{c('┐', Style.YELLOW)}")
            print(f"  {c('│', Style.YELLOW)} {c('⁉️ QUERIES', Style.BOLD + Style.YELLOW)}{' ' * max(0, third - 11 - 2)}{c('│', Style.YELLOW)}")
            print(f"  {c('└', Style.YELLOW)}{c('─' * (third - 2), Style.YELLOW)}{c('┘', Style.YELLOW)}")
            for line in queries[:10]:
                parts = line.split(" ")
                ip = parts[1] if len(parts) > 1 else "?"
                target = parts[2] if len(parts) > 2 else ""
                print(f"    {c('⁉️', Style.YELLOW)} {c(ip, Style.NEON_CYAN)} {c('→', Style.GRAY)} {c(target, Style.WHITE)}")
            if len(queries) > 10:
                print(f"    {c(f'... and {len(queries) - 10} more', Style.DIM)}")
            print()

        if counters:
            print(f"  {c('┌', Style.NEON_MAGENTA)}{c('─' * (third - 2), Style.NEON_MAGENTA)}{c('┐', Style.NEON_MAGENTA)}")
            print(f"  {c('│', Style.NEON_MAGENTA)} {c('📍 IP COUNTERS', Style.BOLD + Style.NEON_MAGENTA)}{' ' * max(0, third - 15 - 2)}{c('│', Style.NEON_MAGENTA)}")
            print(f"  {c('└', Style.NEON_MAGENTA)}{c('─' * (third - 2), Style.NEON_MAGENTA)}{c('┘', Style.NEON_MAGENTA)}")
            for line in counters[:10]:
                parts = line.split(" ")
                ip = parts[1] if len(parts) > 1 else "?"
                count = parts[2] if len(parts) > 2 else "?"
                print(f"    {c('📍', Style.NEON_MAGENTA)} {c(ip, Style.NEON_CYAN)} {c(count, Style.GREEN)}")
            if len(counters) > 10:
                print(f"    {c(f'... and {len(counters) - 10} more', Style.DIM)}")
            print()

        print(c("  ─" + "─" * (cols - 4), Style.DIM))
        print(f"  {c('Total entries:', Style.GRAY)} {c(str(len(activity)), Style.BOLD + Style.WHITE)}"
              f"    {c('Blocked:', Style.GRAY)} {c(str(len(blocked)), Style.NEON_MAGENTA)}"
              f"    {c('Queries:', Style.GRAY)} {c(str(len(queries)), Style.YELLOW)}"
              f"    {c('Counters:', Style.GRAY)} {c(str(len(counters)), Style.NEON_MAGENTA)}")
    else:
        print(f"  {c('No traffic data yet. Please wait...', Style.DIM)}")

    print()
