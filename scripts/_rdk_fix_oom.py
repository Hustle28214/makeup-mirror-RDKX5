"""Apply anti-OOM fixes on the RDK X5.

1. Create /swapfile (2 GB), enable swap, persist in fstab.
2. Push updated app.py + systemd unit.
3. daemon-reload + restart services.
4. Show before/after.
"""
import paramiko, sys, os
HOST, USER, PW = "192.168.127.10", "sunrise", "sunrise"
REPO_LOCAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_REMOTE = "/home/sunrise/make-up-mirror"

def shq(s): return "'" + s.replace("'", "'\\''") + "'"
def run(c, cmd, sudo=False, timeout=180, check=True):
    inner = f"sudo -S -p '' bash -lc {shq(cmd)}" if sudo else f"bash -lc {shq(cmd)}"
    stdin,stdout,stderr = c.exec_command(inner, timeout=timeout)
    if sudo: stdin.write(PW+"\n"); stdin.flush()
    out = stdout.read().decode('utf-8','replace')
    err = stderr.read().decode('utf-8','replace')
    rc = stdout.channel.recv_exit_status()
    if out.strip(): print(out.rstrip())
    if err.strip(): print("ERR:", err.rstrip(), file=sys.stderr)
    if check and rc != 0:
        raise RuntimeError(f"rc={rc}")
    return rc, out

c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, look_for_keys=False, allow_agent=False, timeout=15)

print("=== before ===")
run(c, "free -h; echo ---; swapon --show || echo no-swap")

print("=== create + enable 2GB swap (idempotent) ===")
run(c, r"""
set -e
if [ ! -f /swapfile ] || [ "$(stat -c %s /swapfile)" -lt 2147483648 ]; then
  swapoff /swapfile 2>/dev/null || true
  rm -f /swapfile
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
fi
swapon /swapfile || true
if ! grep -q "^/swapfile" /etc/fstab; then
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi
sysctl vm.swappiness=30 || true
grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=30' >> /etc/sysctl.conf
""", sudo=True, timeout=300)

print("=== swap now ===")
run(c, "free -h; echo ---; swapon --show")

print("=== push updated files ===")
sftp = c.open_sftp()
uploads = [
    ("backend/app.py",                       f"{REPO_REMOTE}/backend/app.py"),
    ("systemd/makeup-mirror-backend.service", f"{REPO_REMOTE}/systemd/makeup-mirror-backend.service"),
]
for local, remote in uploads:
    sftp.put(os.path.join(REPO_LOCAL, local), remote)
    print("pushed", local)
sftp.close()

print("=== install unit + reload + restart ===")
run(c, f"cp {REPO_REMOTE}/systemd/makeup-mirror-backend.service /etc/systemd/system/makeup-mirror-backend.service", sudo=True)
run(c, "systemctl daemon-reload", sudo=True)
run(c, "systemctl restart makeup-mirror-backend.service", sudo=True)
run(c, "systemctl restart makeup-mirror-kiosk.service", sudo=True)

print("=== settle 8s ===")
run(c, "sleep 8")

print("=== after: memory + top CPU ===")
run(c, "free -h")
run(c, "ps -eo pid,pcpu,pmem,rss,comm --sort=-pcpu | head -8")

print("=== backend health ===")
run(c, "curl -sf --max-time 3 http://127.0.0.1:8080/detections.json | head -c 200; echo")
run(c, "systemctl --no-pager --lines=5 status makeup-mirror-backend.service", check=False)
c.close()
print("done.")
