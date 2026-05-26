from commands import get_cpu_usage
from commands import get_block_count
from db import addHTML
from db import clearHTML
from db import getHTML
from db import html
from config import REDIRECT

def basicHTML(timestamp):
    cpu = get_cpu_usage()
    blocked = get_block_count().strip()
    HTML = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0f;color:#e0e0ff;font-family:"Courier New",monospace;min-height:100vh}}
.neon{{color:#00ffff;text-shadow:0 0 8px #00ffff,0 0 16px #00ffff}}
.glow{{box-shadow:0 0 10px #ff00ff,0 0 20px #ff00ff,inset 0 0 10px rgba(255,0,255,0.1)}}
.cyberpunk{{border:1px solid #00ffff;background:#050510}}
@keyframes neon-pulse{{0%,100%{{text-shadow:0 0 5px #0ff}}50%{{text-shadow:0 0 20px #0ff,0 0 40px #0ff}}}}
.terminal{{font-family:"Courier New",monospace;background:#050510;padding:4px 8px}}
.scanline{{background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,255,255,0.02) 2px,rgba(0,255,255,0.02) 4px)}}
header{{padding:16px;border-bottom:2px solid #00ffff;display:flex;align-items:center;justify-content:space-between}}
h1{{color:#00ffff;text-shadow:0 0 10px #00ffff;animation:neon-pulse 2s infinite}}
.stat-badge{{padding:4px 12px;border:1px solid #ff00ff;color:#ff00ff;font-family:monospace;margin:0 4px}}
main{{display:flex;flex:1;gap:1rem;padding:1rem}}
.column{{flex:1;background:#050510;border:1px solid #00ffff;overflow:hidden;display:flex;flex-direction:column}}
.column-header{{padding:0.75rem 1rem;font-size:0.8rem;font-weight:700;text-transform:uppercase;border-bottom:1px solid #00ffff;color:#00ffff}}
.column-header.blocked{{color:#ff00ff;border-bottom-color:#ff00ff}}
.column-header.queries{{color:#ffff00;border-bottom-color:#ffff00}}
.column-body{{flex:1;overflow-y:auto;padding:0.5rem}}
.entry{{display:flex;align-items:center;gap:0.5rem;padding:0.5rem 0.625rem;margin-bottom:0.25rem;font-size:0.8125rem;word-break:break-all}}
.entry a{{color:#00ffff;text-decoration:none}}
.entry a:hover{{color:#ff00ff;text-shadow:0 0 5px #ff00ff}}
.entry .meta{{color:#888}}
.entry .count{{background:rgba(0,255,255,0.1);color:#00ffff;padding:0.125rem 0.5rem;font-size:0.6875rem;margin-left:auto}}
</style>
</head><body class="scanline">
<header class="cyberpunk glow">
  <h1 class="neon" style="animation:neon-pulse 2s infinite">&#9889; CYBERPUNK FIREWALL TERMINAL</h1>
  <div>
    <span class="stat-badge">&#9889; {cpu}</span>
    <span class="stat-badge">&#128683; Blocked: {blocked}</span>
  </div>
</header>
<div class="terminal" style="padding:8px 16px;border-bottom:1px solid #00ffff">
  <span class="neon">Traffic as of:</span> {timestamp}
</div>
"""
    return HTML

def htmlRELOAD():
    return """<script>
(function() {
  let seconds = 40;
  const el = document.getElementById('countdown');
  const updateCountdown = () => { if (el) el.textContent = seconds + 's'; };
  const tick = () => {
    seconds--;
    if (seconds <= 0) { window.location.reload(); }
    updateCountdown();
  };
  const style = document.createElement('style');
  style.textContent =
    '#reload-bar { position: fixed; bottom: 0; left: 0; height: 3px; background: linear-gradient(90deg, #00ffff, #ff00ff); transition: width 1s linear; z-index: 999; }' +
    '#reload-indicator { position: fixed; bottom: 10px; right: 16px; font-size: 0.75rem; color: #00ffff; z-index: 999; font-family: monospace; }';
  document.head.appendChild(style);
  const bar = document.createElement('div'); bar.id = 'reload-bar'; bar.style.width = '100%';
  const indicator = document.createElement('div'); indicator.id = 'reload-indicator';
  indicator.innerHTML = '&#9889; <span id="countdown">40s</span>';
  document.body.appendChild(bar);
  document.body.appendChild(indicator);
  setInterval(tick, 1000);
  let w = 100;
  setInterval(function() { w -= 2.5; if (w < 0) w = 0; bar.style.width = w + '%'; }, 1000);
})();
</script>"""

def buildWeb(activity, timestamp):
    blocked_array = []
    standard_queries = []
    ip_counters = []

    for line in activity:
        if "🚨 Blocked IP:" in line:
            value = line.split(" ")
            line = f'<div class="entry"><span style="color:#ff00ff">&#128683;</span><a target="_blank" href="https://{value[3]}">{value[3]}</a> <span class="meta">{value[4]} {value[5]}</span><a style="color:#00ffff" target="_blank" href="/ip?ip={value[3]}&date={timestamp}">&#128269;</a></div>'
            blocked_array.append(line)
        if "🚨 Blocked Subnet:" in line:
            value = line.split(" ")
            line = f'<div class="entry"><span style="color:#ff00ff">&#128683;</span><a target="_blank" href="https://{value[3]}">{value[3]}</a> <span class="meta">{value[4]} {value[5]}</span><a style="color:#00ffff" target="_blank" href="/ip?ip={value[3]}&date={timestamp}">&#128269;</a></div>'
            blocked_array.append(line)
        if "📍" in line:
            URL_FIX = line.split(" ")
            value = line.split(" ")
            line = f'<div class="entry"><span style="color:#00ffff">&#128205;</span><a target="_blank" href="https://{URL_FIX[1]}">{URL_FIX[1]}</a><span class="count">{value[2]} hits</span><a style="color:#00ffff" target="_blank" href="/ip?ip={URL_FIX[1]}&date={timestamp}">&#128269;</a></div>'
            ip_counters.append(line)
        if "🕵️" in line:
            URL_FIX = line.split(" ")
            URL_PARSE = line.split(" ")
            line = f'<div class="entry"><span style="color:#ffff00">&#8265;</span><a target="_blank" href="https://{URL_FIX[1]}">{URL_FIX[1]}</a> <span class="meta">&#10132; {URL_PARSE[2]}</span><a style="color:#00ffff" target="_blank" href="/ip?ip={URL_PARSE[1]}&date={timestamp}">&#128269;</a></div>'
            standard_queries.append(line)

    clearHTML()
    addHTML(basicHTML(timestamp))

    addHTML('<main>')

    addHTML('<div class="column"><div class="column-header blocked">&#128683; Blocked Traffic</div><div class="column-body">')
    for line in blocked_array:
        addHTML(line)
    addHTML('</div></div>')

    addHTML('<div class="column"><div class="column-header queries">&#8265; Queries</div><div class="column-body">')
    for line in standard_queries:
        addHTML(line)
    addHTML('</div></div>')

    addHTML('<div class="column"><div class="column-header counters">&#128205; IP Counters</div><div class="column-body">')
    for line in ip_counters:
        addHTML(line)
    addHTML('</div></div>')

    addHTML('</main>')
    addHTML(htmlRELOAD())
    addHTML('</body></html>')
