"""Install firefox on the RDK X5, push the updated kiosk.sh, restart kiosk unit."""
import sys, paramiko, os
HOST, USER, PW = "192.168.127.10", "sunrise", "sunrise"
def shq(s): return "'" + s.replace("'", "'\\''") + "'"
def run(c, cmd, sudo=False, timeout=900, quiet=False):
    inner = f"sudo -S -p '' bash -lc {shq(cmd)}" if sudo else f"bash -lc {shq(cmd)}"
    stdin,stdout,stderr = c.exec_command(inner, timeout=timeout)
    if sudo: stdin.write(PW+"\n"); stdin.flush()
    out = stdout.read().decode('utf-8','replace')
    err = stderr.read().decode('utf-8','replace')
    if not quiet:
        if out.strip(): print(out.rstrip())
        if err.strip(): print("ERR:", err.rstrip(), file=sys.stderr)
    return stdout.channel.recv_exit_status(), out, err

c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, look_for_keys=False, allow_agent=False, timeout=15)

print("--- policy firefox ---")
run(c, "apt-cache policy firefox 2>&1 | head -20")

print("--- push updated kiosk.sh ---")
sftp = c.open_sftp()
sftp.put("scripts/kiosk.sh", "/home/sunrise/make-up-mirror/scripts/kiosk.sh")
sftp.chmod("/home/sunrise/make-up-mirror/scripts/kiosk.sh", 0o755)
sftp.close()

print("--- apt install firefox (this can take a while) ---")
rc, out, err = run(c, "DEBIAN_FRONTEND=noninteractive apt install -y firefox 2>&1 | tail -30",
                   sudo=True, timeout=900)
print("apt rc =", rc)

print("--- firefox binary? ---")
run(c, "command -v firefox; firefox --version 2>&1 | head -3")

print("--- re-enable + start kiosk ---")
run(c, "systemctl enable makeup-mirror-kiosk.service", sudo=True)
run(c, "systemctl restart makeup-mirror-kiosk.service", sudo=True)
run(c, "sleep 3; systemctl --no-pager --lines=15 status makeup-mirror-kiosk.service", check=False)

print("--- final: backend health + curl / ---")
run(c, "curl -sf --max-time 3 http://127.0.0.1:8080/detections.json | head -c 200; echo")
run(c, "curl -sf --max-time 3 -o /dev/null -w 'HTTP %{http_code}\\n' http://127.0.0.1:8080/")
c.close()
