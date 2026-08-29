"""Find an available browser package on the RDK X5."""
import sys, paramiko
HOST, USER, PW = "192.168.127.10", "sunrise", "sunrise"
def run(c, cmd, sudo=False):
    if sudo: cmd = f"sudo -S -p '' bash -lc '{cmd}'"
    else:    cmd = f"bash -lc '{cmd}'"
    stdin,stdout,stderr = c.exec_command(cmd, timeout=180)
    if sudo: stdin.write(PW+"\n"); stdin.flush()
    print(stdout.read().decode('utf-8','replace').rstrip())
    er = stderr.read().decode('utf-8','replace')
    if er.strip(): print("ERR:", er.rstrip(), file=sys.stderr)
c=paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, look_for_keys=False, allow_agent=False, timeout=15)
print("--- lsb_release ---"); run(c, "lsb_release -a 2>&1")
print("--- apt-cache search browsers ---")
run(c, "apt-cache search --names-only 'firefox|chromium|epiphany|midori|falkon|surf' 2>&1 | head -30")
print("--- snap? ---"); run(c, "command -v snap && snap list 2>&1 | head -10 || echo no-snap")
print("--- desktop env / DM ---")
run(c, "systemctl status lightdm --no-pager 2>&1 | head -5")
run(c, "echo XDG_CURRENT_DESKTOP=$XDG_CURRENT_DESKTOP; ls /usr/share/xsessions 2>&1")
c.close()
