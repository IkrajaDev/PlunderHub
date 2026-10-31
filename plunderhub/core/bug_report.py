# -*- coding: utf-8 -*-
#
# PlunderHub - a unified game launcher for Linux
# Copyright (C) 2026 Ikraja
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""What a bug report needs from this computer: the system in a few lines,
the end of the log, and the GitHub link that opens the bug form with the
system already filled in. Everything goes through privacy.scrub."""

import os
import platform
import re
import subprocess
from collections import deque
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

from plunderhub.constants import APP_CHANNEL, APP_VERSION, BUG_REPORT_URL, LOG_FILE
from plunderhub.core.scan_paths import LAUNCHER_FLATPAK_IDS, flatpak_app_installed
from plunderhub.core.utils import which
from plunderhub.launchers.registry import LAUNCHER_REGISTRY
from plunderhub.privacy import scrub

BUG_FORM = "bug_report.yml"
LOG_TAIL_LINES = 300
# GitHub refuses very long links; the system lines are far under this
_MAX_URL = 7000

_INSTALL_NAMES = {
    "pacman": "package (makepkg / AUR)",
    "script": "install script",
    "git": "git checkout",
}

_GPU_VENDORS = {"0x1002": "AMD", "0x10de": "NVIDIA", "0x8086": "Intel"}


def _os_release() -> Dict[str, str]:
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        info = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                info[key.strip()] = value.strip().strip('"')
        return info
    return {}


def distro() -> str:
    info = _os_release()
    name = info.get("PRETTY_NAME") or " ".join(
        v for v in (info.get("NAME"), info.get("VERSION")) if v)
    return name or "unknown"


# Compositors and desktops that leave XDG_CURRENT_DESKTOP unset when
# started by hand (sway from a console, ...) still say who they are here.
_DESKTOP_SIGNS = (
    ("HYPRLAND_INSTANCE_SIGNATURE", "Hyprland"), ("SWAYSOCK", "sway"), ("NIRI_SOCKET", "niri"),
    ("I3SOCK", "i3"), ("WAYFIRE_SOCKET", "Wayfire"), ("KDE_FULL_SESSION", "KDE"),
    ("GNOME_DESKTOP_SESSION_ID", "GNOME"), ("MATE_DESKTOP_SESSION_ID", "MATE"),
    ("CINNAMON_VERSION", "X-Cinnamon"), ("LABWC_PID", "labwc"), ("RIVER_INIT", "river"),
    ("GAMESCOPE_WAYLAND_DISPLAY", "gamescope"),
)


def desktop() -> str:
    for var in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
        value = os.environ.get(var, "").strip()
        if value:
            return os.path.basename(value.rstrip("/")).replace(":", " / ")
    for var, name in _DESKTOP_SIGNS:
        if os.environ.get(var):
            return name
    return "unknown"


def session() -> str:
    """The session type, and what GTK draws through when it differs
    (an X11 app on Wayland runs through XWayland)."""
    kind = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if kind not in ("wayland", "x11"):
        kind = "wayland" if os.environ.get("WAYLAND_DISPLAY") else ("x11" if os.environ.get("DISPLAY") else "")
    text = {"wayland": "Wayland", "x11": "X11"}.get(kind, kind or "unknown")
    try:
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        backend = type(display).__name__ if display is not None else ""
        if "X11" in backend and kind == "wayland":
            text += " (app via XWayland)"
        elif "Wayland" in backend and kind == "x11":
            text += " (app via Wayland)"
    except Exception:
        pass
    return text


_PCI_IDS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids",
            "/run/host/usr/share/hwdata/pci.ids")


def _pci_names(wanted: List[Tuple[str, str]]) -> Dict[Tuple[str, str], str]:
    """(vendor, device) -> "Vendor Device name" from the pci.ids list."""
    wanted = {(v.lower(), d.lower()) for v, d in wanted}
    vendors = {v for v, _d in wanted}
    names: Dict[Tuple[str, str], str] = {}
    for path in _PCI_IDS:
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            vendor, vendor_name = None, ""
            for line in handle:
                if line.startswith("#") or not line.strip():
                    continue
                if line.startswith("C "):
                    break  # the device classes follow the vendors
                if not line.startswith("\t"):
                    vendor, _, vendor_name = line.strip().partition("  ")
                    vendor = vendor if vendor in vendors else None
                elif vendor and not line.startswith("\t\t"):
                    device, _, device_name = line.strip().partition("  ")
                    if (vendor, device) in wanted:
                        short = vendor_name.split(" [")[-1].rstrip("]") if "[" in vendor_name else vendor_name
                        names[(vendor, device)] = f"{short} {device_name}"
        break
    return names


_GL_INFO: Optional[Tuple[str, str]] = None


def _gl_renderer() -> Tuple[str, str]:
    """(renderer, driver version) of the GPU drawing the desktop, from
    glxinfo when it is installed - ("", "") otherwise."""
    global _GL_INFO
    if _GL_INFO is not None:
        return _GL_INFO
    renderer = version = ""
    if which("glxinfo"):
        try:
            out = subprocess.run(["glxinfo", "-B"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        for line in out.splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key == "OpenGL renderer string" and not renderer:
                renderer = value.split(" (")[0].split("/PCIe")[0].strip()
                renderer = re.sub(r"^Mesa ", "", renderer)
            elif key in ("OpenGL version string", "OpenGL core profile version string") and not version:
                match = re.search(r"(Mesa|NVIDIA) (\S+)", value)
                if match:
                    version = f"{match.group(1)} {match.group(2)}"
    _GL_INFO = (renderer, version)
    return _GL_INFO


def gpus() -> str:
    """Every graphics card by its name and kernel driver; the one drawing
    the desktop by its exact name with the Mesa / NVIDIA version."""
    cards = []
    drm = Path("/sys/class/drm")
    try:
        entries = sorted(p for p in drm.iterdir() if re.fullmatch(r"card\d+", p.name))
    except OSError:
        entries = []
    seen = set()
    for card in entries:
        dev = card / "device"
        try:
            vendor = (dev / "vendor").read_text().strip().lower().replace("0x", "")
            device = (dev / "device").read_text().strip().lower().replace("0x", "")
        except OSError:
            continue
        try:
            slot = os.path.basename(os.path.realpath(dev))
        except OSError:
            slot = card.name
        if slot in seen:
            continue
        seen.add(slot)
        try:
            driver = os.path.basename(os.readlink(dev / "driver"))
        except OSError:
            driver = ""
        cards.append((vendor, device, driver))
    names = _pci_names([(v, d) for v, d, _drv in cards])
    renderer, version = _gl_renderer()
    parts = []
    for vendor, device, driver in cards:
        name = names.get((vendor, device)) or f"{_GPU_VENDORS.get('0x' + vendor, vendor)} [{vendor}:{device}]"
        details = [driver] if driver else []
        brand = _GPU_VENDORS.get("0x" + vendor, "").lower()
        low = renderer.lower()
        if renderer and brand and (brand in low or (brand == "amd" and "radeon" in low)):
            # the card drawing the desktop: its exact name and driver version
            name = renderer
            if version:
                details.append(version)
            renderer = ""
        if driver == "nvidia" and not any(d.startswith("NVIDIA") for d in details):
            try:
                details.append(Path("/sys/module/nvidia/version").read_text().strip())
            except OSError:
                pass
        parts.append(f"{name} ({', '.join(details)})" if details else name)
    if not parts and renderer:
        parts.append(f"{renderer} ({version})" if version else renderer)
    return " + ".join(parts) or "unknown"


def monitors(window=None) -> str:
    """Each monitor as GTK lays out on it (logical size), its refresh rate
    and scale; "app" marks the one the window is on."""
    try:
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display is None:
            return "unknown"
        here = None
        gdk_window = window.get_window() if window is not None else None
        if gdk_window is not None:
            here = display.get_monitor_at_window(gdk_window)
        parts = []
        for i in range(display.get_n_monitors()):
            monitor = display.get_monitor(i)
            geo = monitor.get_geometry()
            text = f"{geo.width}x{geo.height}"
            rate = monitor.get_refresh_rate()
            if rate:
                text += f" @{round(rate / 1000)} Hz"
            text += f", scale {monitor.get_scale_factor()}"
            if here is not None and monitor == here and display.get_n_monitors() > 1:
                text += " (app)"
            parts.append(text)
        return " + ".join(parts) or "unknown"
    except Exception:
        return "unknown"


def look() -> str:
    """The system's GTK theme and text scale (big fonts change layouts)."""
    try:
        from gi.repository import Gtk
        settings = Gtk.Settings.get_default()
        theme = settings.get_property("gtk-theme-name") or "?"
        dark = settings.get_property("gtk-application-prefer-dark-theme")
        try:
            # the system's own, from before the app's theme replaced it
            from plunderhub.theme import _capture_original_theme_preference
            orig_dark, orig_name = _capture_original_theme_preference()
            theme, dark = orig_name or theme, orig_dark
        except Exception:
            pass
        if dark:
            theme += " (dark)"
        dpi = settings.get_property("gtk-xft-dpi")
        scale = f"{dpi / 1024 / 96:.2f}" if dpi and dpi > 0 else "1.00"
        font = settings.get_property("gtk-font-name") or "?"
        animations = "on" if settings.get_property("gtk-enable-animations") else "off"
        return f"{theme}, font {font}, text scale {scale}, animations {animations}"
    except Exception:
        return "unknown"


