"""Whether Proton's HDR switch has to be forced on this desktop.

GE-Proton 11 gives a game HDR only when Wine's Wayland driver sees the
monitor already in HDR. Some desktops switch to HDR only once a game asks
for it, so the monitor looks SDR at start and the game's HDR option stays
grey: Hyprland with render:cm_auto_hdr (the monitor on cm = srgb), KDE at
an SDR brightness equal to the peak brightness. PROTON_FORCE_HDR=1 reports
HDR anyway (GE's own proton script names both cases).

Forcing is harmless where the monitor really shows HDR (in HDR mode, or
switched to it by the desktop) and wrong only where it can't - there the
game sends an HDR picture to an SDR screen and it looks washed out. So it's
forced only where this desktop can show it, judged by the monitor a game
opens on - the active one (an HDR and an SDR monitor side by side: a game on
the SDR one would look washed out):
- Hyprland: the focused monitor HDR-capable with cm_auto_hdr on, or already
  on the hdr / hdredid preset;
- KDE: HDR switched on for the primary monitor (its sdr-brightness vs peak
  can't be read: kscreen-doctor doesn't give the peak, and forcing is
  harmless there).
The answer is kept and refreshed in the background (prefetch at start), so
the launch-options dialog never waits for hyprctl / kscreen-doctor.
Elsewhere (GNOME reports HDR right; X11 and SDR monitors can't show it)
PROTON_ENABLE_HDR=1 alone. Both are written together: a Proton that doesn't
know PROTON_FORCE_HDR (Valve's) goes on the ENABLE one.
"""

import json
import os
import subprocess
import threading
import time
from typing import List, Optional

from plunderhub.core.utils import which
from plunderhub.logging_setup import logger

FORCE_HDR_KEY = "PROTON_FORCE_HDR"

_TTL = 30.0          # the launch-options dialog asks on every redraw; a desktop setting rarely changes
_lock = threading.Lock()
_cached: Optional[tuple] = None    # (monotonic time, answer)


def _run_json(cmd: List[str]):
    if not which(cmd[0]):
        return None
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug(f"{cmd[0]} failed: {e}")
        return None
    if done.returncode != 0:
        return None
    try:
        return json.loads(done.stdout)
    except ValueError:
        return None


def _hyprland() -> bool:
    monitors = _run_json(["hyprctl", "-j", "monitors"])
    if not isinstance(monitors, list):
        return False
    option = _run_json(["hyprctl", "-j", "getoption", "render:cm_auto_hdr"])
    auto = option.get("int", 0) if isinstance(option, dict) else 0
    enabled = [m for m in monitors if isinstance(m, dict) and not m.get("disabled")]
    mon = next((m for m in enabled if m.get("focused")), enabled[0] if enabled else None)
    if mon is None:
        return False
    if str(mon.get("colorManagementPreset", "")).lower() in ("hdr", "hdredid"):
        return True
    hardware = mon.get("hardwareDetails") if isinstance(mon.get("hardwareDetails"), dict) else {}
    # older Hyprland doesn't say: cm_auto_hdr set on purpose is taken as meant for an HDR monitor
    return isinstance(auto, int) and auto > 0 and bool(hardware.get("hdr", True))


def _kde() -> bool:
    data = _run_json(["kscreen-doctor", "-j"])
    outputs = data.get("outputs") if isinstance(data, dict) else None
    if not isinstance(outputs, list):
        return False
    live = [o for o in outputs if isinstance(o, dict) and o.get("connected", True) and o.get("enabled", True)]
    if not live:
        return False
    # priority 1 = the primary monitor (KDE's display settings)
    primary = min(live, key=lambda o: o.get("priority") if isinstance(o.get("priority"), int) else 99)
    return primary.get("hdr") is True


def _detect() -> bool:
    if not os.environ.get("WAYLAND_DISPLAY"):
        return False          # X11: no desktop HDR to force into
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return _hyprland()
    desktop = (os.environ.get("XDG_CURRENT_DESKTOP", "") + ":" + os.environ.get("DESKTOP_SESSION", "")).lower()
    if "kde" in desktop or "plasma" in desktop:
        return _kde()
    return False


_refreshing = False


def _ask() -> bool:
    try:
        return _detect()
    except Exception as e:     # never let a desktop query break saving launch options
        logger.debug(f"HDR detection failed: {e}")
        return False


def _refresh() -> None:
    global _cached, _refreshing
    answer = _ask()
    with _lock:
        _cached = (time.monotonic(), answer)
        _refreshing = False


def prefetch() -> None:
    """Asks the desktop in the background (at start), so the first dialog doesn't."""
    global _refreshing
    with _lock:
        if _refreshing:
            return
        _refreshing = True
    threading.Thread(target=_refresh, daemon=True, name="plunderhub-hdr-detect").start()


def needs_force_hdr() -> bool:
    """True where PROTON_FORCE_HDR=1 should go with PROTON_ENABLE_HDR=1.
    A known answer comes back at once (an old one is refreshed in the
    background); only the very first question waits for the desktop."""
    global _cached
    with _lock:
        cached = _cached
    if cached is not None:
        if time.monotonic() - cached[0] >= _TTL:
            prefetch()
        return cached[1]
    answer = _ask()
    with _lock:
        _cached = (time.monotonic(), answer)
    return answer


def reset_cache() -> None:
    global _cached
    with _lock:
        _cached = None
