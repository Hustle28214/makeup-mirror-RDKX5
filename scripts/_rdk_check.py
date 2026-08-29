"""Check kiosk state on the board, install chromium if missing, restart kiosk."""
import sys
import paramiko

HOST, USER, PW = "192.168.127.10", "sunrise", "sunrise"

def run(c, cmd, sudo=False, check=False):
    if sudo:
        cmd = f"sudo -S -p '' bash -lc {shq(cmd)}"
    else:
        cmd = f"bash -lc {shq(cmd)}"
    stdin, stdout, stderr = c.exec_command(cmd, timeout=600)
    if sudo:
        stdin.write(PW + "\n"); stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if out.strip(): print(out.rstrip())
    if err.strip(): print("STDERR:", err.rstrip(), file=sys.stderr)
    if check and rc != 0:
        raise RuntimeError(f"rc={rc}: {cmd}")
    return rc, out

def shq(s): return "'" + s.replace("'", "'\\''") + "'"

c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, look_for_keys=False, allow_agent=False, timeout=15)

print("--- kiosk status ---")
run(c, "systemctl --no-pager --lines=20 status makeup-mirror-kiosk.service")
print("--- chromium binaries ---")
run(c, "command -v chromium chromium-browser google-chrome 2>&1 || echo NONE")
print("--- apt-cache policy chromium-browser ---")
run(c, "apt-cache policy chromium-browser chromium 2>&1 | head -30")
print("--- X server present? ---")
run(c, "ls /tmp/.X11-unix 2>&1; echo --; pgrep -a Xorg || pgrep -a wayland; echo --; systemctl get-default")
c.close()
