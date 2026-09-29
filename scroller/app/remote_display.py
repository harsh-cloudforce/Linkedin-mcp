"""Virtual display + VNC for remote LinkedIn login in Azure (no local browser)."""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


NOVNC_ROOT = Path(os.getenv("NOVNC_ROOT", "/usr/share/novnc"))
if not NOVNC_ROOT.is_dir():
    alt = Path("/usr/share/novnc")
    NOVNC_ROOT = alt if alt.is_dir() else NOVNC_ROOT

LOG_DIR = Path(os.getenv("REMOTE_DISPLAY_LOG_DIR", "/tmp/remote-display"))


@dataclass
class RemoteDisplay:
    display: str  # e.g. ":99"
    vnc_port: int
    processes: list[subprocess.Popen] = field(default_factory=list)

    @property
    def display_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        return env


_active: RemoteDisplay | None = None


def remote_login_available() -> bool:
    # websockify not required — Starlette bridges browser WS ↔ x11vnc TCP directly
    return bool(shutil.which("Xvfb") and shutil.which("x11vnc"))


def novnc_static_root() -> Path | None:
    if NOVNC_ROOT.is_dir():
        return NOVNC_ROOT
    return None


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def display_ready() -> bool:
    d = _active
    if d is None:
        return False
    for p in d.processes:
        if p.poll() is not None:
            return False
    return _port_open("127.0.0.1", d.vnc_port)


def start_remote_display(
    *,
    display_num: int = 99,
    vnc_port: int = 5900,
) -> RemoteDisplay:
    """Start Xvfb + x11vnc. One active session at a time (ACA single replica)."""
    global _active
    stop_remote_display()

    if not remote_login_available():
        raise RuntimeError(
            "Remote login requires Xvfb and x11vnc (install in the container image)."
        )

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    display = f":{display_num}"
    procs: list[subprocess.Popen] = []

    xvfb_log = (LOG_DIR / "xvfb.log").open("ab", buffering=0)
    x11_log = (LOG_DIR / "x11vnc.log").open("ab", buffering=0)

    xvfb = subprocess.Popen(
        [
            "Xvfb",
            display,
            "-screen",
            "0",
            "1400x900x24",
            "-ac",
            "+extension",
            "RANDR",
        ],
        stdout=xvfb_log,
        stderr=subprocess.STDOUT,
    )
    procs.append(xvfb)
    time.sleep(0.8)
    if xvfb.poll() is not None:
        _kill_all(procs)
        raise RuntimeError("Xvfb failed to start")

    x11vnc = subprocess.Popen(
        [
            "x11vnc",
            "-display",
            display,
            "-rfbport",
            str(vnc_port),
            "-localhost",
            "-forever",
            "-shared",
            "-nopw",
            "-xkb",
            "-noxdamage",
            "-wait",
            "10",
            "-defer",
            "10",
            "-quiet",
        ],
        stdout=x11_log,
        stderr=subprocess.STDOUT,
    )
    procs.append(x11vnc)
    for _ in range(20):
        if x11vnc.poll() is not None:
            _kill_all(procs)
            raise RuntimeError("x11vnc failed to start (see /tmp/remote-display/x11vnc.log)")
        if _port_open("127.0.0.1", vnc_port):
            break
        time.sleep(0.25)
    else:
        _kill_all(procs)
        raise RuntimeError("x11vnc did not open VNC port")

    print(f"[remote-display] ready display={display} vnc={vnc_port}", flush=True)
    _active = RemoteDisplay(display=display, vnc_port=vnc_port, processes=procs)
    return _active


def stop_remote_display() -> None:
    global _active
    if _active is None:
        return
    _kill_all(_active.processes)
    _active = None
    print("[remote-display] stopped", flush=True)


def get_active_display() -> RemoteDisplay | None:
    return _active


def _kill_all(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        try:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        except Exception:
            pass
    time.sleep(0.3)
    for p in procs:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass


def build_login_url(public_base: str, access_token: str) -> str:
    """Full URL the agent must show unchanged (include /login/ prefix).

    path MUST be absolute (/websockify). A relative path=websockify from
    /login/vnc.html makes the browser open /login/websockify → connection fails.
    """
    base = public_base.rstrip("/")
    return (
        f"{base}/login/vnc.html"
        f"?autoconnect=true&resize=scale"
        f"&path=/websockify"
        f"&token={access_token}"
    )


def session_summary() -> dict[str, Any]:
    d = get_active_display()
    if not d:
        return {"active": False, "ready": False}
    return {
        "active": True,
        "ready": display_ready(),
        "display": d.display,
        "vnc_port": d.vnc_port,
        "novnc_root": str(novnc_static_root()),
        "vnc_port_open": _port_open("127.0.0.1", d.vnc_port),
        "process_alive": all(p.poll() is None for p in d.processes),
    }