def cpu_ram() -> str:
    cpu = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text(errors="ignore").splitlines():
            if line.startswith("model name"):
                cpu = " ".join(line.partition(":")[2].split())
                break
        cpu += f" ({os.cpu_count()} threads)"
    except OSError:
        pass
    ram = ""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                ram = f", {int(line.split()[1]) / 1024 / 1024:.1f} GB RAM"
                break
    except (OSError, ValueError, IndexError):
        pass
    return cpu + ram


def install_method() -> str:
    if Path("/.flatpak-info").exists():
        return "Flatpak"
    try:
        from plunderhub.core.self_update import detect_install_method
        method = detect_install_method()
    except Exception:
        return "unknown"
    return _INSTALL_NAMES.get(method, "other")


def _run(cmd: List[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _flatpak_versions() -> Dict[str, str]:
    if not which("flatpak"):
        return {}
    versions = {}
    for line in _run(["flatpak", "list", "--app", "--columns=application,version"]).splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1].strip():
            versions[parts[0].strip()] = parts[1].strip()
    return versions


def _package_version(path: str) -> str:
    """The version of the distro package that owns path (pacman, dpkg or
    rpm) - "" when none does (an AppImage, a hand-made install)."""
    candidates = [path, os.path.realpath(path)]
    if path.startswith("/usr/"):
        candidates.append(path[4:])  # dpkg on merged-/usr systems lists /bin/...
    for candidate in dict.fromkeys(candidates):
        if which("pacman"):
            out = _run(["pacman", "-Qo", candidate]).split()
            if len(out) >= 2 and "owned" in out:
                return out[-1]
        if which("dpkg-query"):
            owner = _run(["dpkg", "-S", candidate]).partition(":")[0].strip()
            if owner and " " not in owner:
                version = _run(["dpkg-query", "-W", "-f=${Version}", owner]).strip()
                if version:
                    return version
        if which("xbps-query"):
            # "pkgname-1.2_1: /usr/bin/x"
            owner = _run(["xbps-query", "-o", candidate]).partition(":")[0].strip()
            match = re.match(r".+-([0-9][^-]*_[0-9]+)$", owner)
            if match:
                return match.group(1)
        if which("apk"):
            # "/usr/bin/x is owned by pkgname-1.2-r0"
            out = _run(["apk", "info", "--who-owns", candidate]).strip().split()
            if out and "owned" in out:
                match = re.match(r".+?-([0-9].*-r[0-9]+)$", out[-1])
                if match:
                    return match.group(1)
        if which("rpm"):
            out = _run(["rpm", "-qf", "--qf", "%{VERSION}-%{RELEASE}", candidate]).strip()
            if out and "not owned" not in out and " " not in out:
                return out
    return ""


def _steam_client_version(flatpak: bool) -> str:
    """The Steam client's own build (the package only holds its bootstrap)."""
    homes = [Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam"] if flatpak else [
        Path.home() / ".local/share/Steam", Path.home() / ".steam/steam"]
    for root in homes:
        try:
            text = (root / "package" / "steam_client_ubuntu12.manifest").read_text(errors="ignore")
        except OSError:
            continue
        match = re.search(r'"version"\s+"(\d+)"', text)
        if match:
            return f"client {match.group(1)}"
    return ""


def launchers(enabled: Optional[Iterable[str]] = None) -> str:
    """Each installed launcher, native or Flatpak (or both), with its version."""
    enabled = set(enabled) if enabled is not None else None
    flatpaks = None
    parts = []
    for ldef in LAUNCHER_REGISTRY:
        kinds = []
        binary = which(ldef.binary) if ldef.binary else None
        if binary:
            version = _steam_client_version(False) if ldef.name == "Steam" else ""
            version = version or _package_version(binary)
            kinds.append(f"native {version}".strip())
        for app_id in LAUNCHER_FLATPAK_IDS.get(ldef.name, []):
            if flatpak_app_installed(app_id):
                if flatpaks is None:
                    flatpaks = _flatpak_versions()
                version = (_steam_client_version(True) if ldef.name == "Steam" else "") or flatpaks.get(app_id, "")
                kinds.append(f"Flatpak {version}".strip())
                break
        if not kinds:
            continue
        text = f"{ldef.name} ({' + '.join(kinds)})"
        if enabled is not None and ldef.name not in enabled:
            text += " [off]"
        parts.append(text)
    return ", ".join(parts) or "none found"


_JUNK_DMI = ("system product name", "to be filled", "default string", "not specified", "none", "o.e.m.")
_HANDHELD_BOARDS = {"jupiter": "Steam Deck LCD", "galileo": "Steam Deck OLED"}


def _dmi(name: str) -> str:
    try:
        value = Path("/sys/devices/virtual/dmi/id", name).read_text(errors="ignore").strip()
    except OSError:
        return ""
    return "" if not value or any(j in value.lower() for j in _JUNK_DMI) else value


def device() -> str:
    """The computer's model (a laptop, a handheld) - or the mainboard of a
    self-built desktop. A model name, nothing that tells a person apart."""
    board = _dmi("board_name")
    if board.lower() in _HANDHELD_BOARDS:
        return _HANDHELD_BOARDS[board.lower()]
    vendor, product = _dmi("sys_vendor"), _dmi("product_name")
    if product:
        return f"{vendor} {product}".strip() if vendor and vendor.split()[0] not in product else product
    if board:
        return f"{_dmi('board_vendor')} {board} (mainboard)".strip()
    return "unknown"


def gamescope() -> str:
    """The installed gamescope's version (games can run in it), and whether
    PlunderHub itself runs inside one (Steam Deck game mode, a gamescope
    session)."""
    version = ""
    path = which("gamescope")  # Debian: /usr/games, often not on PATH
    if path:
        try:
            proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=3)
            match = re.search(r"gamescope version ([0-9][^\s(]*)", proc.stdout + proc.stderr)
            version = match.group(1) if match else ""
        except (OSError, subprocess.TimeoutExpired):
            pass
        # a build without its version compiled in (Fedora): the package's
        version = version or _package_version(path) or "?"
    inside = bool(os.environ.get("GAMESCOPE_WAYLAND_DISPLAY")) or "gamescope" in desktop().lower()
    game_mode = os.environ.get("SteamDeck") == "1" or os.environ.get("SteamGamepadUI") == "1"
    text = f"{version} installed" if version else "not installed"
    if inside:
        text += ", PlunderHub runs inside it" + (" (Steam game mode)" if game_mode else "")
    else:
        text += ", PlunderHub not inside it"
    return text


