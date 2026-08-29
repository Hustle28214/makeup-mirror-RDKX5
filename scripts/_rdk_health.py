"""Live health probe: memory, load, OOM/kernel logs, service journal tails."""
import paramiko, sys
HOST, USER, PW = "192.168.127.10", "sunrise", "sunrise"
def shq(s): return "'" + s.replace("'", "'\\''") + "'"
def run(c, cmd, sudo=False, timeout=60):
    inner = f"sudo -S -p '' bash -lc {shq(cmd)}" if sudo else f"bash -lc {shq(cmd)}"
    stdin,stdout,stderr = c.exec_command(inner, timeout=timeout)
    if sudo: stdin.write(PW+"\n"); stdin.flush()
    out = stdout.read().decode('utf-8','replace')
    err = stderr.read().decode('utf-8','replace')
    if out.strip(): print(out.rstrip())
    if err.strip(): print("ERR:", err.rstrip(), file=sys.stderr)

c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, look_for_keys=False, allow_agent=False, timeout=15)

print("=== uptime / load ==="); run(c, "uptime; echo; nproc")
print("=== free -h ==="); run(c, "free -h")
print("=== swap ==="); run(c, "swapon --show; cat /proc/swaps 2>&1")
print("=== top memory hogs ===")
run(c, "ps -eo pid,ppid,pcpu,pmem,rss,comm --sort=-rss | head -12")
print("=== top CPU hogs ===")
run(c, "ps -eo pid,pcpu,pmem,rss,comm --sort=-pcpu | head -12")
print("=== backend service ===")
run(c, "systemctl --no-pager --lines=5 status makeup-mirror-backend.service")
print("=== backend log tail ===")
run(c, "journalctl -u makeup-mirror-backend.service --no-pager -n 30")
print("=== kiosk service ===")
run(c, "systemctl --no-pager --lines=5 status makeup-mirror-kiosk.service")
print("=== dmesg — last OOM / kernel errors ===")
run(c, "dmesg | tail -40", sudo=True)
print("=== journalctl -k tail ===")
run(c, "journalctl -k -n 20 --no-pager")
print("=== /proc/loadavg + /proc/pressure/* ===")
run(c, "cat /proc/loadavg; echo ---; for p in /proc/pressure/*; do echo $p; cat $p; done 2>/dev/null")
print("=== temperature ===")
run(c, "cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | awk '{printf \"%.1f C\\n\", $1/1000}'")
c.close()
