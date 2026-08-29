"""apt update + list of available browser packages on the RDK X5."""
import sys, paramiko
HOST, USER, PW = "192.168.127.10", "sunrise", "sunrise"

def shq(s): return "'" + s.replace("'", "'\\''") + "'"

def run(c, cmd, sudo=False, timeout=300):
    inner = f"sudo -S -p '' bash -lc {shq(cmd)}" if sudo else f"bash -lc {shq(cmd)}"
    stdin,stdout,stderr = c.exec_command(inner, timeout=timeout)
    if sudo: stdin.write(PW+"\n"); stdin.flush()
    out = stdout.read().decode('utf-8','replace')
    err = stderr.read().decode('utf-8','replace')
    if out.strip(): print(out.rstrip())
    if err.strip(): print("ERR:", err.rstrip(), file=sys.stderr)
    return stdout.channel.recv_exit_status()

c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, look_for_keys=False, allow_agent=False, timeout=15)

print("--- apt update ---")
run(c, "apt update 2>&1 | tail -5", sudo=True)
print("--- search firefox / chromium / others ---")
run(c, "apt-cache search --names-only firefox 2>&1 | head -20")
run(c, "apt-cache search --names-only chromium 2>&1 | head -20")
run(c, "apt-cache search --names-only epiphany 2>&1 | head -10")
run(c, "apt-cache search --names-only midori 2>&1 | head -10")
run(c, "apt-cache search --names-only falkon 2>&1 | head -10")
print("--- policy chromium ---")
run(c, "apt-cache policy chromium chromium-browser firefox firefox-esr 2>&1 | head -40")
c.close()
