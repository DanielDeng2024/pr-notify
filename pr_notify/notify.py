"""Desktop notification delivery (macOS, via the bundled PRNotify.app helper).

The helper calls UNUserNotificationCenter directly, so notifications show up
under their own "PRNotify" entry in System Settings and clicking one opens the PR.
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

from .events import Event

BUNDLE_ID = "com.local.pr-notify"
_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "helper" / "PRNotify.swift"
_ICON = _ROOT / "assets" / "icon.png"
APP = Path.home() / ".local" / "share" / "pr-notify" / "PRNotify.app"
BIN = APP / "Contents" / "MacOS" / "PRNotify"
STAMP = APP.parent / "PRNotify.src-hash"  # outside the bundle: codesign rejects stray files in it

_PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>{BUNDLE_ID}</string>
  <key>CFBundleName</key><string>PRNotify</string>
  <key>CFBundleDisplayName</key><string>PRNotify</string>
  <key>CFBundleExecutable</key><string>PRNotify</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSUIElement</key><true/>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
</dict></plist>
"""


class NotifyError(RuntimeError):
    pass


def summarize(events: list[Event]) -> str:
    """One line per distinct text, e.g. 'alice commented (x3)'."""
    counts = Counter(e.text for e in events)
    return "\n".join(t if n == 1 else f"{t} (x{n})" for t, n in counts.items())


def install_helper() -> None:
    """Compile and ad-hoc sign PRNotify.app from helper/PRNotify.swift."""
    if not _SRC.exists():
        raise NotifyError(f"helper source missing: {_SRC}")
    (APP / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (APP / "Contents" / "Info.plist").write_text(_PLIST)
    _build_icons()
    for cmd in (
        ["swiftc", "-O", "-swift-version", "5", str(_SRC), "-o", str(BIN)],
        ["codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID, str(APP)],
    ):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise NotifyError(f"{cmd[0]} failed: {proc.stderr.strip()}")
    (STAMP).write_text(_src_hash())


def _build_icons() -> None:
    """AppIcon.icns (Finder, notification banners) and a small menubar.png from assets/icon.png."""
    res = APP / "Contents" / "Resources"
    res.mkdir(parents=True, exist_ok=True)
    if not _ICON.exists():
        return
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                _run(["sips", "-z", str(size * scale), str(size * scale), str(_ICON), "--out", str(iconset / name)])
        _run(["iconutil", "-c", "icns", str(iconset), "-o", str(res / "AppIcon.icns")])
    _run(["sips", "-z", "36", "36", str(_ICON), "--out", str(res / "menubar.png")])  # 18pt @2x


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise NotifyError(f"{cmd[0]} failed: {proc.stderr.strip()}")


def _src_hash() -> str:
    h = hashlib.sha256(_SRC.read_bytes())
    if _ICON.exists():
        h.update(_ICON.read_bytes())
    return h.hexdigest()


def _ensure_helper() -> None:
    if not BIN.exists() or not STAMP.exists() or STAMP.read_text() != _src_hash():
        install_helper()


def check() -> tuple[int, str]:
    """Request permission (shows the macOS prompt on first use) and report status."""
    _ensure_helper()
    proc = subprocess.run([str(BIN), "--check"], capture_output=True, text=True, timeout=120)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def send(title: str, body: str, url: str, ident: str) -> None:
    if sys.platform != "darwin":
        raise NotifyError("desktop notifications are only implemented for macOS")
    _ensure_helper()
    proc = subprocess.run(
        [str(BIN), "--title", title, "--body", body, "--url", url, "--id", ident],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise NotifyError(proc.stderr.strip() or f"PRNotify exited {proc.returncode}")
