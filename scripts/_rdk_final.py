"""Final status check on the RDK X5."""
import paramiko, sys
HOST, USER, PW = "192.168.127.10", "sunrise", "sunrise"
def shq(s): return "'" + s.replace("'", "'\\''") + "'"
def run(c, cmd, sudo=False, timeout=30):
    inner = f"sudo -S -p '' bash -lc {shq(cmd)}" if sudo else f"bash -lc {shq(cmd)}"
    stdin,stdout,stderr = c.exec_command(inner, timeout=timeout)
    if sudo: stdin.write(PW+"\n"); stdin.flush()
    out = stdout.read().decode('utf-8','replace')
    err = stderr.read().decode('utf-8','replace')
    if out.strip(): print(out.rstrip())
    if err.strip(): print("ERR:", err.rstrip(), file=sys.stderr)

c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, look_for_keys=False, allow_agent=False, timeout=15)

print("=== backend ===")
run(c, "systemctl --no-pager --lines=8 status makeup-mirror-backend.service")
print()
print("=== kiosk ===")
run(c, "systemctl --no-pager --lines=15 status makeup-mirror-kiosk.service")
print()
print("=== units enabled? ===")
run(c, "systemctl is-enabled makeup-mirror-backend.service makeup-mirror-kiosk.service")
print()
print("=== detection.json ===")
run(c, "curl -sf --max-time 3 http://127.0.0.1:8080/detections.json | python3 -c 'import sys,json; d=json.load(sys.stdin); print(\"mode:\",d.get(\"mode\"), \"face:\",d.get(\"face\"), \"lighting:\",d.get(\"lighting\",{}).get(\"verdict\"))'")
print()
print("=== firefox running? ===")
run(c, "pgrep -af firefox | head -3 || echo not-running")
c.close()
