"""VM-HOST side of the System Settings server-enable UI test. Boots the INSTALLED disk the way the
install gate does (its NVRAM, a serial console, systemd's debug shell on ttyS1), opens the desktop's
CDP port for the TEST ONLY, and keeps the VM up until killed. Root shell commands are relayed from
a FIFO so the driver on the other machine can ask things.

    python3 check_livecd_install_vm.py ISO --disk /root/probe/ui-install.qcow2 --keep-disk --evidence-dir /root/probe/ev-install
    python3 vm_server_ui_boot.py /root/probe/ui-install.qcow2 /root/probe/ev-install/OVMF_VARS.fd /root/probe/ev-ui

(both copied next to each other on the KVM host). The desktop's debug port is opened on the TEST DISK
ONLY (/etc/environment + a restart of the tty1 login), never in the image. Root commands for the
driver's evidence: append lines to <evidence>/cmds; answers land in <evidence>/root-shell.log.
Stop it by killing its qemu when the driver has passed -- a server left running rate-limits relays.
"""
import os, re, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_livecd_install_vm as g

disk, vars_copy, ev = sys.argv[1], sys.argv[2], Path(sys.argv[3])
ev.mkdir(parents=True, exist_ok=True)
sd = ev / "sock"; sd.mkdir(exist_ok=True)
code, _ = g.ovmf()
print("audible:", g._make_installed_boot_audible(disk, ev, extra="systemd.debug_shell=ttyS1"), flush=True)
con_s, sh_s = sd / "boot-console.sock", sd / "root-shell.sock"
for s in (con_s, sh_s):
    s.unlink(missing_ok=True)
args = g.qemu_args(disk, None, con_s, code, vars_copy, 6144, 4, net=True, shell_path=sh_s)
i = args.index("-nic"); args[i + 1] += ",hostfwd=tcp:127.0.0.1:9322-:9223"
proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=open(ev / "qemu.err", "w"))
for _ in range(100):
    if con_s.exists() or proc.poll() is not None:
        break
    time.sleep(0.1)
con = g.Serial(con_s, open(ev / "boot-console.log", "w"))
deadline = time.time() + 900
while time.time() < deadline:
    con.read(1.0)
    if re.search(r"(Please enter passphrase|Enter passphrase)", con.buf) and "PASS-SENT" not in globals():
        con.send(g.INSTALL_PASSWORD); globals()["PASS-SENT"] = 1; print("sent the disk passphrase", flush=True)
    if re.search(r"(login:|Startup finished)", con.buf):
        break
print("booted" if re.search(r"(login:|Startup finished)", con.buf) else "NOT booted", flush=True)
sh = g.Serial(sh_s, open(ev / "root-shell.log", "w"))


def ask(cmd, pat, t=120):
    mark = len(sh.buf)
    sh.send(cmd)
    return sh.expect(pat, t, since=mark)


print(ask("echo R''OOT-OK", r"ROOT-OK", 60) is not None and "root shell OK", flush=True)
ask("systemctl is-system-running --wait; echo B''OOTED", r"BOOTED", 600)
ask("grep -q PC_SHELL_EXTRA_ARGS /etc/environment || echo 'PC_SHELL_EXTRA_ARGS=--remote-debugging-port=9222' >> /etc/environment; echo E''NV-OK", r"ENV-OK")
relay = ("nohup python3 -c \"import socket,threading\n"
         "def p(a,b):\n try:\n  while 1:\n   d=a.recv(65536)\n   if not d:break\n   b.sendall(d)\n except Exception:pass\n a.close();b.close()\n"
         "s=socket.socket();s.setsockopt(1,2,1);s.bind(('0.0.0.0',9223));s.listen(8)\n"
         "while 1:\n c,_=s.accept()\n try:\n  u=socket.create_connection(('127.0.0.1',9222))\n except Exception:\n  c.close();continue\n"
         " threading.Thread(target=p,args=(c,u),daemon=1).start();threading.Thread(target=p,args=(u,c),daemon=1).start()\n\" >/dev/null 2>&1 &")
sh.send(relay)
ask("systemctl restart getty@tty1; echo G''ETTY-OK", r"GETTY-OK")
print("login screen restarted with the debug port; relay 9223->9222 up; host 127.0.0.1:9322", flush=True)
# Keep answering: commands appended to ev/cmd.fifo-style file are run; results go to root-shell.log.
cmdf = ev / "cmds"; cmdf.write_text(""); done = 0
while proc.poll() is None:
    sh.read(1.0); con.read(0.1)
    lines = cmdf.read_text().splitlines()
    for line in lines[done:]:
        sh.send(line)
    done = len(lines)
print("qemu exited", proc.returncode, flush=True)
