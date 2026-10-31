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

"""The tools Shipwright installs: what Launch options switches on
(GameMode, MangoHud, gamescope, vkBasalt) and the helpers around Proton
and Wine. They're installed the way launchers are (ui/launcher_tasks),
so each says which package it is on every distribution."""

import glob
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from plunderhub.core.utils import which
from plunderhub.launchers.registry import NativeRecipe

GROUPS = ("performance", "graphics", "proton", "controllers", "apps")


@dataclass(frozen=True)
class ToolDef:
    """id         the key Launch options uses (missing_tools())
    binary     what's on PATH once it's installed - or None for a
               Vulkan layer, found by `layers` instead
    gui        has a window of its own, so Shipwright offers Open
    gui_args   added to the command that opens that window
    extras     family -> the 32-bit build that 32-bit games need, put on
               alongside when the repositories have it (Arch's multilib
               lib32-*, Fedora's .i686) and taken off with it
    by_package installed is known only from the package (udev rules,
               kernel drivers) - there's no binary or layer to find
    headers    a DKMS kernel driver: needs the running kernel's headers
    post_install / pre_remove  root_helper actions run around the
               package (LACT's daemon)
    ready_key  what the finished install says, instead of the default
    opens      the window to open when binary has none of its own (a
               command-line program with a separate interface), first found
    removable  False for shared libraries other software may need too
    arch       the pacman package - looked up in the AUR when the
               repositories don't have it
    recipes    distribution family -> NativeRecipe
    flatpak    Flathub app ids (only for apps - MangoHud and gamescope
               on Flathub are extensions for Flatpak launchers)"""
    id: str
    name: str
    group: str
    icon: str
    binary: Optional[str]
    arch: str
    recipes: Dict[str, NativeRecipe] = field(default_factory=dict)
    flatpak: Tuple[str, ...] = ()
    gui: bool = False
    gui_args: Tuple[str, ...] = ()
    layers: Tuple[str, ...] = ()
    extras: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    by_package: bool = False
    headers: bool = False
    post_install: Tuple[str, ...] = ()
    pre_remove: Tuple[str, ...] = ()
    ready_key: str = ""
    removable: bool = True
    opens: Tuple[str, ...] = ()


def _same(*packages: str, **extra) -> Dict[str, NativeRecipe]:
    """The same package name on Debian, Fedora and openSUSE."""
    return {family: NativeRecipe(packages, **extra) for family in ("debian", "fedora", "suse")}


