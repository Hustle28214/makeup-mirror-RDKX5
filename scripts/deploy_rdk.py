"""Deploy make-up-mirror to an RDK X5 board over SSH (password auth).

Usage:
    RDK_PASSWORD='...' python scripts/deploy_rdk.py [HOST] [USER]

Defaults: sunrise@192.168.127.10. Set RDK_PASSWORD in the environment; no password is stored in source.

Steps:
    1. probe the board (python3 / cv2 / video device / chromium)
    2. rsync (via SFTP) the repo tree to /home/<user>/make-up-mirror
    3. render the systemd units (substituting user + repo path) and install
    4. daemon-reload + enable + (re)start both units
    5. print final status + a curl of /detections.json for sanity
"""

from __future__ import annotations

import os
import posixpath
import stat
import sys
from pathlib import Path

import paramiko

DEFAULT_HOST = "192.168.127.10"
DEFAULT_USER = "sunrise"
DEFAULT_PASS = os.environ.get("RDK_PASSWORD", "")

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_ROOT_TMPL = "/home/{user}/make-up-mirror"

# Only these subtrees + files ship to the board.
INCLUDE_DIRS = ["backend", "frontend", "scripts", "systemd"]
INCLUDE_FILES = ["README.md"]
EXCLUDE_NAMES = {"__pycache__", ".pytest_cache", ".vscode", ".idea"}
EXCLUDE_EXTS = {".pyc", ".pyo"}


# --------------------------------------------------------------------------

def _print(step: str, msg: str) -> None:
    print(f"[{step}] {msg}", flush=True)


def run(client: paramiko.SSHClient, cmd: str, *, sudo_pw: str | None = None,
        check: bool = True, quiet: bool = False) -> tuple[int, str, str]:
    if sudo_pw:
        # -S reads password from stdin; -p '' suppresses the prompt.
        cmd = f"sudo -S -p '' bash -lc {shell_quote(cmd)}"
    else:
        cmd = f"bash -lc {shell_quote(cmd)}"
    stdin, stdout, stderr = client.exec_command(cmd, get_pty=False, timeout=120)
    if sudo_pw:
        stdin.write(sudo_pw + "\n"); stdin.flush()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    if not quiet:
        if out.strip():
            print(out.rstrip())
        if err.strip():
            print(err.rstrip(), file=sys.stderr)
    if check and rc != 0:
        raise RuntimeError(f"remote command failed (rc={rc}): {cmd}")
    return rc, out, err


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# --- SFTP mkdir/put helpers ----------------------------------------------

def sftp_mkdir_p(sftp: paramiko.SFTPClient, remote_path: str) -> None:
    parts = remote_path.strip("/").split("/")
    cur = ""
    for p in parts:
        cur += "/" + p
        try:
            sftp.stat(cur)
        except IOError:
            sftp.mkdir(cur)


def sftp_put_tree(sftp: paramiko.SFTPClient, local: Path, remote: str) -> int:
    n = 0
    if local.is_file():
        sftp_mkdir_p(sftp, posixpath.dirname(remote))
        sftp.put(str(local), remote)
        _mirror_mode(sftp, local, remote)
        return 1
    sftp_mkdir_p(sftp, remote)
    for entry in local.iterdir():
        if entry.name in EXCLUDE_NAMES:
            continue
        if entry.suffix in EXCLUDE_EXTS:
            continue
        r = posixpath.join(remote, entry.name)
        if entry.is_dir():
            n += sftp_put_tree(sftp, entry, r)
        else:
            sftp.put(str(entry), r)
            _mirror_mode(sftp, entry, r)
            n += 1
    return n


def _mirror_mode(sftp: paramiko.SFTPClient, local: Path, remote: str) -> None:
    # Executable bits on .sh files matter; everything else stays 0644.
    if local.suffix == ".sh":
        sftp.chmod(remote, 0o755)


# --- main -----------------------------------------------------------------