def controllers() -> str:
    names = []
    for js in sorted(Path("/sys/class/input").glob("js*")):
        try:
            name = (js / "device" / "name").read_text(errors="ignore").strip()
        except OSError:
            continue
        if name and name not in names:
            names.append(name)
    return ", ".join(names) or "none"


# app settings that change how things look or launch
_SETTING_KEYS = (
    ("view_mode", "view"), ("card_view_mode", "cards"), ("cover_size", "cover size"),
    ("animated_covers", "animated covers"), ("show_covers", "covers"), ("font_scale", "font"),
    ("show_bubbles", "bubbles"), ("launch_mode", "launch"), ("launch_on_next_workspace", "next workspace"),
    ("minimize_on_launch", "minimize on launch"), ("auto_close", "auto close"), ("use_tray", "tray"),
    ("console_ui_scale", "console scale"),
)


def app_settings(settings: Optional[Dict]) -> str:
    if not settings:
        return "default"
    parts = []
    for key, label in _SETTING_KEYS:
        if key in settings:
            value = settings[key]
            if isinstance(value, bool):
                value = "on" if value else "off"
            parts.append(f"{label} {value}")
    return ", ".join(parts) or "default"


def gtk_lines(window=None) -> Dict[str, str]:
    """The parts that ask GTK - read on the GTK thread, the rest can be
    read on another (system_lines(gtk=...))."""
    try:
        from gi.repository import Gtk
        gtk = f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}"
    except Exception:
        gtk = "unknown"
    return {"Session": session(), "Monitors": monitors(window), "GTK look": look(),
            "GTK / Python": f"{gtk} / {platform.python_version()}"}