TOOLS: List[ToolDef] = [
    ToolDef("gamemode", "GameMode", "performance", "zap-symbolic", "gamemoderun", "gamemode",
            _same("gamemode"), extras={"arch": ("lib32-gamemode",), "fedora": ("gamemode.i686",)}),
    ToolDef("mangohud", "MangoHud", "performance", "gauge-symbolic", "mangohud", "mangohud",
            _same("mangohud"), extras={"arch": ("lib32-mangohud",), "fedora": ("mangohud.i686",)}),
    ToolDef("gamescope", "gamescope", "performance", "scaling-symbolic", "gamescope", "gamescope",
            _same("gamescope")),
    ToolDef("vkbasalt", "vkBasalt", "graphics", "sparkles-symbolic", None, "vkbasalt",
            {"debian": NativeRecipe(("vkbasalt",)), "fedora": NativeRecipe(("vkBasalt",)),
             "suse": NativeRecipe(("vkbasalt",))},
            layers=("vkBasalt*.json", "vkbasalt*.json"),
            extras={"arch": ("lib32-vkbasalt",), "fedora": ("vkBasalt.i686",)}),
    ToolDef("goverlay", "GOverlay", "graphics", "preferences-other-symbolic", "goverlay", "goverlay",
            _same("goverlay"), flatpak=("io.github.benjamimgois.goverlay",), gui=True),
    ToolDef("protonplus", "ProtonPlus", "proton", "system-software-install-symbolic", "protonplus", "protonplus",
            {"fedora": NativeRecipe(("protonplus",), copr="wehagy/protonplus")},
            flatpak=("com.vysp3r.ProtonPlus",), gui=True),
    ToolDef("protontricks", "Protontricks", "proton", "wand-sparkles-symbolic", "protontricks", "protontricks",
            _same("protontricks"), flatpak=("com.github.Matoking.protontricks",), gui=True, gui_args=("--gui",)),
    ToolDef("winetricks", "Winetricks", "proton", "wine-symbolic", "winetricks", "winetricks",
            _same("winetricks"), gui=True),
    # The 32-bit libraries Wine games load (what CachyOS's gaming meta
    # package and Lutris's wiki list): sound, video, fonts, TLS. Each
    # distribution's names, t64 and older both - only those the
    # repositories have are installed.
    ToolDef("winedeps", "Wine dependencies", "proton", "package-symbolic", None, "lib32-alsa-plugins",
            {"debian": NativeRecipe(("libasound2-plugins:i386",)),
             "fedora": NativeRecipe(("alsa-plugins-pulseaudio.i686",)),
             "suse": NativeRecipe(("alsa-plugins-pulse-32bit",))},
            extras={
                "arch": ("lib32-gtk3", "lib32-gnutls", "lib32-libpulse", "lib32-libva", "lib32-libjpeg-turbo",
                         "lib32-opencl-icd-loader", "lib32-gst-plugins-base-libs", "lib32-openal", "lib32-mpg123",
                         "lib32-giflib", "lib32-sdl2-compat"),
                "debian": ("libgtk-3-0t64:i386", "libgtk-3-0:i386", "libgnutls30t64:i386", "libgnutls30:i386",
                           "libpulse0:i386", "libva2:i386", "libjpeg62-turbo:i386", "libjpeg-turbo8:i386",
                           "ocl-icd-libopencl1:i386", "libgstreamer-plugins-base1.0-0:i386", "libopenal1:i386",
                           "libmpg123-0t64:i386", "libmpg123-0:i386", "libgif7:i386", "libsdl2-2.0-0:i386"),
                "fedora": ("gtk3.i686", "gnutls.i686", "pulseaudio-libs.i686", "libva.i686", "libjpeg-turbo.i686",
                           "ocl-icd.i686", "gstreamer1-plugins-base.i686", "openal-soft.i686", "mpg123-libs.i686",
                           "giflib.i686", "sdl2-compat.i686", "SDL2.i686"),
                "suse": ("libgtk-3-0-32bit", "libgnutls30-32bit", "libpulse0-32bit", "libva2-32bit",
                         "libjpeg8-32bit", "libOpenCL1-32bit", "libgstreamer-1_0-0-32bit", "libopenal1-32bit",
                         "libmpg123-0-32bit", "libgif7-32bit", "libSDL2-2_0-0-32bit"),
            },
            by_package=True, removable=False, ready_key="tool_ready_text_winedeps"),
    # Arch only - Fedora, Debian and openSUSE don't package it
    ToolDef("umu", "umu-launcher", "proton", "rocket-symbolic", "umu-run", "umu-launcher"),
    ToolDef("gamedevices", "game-devices-udev", "controllers", "usb-symbolic", None, "game-devices-udev",
            {"debian": NativeRecipe(("steam-devices",)), "fedora": NativeRecipe(("steam-devices",)),
             "suse": NativeRecipe(("steam-devices",))},
            by_package=True, post_install=("udev-reload",), ready_key="tool_ready_text_controller"),
    ToolDef("xpadneo", "xpadneo", "controllers", "bluetooth-symbolic", None, "xpadneo-dkms",
            by_package=True, headers=True, ready_key="tool_ready_text_driver"),
    ToolDef("xone", "xone", "controllers", "input-gaming-symbolic", None, "xone-dkms",
            extras={"arch": ("xone-dongle-firmware",)},
            by_package=True, headers=True, ready_key="tool_ready_text_driver"),
    ToolDef("lact", "LACT", "apps", "fan-symbolic", "lact", "lact",
            {"fedora": NativeRecipe(("lact",), copr="ilyaz/LACT")},
            gui=True, post_install=("enable-lactd",), pre_remove=("disable-lactd",)),
    ToolDef("corectrl", "CoreCtrl", "apps", "cpu-symbolic", "corectrl", "corectrl",
            {"debian": NativeRecipe(("corectrl",)), "fedora": NativeRecipe(("corectrl",))}, gui=True),
    ToolDef("obs", "OBS Studio", "apps", "video-symbolic", "obs", "obs-studio",
            _same("obs-studio"), flatpak=("com.obsproject.Studio",), gui=True),
    # The recorder is a command-line program; its window comes separately:
    # Arch's and the COPR's ShadowPlay-like overlay (gsr-ui), openSUSE's
    # GTK one. Debian (only from forky on) and Ubuntu don't package it,
    # Leap neither - there it's the Flathub app, which carries both.
    ToolDef("gsr", "GPU Screen Recorder", "apps", "monitor-dot-symbolic", "gpu-screen-recorder",
            "gpu-screen-recorder",
            {"fedora": NativeRecipe(("gpu-screen-recorder",), copr="brycensranch/gpu-screen-recorder-git"),
             "suse": NativeRecipe(("gpu-screen-recorder",))},
            flatpak=("com.dec05eba.gpu_screen_recorder",), gui=True,
            extras={"arch": ("gpu-screen-recorder-ui",), "fedora": ("gpu-screen-recorder-ui",),
                    "suse": ("gpu-screen-recorder-gtk",)},
            opens=("gsr-ui", "gpu-screen-recorder-gtk")),
]

