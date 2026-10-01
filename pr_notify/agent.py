"""launchd login item that keeps the PRNotify menu bar app running."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import notify

LABEL = "com.local.pr-notify"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
REPO = Path(__file__).resolve().parent.parent
INTERVAL = 120


class AgentError(RuntimeError):
    pass


def _launchctl(*args: str, check: bool = True) -> None:
    proc = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AgentError(f"launchctl {' '.join(args)}: {proc.stderr.strip()}")


def install(state_path: Path, interval: int = INTERVAL) -> None:
    notify.install_helper()
    log = state_path.parent / "app.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            str(notify.BIN), "--menubar",
            "--python", sys.executable, "--cwd", str(REPO),
            "--interval", str(interval), "--state", str(state_path),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}", check=False)
    _launchctl("bootstrap", domain, str(PLIST))
    print(f"installed {PLIST}\nmenu bar app running; log: {log}")


def uninstall() -> None:
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}", check=False)
    PLIST.unlink(missing_ok=True)
    print("removed login item")
