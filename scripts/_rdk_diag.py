"""Stop the failing kiosk restart loop; inspect apt sources; look for browser packages already cached."""
import sys, paramiko
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

print("--- stop kiosk restart loop ---")
run(c, "systemctl stop makeup-mirror-kiosk.service", sudo=True)
run(c, "systemctl disable makeup-mirror-kiosk.service", sudo=True)
print("--- apt sources ---")
run(c, "cat /etc/apt/sources.list; echo ---; ls /etc/apt/sources.list.d/ 2>/dev/null; echo ---; grep -rh '^deb' /etc/apt/sources.list.d/ 2>/dev/null | head -20")
print("--- what browser-ish is in apt cache locally? ---")
run(c, "apt-cache pkgnames 2>&1 | grep -iE '^(firefox|chromium|epiphany|midori|falkon|surf|luakit|qutebrowser|webkit)' | head -30")
print("--- backend health ---")
run(c, "curl -sf --max-time 3 http://127.0.0.1:8080/detections.json | head -c 200; echo")
c.close()