TOOLS_BY_ID: Dict[str, ToolDef] = {t.id: t for t in TOOLS}
TOOLS_BY_NAME: Dict[str, ToolDef] = {t.name: t for t in TOOLS}

_LAYER_DIRS = ("/usr/share/vulkan/implicit_layer.d", "/usr/share/vulkan/explicit_layer.d",
               "/etc/vulkan/implicit_layer.d", "/etc/vulkan/explicit_layer.d")


def installed_path(tool: ToolDef) -> Optional[str]:
    """The binary or Vulkan layer file that shows the tool is installed
    natively - its owning package is what gets removed."""
    if tool.binary:
        return which(tool.binary)
    for directory in _LAYER_DIRS:
        for pattern in tool.layers:
            hits = sorted(glob.glob(f"{directory}/{pattern}"))
            if hits:
                return hits[0]
    return None


# --- updates ---------------------------------------------------------------------

def _out(cmd: List[str], timeout: int = 60) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass
class Update:
    """A newer version waiting: of a package (aur when it's an AUR one) or
    of a Flatpak app."""
    name: str
    old: str
    new: str
    flatpak: bool = False
    aur: bool = False


def full_upgrade_only(family: str) -> bool:
    """Arch (and so CachyOS, EndeavourOS...) and openSUSE Tumbleweed don't
    support updating single packages - only the whole system is safe."""
    if family == "arch":
        return True
    if family == "suse":
        try:
            text = open("/etc/os-release", encoding="utf-8", errors="ignore").read().lower()
        except OSError:
            return False
        return "tumbleweed" in text or "slowroll" in text
    return False


