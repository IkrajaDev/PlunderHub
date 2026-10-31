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

"""MangoHud, gamescope, vkBasalt and OBS's game capture for Flatpak
launchers. A Flatpak Steam (or Heroic, Lutris...) can't see the ones
installed on the system: they come from Flathub as Vulkan layer
extensions of the launcher's runtime, one per runtime version - so
which ones a launcher needs is read from its runtime."""

import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from plunderhub.core.scan_paths import LAUNCHER_FLATPAK_IDS

EXTENSION_POINT = "org.freedesktop.Platform.VulkanLayer"


@dataclass(frozen=True)
class Layer:
    id: str         # the switch it serves in Launch options (or "obsvkcapture")
    name: str
    icon: str
    ext: str        # the extension's id on Flathub


LAYERS: List[Layer] = [
    Layer("mangohud", "MangoHud", "gauge-symbolic", f"{EXTENSION_POINT}.MangoHud"),
    Layer("gamescope", "gamescope", "scaling-symbolic", f"{EXTENSION_POINT}.gamescope"),
    Layer("vkbasalt", "vkBasalt", "sparkles-symbolic", f"{EXTENSION_POINT}.vkBasalt"),
    Layer("obsvkcapture", "OBS game capture", "video-symbolic", f"{EXTENSION_POINT}.OBSVkCapture"),
]
LAYERS_BY_ID: Dict[str, Layer] = {layer.id: layer for layer in LAYERS}


def _run(cmd: List[str], timeout: int = 15) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"})
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _extension_version(metadata: str) -> Optional[str]:
    """The version an [Extension ...VulkanLayer] section asks for."""
    inside = False
    for line in metadata.splitlines():
        line = line.strip()
        if line.startswith("["):
            inside = line == f"[Extension {EXTENSION_POINT}]"
            continue
        if inside and "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
            if key == "version":
                return value
            if key == "versions":
                return value.split(";")[0].strip()
    return None


def _branch(app_id: str) -> Optional[str]:
    """The VulkanLayer version the app's sandbox loads - its own
    metadata may name one, else its runtime's does."""
    version = _extension_version(_run(["flatpak", "info", "--show-metadata", app_id]))
    if version:
        return version
    runtime = _run(["flatpak", "info", "--show-runtime", app_id]).strip()
    if not runtime:
        return None
    return _extension_version(_run(["flatpak", "info", "--show-metadata", f"runtime/{runtime}"]))


def flatpak_launchers() -> List[Tuple[str, str, str]]:
    """(launcher name, app id, layer version) of every launcher that's
    installed as a Flatpak."""
    installed = set(_run(["flatpak", "list", "--app", "--columns=application"]).split())
    found = []
    for name, ids in LAUNCHER_FLATPAK_IDS.items():
        for app_id in ids:
            if app_id in installed:
                branch = _branch(app_id)
                if branch:
                    found.append((name, app_id, branch))
                break
    return found


def installed_refs() -> Set[str]:
    """System and per-user, as "runtime/<id>/<arch>/<branch>"."""
    out = _run(["flatpak", "list", "--runtime", "--columns=ref"])
    return {f"runtime/{line.strip()}" for line in out.splitlines() if line.strip()}


def ref(layer: Layer, branch: str) -> str:
    return f"runtime/{layer.ext}/x86_64/{branch}"


def on_flathub(full_ref: str) -> bool:
    """Whether Flathub has the ref - a layer may not be built yet for the
    newest runtime a launcher already uses. True when it can't be told
    (no connection, no remote yet): the install says why, then."""
    try:
        proc = subprocess.run(["flatpak", "remote-info", "--system", "flathub", full_ref],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return proc.returncode == 0 or "No such ref" not in proc.stderr + proc.stdout


def needed_refs(layer: Layer, launchers=None) -> List[str]:
    launchers = flatpak_launchers() if launchers is None else launchers
    return sorted({ref(layer, branch) for _name, _app, branch in launchers})


def missing_for_app(app_id: str) -> Dict[str, bool]:
    """Launch options for a game of a Flatpak launcher: which switches'
    layers its sandbox lacks (GameMode needs none - it goes through a
    portal)."""
    branch = _branch(app_id)
    if not branch:
        return {}
    have = installed_refs()
    return {layer.id: ref(layer, branch) not in have for layer in LAYERS if layer.id != "obsvkcapture"}