def system_lines(enabled_launchers: Optional[Iterable[str]] = None,
                 extra: Iterable[Tuple[str, str]] = (), window=None,
                 gtk: Optional[Dict[str, str]] = None,
                 settings: Optional[Dict] = None) -> List[Tuple[str, str]]:
    gtk = gtk if gtk is not None else gtk_lines(window)
    lines = [
        ("PlunderHub", f"{APP_VERSION} ({APP_CHANNEL}, {install_method()})"),
        ("Distro", distro()),
        ("Kernel", platform.release() or "unknown"),
        ("Desktop", desktop()),
        ("Session", gtk["Session"]),
        ("Gamescope", gamescope()),
        ("Device", device()),
        ("GPU", gpus()),
        ("CPU / RAM", cpu_ram()),
        ("Monitors", gtk["Monitors"]),
        ("GTK look", gtk["GTK look"]),
        ("GTK / Python", gtk["GTK / Python"]),
        ("Launchers", launchers(enabled_launchers)),
        ("Controllers", controllers()),
    ]
    lines.extend(extra)
    if settings is not None:
        lines.append(("App settings", app_settings(settings)))
    return [(key, scrub(value)) for key, value in lines]


def summary(lines: List[Tuple[str, str]]) -> str:
    """One short line for the folded system box."""
    found = dict(lines)
    gpu = found.get("GPU", "").split(" (")[0].split(" + ")[-1]
    parts = [found.get("Distro", ""), f"{found.get('Desktop', '')} ({found.get('Session', '').split(' (')[0]})", gpu]
    return " · ".join(p for p in parts if p and p != "unknown")


def system_text(lines: List[Tuple[str, str]]) -> str:
    width = max(len(key) for key, _value in lines) + 1
    return "\n".join(f"{(key + ':').ljust(width)} {value}" for key, value in lines)


def log_tail(lines: int = LOG_TAIL_LINES) -> str:
    """The log's last lines, cleaned again (the log already is - this
    covers lines another program might have added)."""
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as handle:
            tail = deque(handle, maxlen=lines)
    except OSError:
        return ""
    return scrub("".join(tail)).rstrip("\n")


def issue_url(system: str) -> str:
    query = {"template": BUG_FORM, "labels": "bug", "system": system}
    url = f"{BUG_REPORT_URL}?{urlencode(query)}"
    if len(url) > _MAX_URL:
        url = f"{BUG_REPORT_URL}?{urlencode({'template': BUG_FORM, 'labels': 'bug'})}"
    return url