def upgradable(family: str) -> Dict[str, Update]:
    """Every installed package with a newer version, from what the
    package manager already knows (nothing is synced as root). Slow -
    call it off the GTK thread."""
    found: Dict[str, Update] = {}
    if family == "arch":
        # checkupdates syncs a private copy of the databases; pacman -Qu
        # only knows as much as the last system sync
        out = _out(["checkupdates"]) if shutil.which("checkupdates") else _out(["pacman", "-Qu"])
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] == "->":
                found[parts[0]] = Update(parts[0], parts[1], parts[3])
        helper = next((h for h in ("paru", "yay") if shutil.which(h)), None)
        if helper:
            for line in _out([helper, "-Qua"]).splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[2] == "->":
                    found[parts[0]] = Update(parts[0], parts[1], parts[3], aur=True)
    elif family == "debian":
        # "mangohud/noble-updates 0.7.2-1 amd64 [upgradable from: 0.7.1-1]"
        for line in _out(["apt", "list", "--upgradable"]).splitlines():
            if "/" not in line or "[upgradable from:" not in line:
                continue
            name = line.split("/")[0]
            parts = line.split()
            old = line.rsplit("[upgradable from:", 1)[1].strip(" ]")
            found[name] = Update(name, old, parts[1] if len(parts) > 1 else "")
    elif family == "fedora":
        for line in _out(["dnf", "-q", "-C", "check-update"]).splitlines():
            parts = line.split()
            if len(parts) >= 3 and "." in parts[0] and not line.startswith(" "):
                name = parts[0].rsplit(".", 1)[0]
                found[name] = Update(name, "", parts[1])
    elif family == "suse":
        for line in _out(["zypper", "--no-refresh", "-q", "list-updates"]).splitlines():
            cols = [c.strip() for c in line.split("|")]
            if len(cols) >= 5 and cols[0] == "v":
                found[cols[2]] = Update(cols[2], cols[3], cols[4])
    return found


def flatpak_updates() -> Dict[str, Update]:
    if not shutil.which("flatpak"):
        return {}
    have = {}
    for line in _out(["flatpak", "list", "--app", "--columns=application,version"]).splitlines():
        parts = line.split("\t")
        if parts and parts[0]:
            have[parts[0].strip()] = parts[1].strip() if len(parts) > 1 else ""
    found = {}
    for line in _out(["flatpak", "remote-ls", "--updates", "--app", "--columns=application,version"]).splitlines():
        parts = line.split("\t")
        app_id = parts[0].strip() if parts else ""
        if app_id in have:
            found[app_id] = Update(app_id, have[app_id], parts[1].strip() if len(parts) > 1 else "", flatpak=True)
    return found


def pending_updates(family: str, packages: Dict[str, str], flatpaks: Dict[str, str]):
    """(the updates of these tools / launchers - key -> Update -, every
    upgradable package on the system). packages / flatpaks: key -> its
    installed package / Flatpak app id. Slow (network) - off the GTK
    thread."""
    system = upgradable(family) if packages else {}
    found: Dict[str, Update] = {key: system[pkg] for key, pkg in packages.items() if pkg in system}
    if family == "fedora":
        for update in found.values():
            update.old = _out(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", update.name]).strip()
    if flatpaks:
        apps = flatpak_updates()
        found.update({key: apps[app_id] for key, app_id in flatpaks.items() if app_id in apps})
    return found, system


def reboot_needed(family: str, kernel: str) -> bool:
    """After an update: the running kernel's modules are gone (Arch
    replaces them), or the distribution says so."""
    if not os.path.isdir(f"/usr/lib/modules/{kernel}") and not os.path.isdir(f"/lib/modules/{kernel}"):
        return True
    if family == "debian":
        return os.path.exists("/var/run/reboot-required")
    if family == "fedora":
        try:
            return subprocess.run(["dnf", "needs-restarting", "-r"], capture_output=True, timeout=60).returncode == 1
        except (OSError, subprocess.SubprocessError):
            return False
    if family == "suse":
        try:
            return subprocess.run(["zypper", "needs-rebooting"], capture_output=True, timeout=60).returncode == 102
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def snapshots_on() -> bool:
    """snapper (CachyOS, openSUSE) takes a snapshot around every update."""
    return bool(shutil.which("snapper")) and os.path.exists("/etc/snapper/configs/root")