def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    user = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_USER
    password = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_PASS
    if not password:
        raise SystemExit("Set RDK_PASSWORD or pass the SSH password as the third argument.")
    remote_root = REMOTE_ROOT_TMPL.format(user=user)

    _print("ssh", f"connecting to {user}@{host} …")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password,
                   look_for_keys=False, allow_agent=False, timeout=15,
                   banner_timeout=15)
    _print("ssh", "connected")

    # ---- 1. probe -----------------------------------------------------
    _print("probe", "gathering board info…")
    for cmd, label in [
        ("uname -a",                             "uname"),
        ("python3 --version",                    "python3"),
        ("python3 -c 'import cv2; print(cv2.__version__)' 2>&1 | tail -1", "cv2"),
        ("python3 -c 'import numpy; print(numpy.__version__)' 2>&1 | tail -1", "numpy"),
        ("ls -l /dev/video* 2>&1 | head",        "video devices"),
        ("v4l2-ctl --list-devices 2>&1 | head -20 || echo v4l2-ctl-missing", "v4l2 list"),
        ("command -v chromium chromium-browser google-chrome 2>&1 || true",  "browser"),
    ]:
        _print(label, "")
        run(client, cmd, check=False)

    # ---- 2. sync tree -------------------------------------------------
    _print("sync", f"pushing repo to {remote_root} …")
    run(client, f"mkdir -p {shell_quote(remote_root)}")
    sftp = client.open_sftp()
    n = 0
    for d in INCLUDE_DIRS:
        local = REPO_ROOT / d
        if not local.exists():
            continue
        n += sftp_put_tree(sftp, local, posixpath.join(remote_root, d))
    for f in INCLUDE_FILES:
        local = REPO_ROOT / f
        if local.exists():
            sftp.put(str(local), posixpath.join(remote_root, f))
            n += 1
    sftp.close()
    _print("sync", f"pushed {n} files")

    # Make sure launcher scripts are executable (double-belt).
    run(client, f"chmod +x {shell_quote(remote_root)}/scripts/*.sh")

    # ---- 3. ensure deps ----------------------------------------------
    _print("deps", "ensuring opencv/numpy present…")
    rc, out, _ = run(client,
        "python3 -c 'import cv2, numpy' 2>&1 && echo OK || echo MISSING",
        check=False, quiet=True)
    if "OK" in out:
        _print("deps", "cv2 + numpy already installed")
    else:
        _print("deps", "installing python3-opencv + python3-numpy via apt")
        run(client, "apt update && apt install -y python3-opencv python3-numpy",
            sudo_pw=password)

    # ---- 4. install systemd unit --------------------------------------
    _print("systemd", "rendering + installing units")
    render_and_install(client, password, remote_root, user)
    run(client, "systemctl daemon-reload", sudo_pw=password)
    run(client, "systemctl enable makeup-mirror-backend.service", sudo_pw=password)
    run(client, "systemctl restart makeup-mirror-backend.service", sudo_pw=password)

    # Kiosk is optional — only enable it if a graphical target exists.
    rc, out, _ = run(client, "systemctl get-default", check=False, quiet=True)
    if "graphical" in out:
        _print("systemd", "graphical target detected, enabling kiosk")
        run(client, "systemctl enable makeup-mirror-kiosk.service",
            sudo_pw=password, check=False)
        run(client, "systemctl restart makeup-mirror-kiosk.service",
            sudo_pw=password, check=False)
    else:
        _print("systemd", "no graphical target — kiosk unit installed but not enabled")

    # ---- 5. verify ----------------------------------------------------
    _print("verify", "waiting 3s for backend to bind…")
    run(client, "sleep 3")
    _print("verify", "systemctl status:")
    run(client, "systemctl --no-pager --lines=15 status makeup-mirror-backend.service",
        check=False)
    _print("verify", "curl /detections.json:")
    run(client, "curl -sf --max-time 4 http://127.0.0.1:8080/detections.json | head -c 400",
        check=False)
    print()
    _print("verify", "curl /:")
    run(client, "curl -sf --max-time 4 -o /dev/null -w 'HTTP %{http_code}\\n' http://127.0.0.1:8080/",
        check=False)

    _print("done", f"open http://{host}:8080/ in a browser")
    client.close()
    return 0


def render_and_install(client, sudo_pw, remote_root, user):
    """Substitute paths in the two unit files and install to /etc/systemd/system."""
    for unit in ("makeup-mirror-backend.service", "makeup-mirror-kiosk.service"):
        src = f"{remote_root}/systemd/{unit}"
        dst = f"/etc/systemd/system/{unit}"
        cmd = (
            f"sed -e 's|/home/sunrise/make-up-mirror|{remote_root}|g' "
            f"-e 's|User=sunrise|User={user}|g' "
            f"-e 's|/home/sunrise/.Xauthority|/home/{user}/.Xauthority|g' "
            f"{src} > /tmp/{unit} && mv /tmp/{unit} {dst}"
        )
        run(client, cmd, sudo_pw=sudo_pw)


if __name__ == "__main__":
    sys.exit(main())
