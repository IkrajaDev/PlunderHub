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


from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.core.models import LauncherDef


LAUNCHER_REGISTRY: List[LauncherDef] = [
    LauncherDef("Steam", "scan_steam", "steam"),
    LauncherDef("Heroic", "scan_heroic", "heroic"),
    LauncherDef("Faugus", "scan_faugus", "faugus-launcher"),
    LauncherDef("Lutris", "scan_lutris", "lutris"),
    LauncherDef("Bottles", "scan_bottles", "bottles"),
]

ALL_LAUNCHER_NAMES: List[str] = [ldef.name for ldef in LAUNCHER_REGISTRY]

@dataclass(frozen=True)
class NativeRecipe:
    """How a launcher comes from a distribution's own packages.

    packages   tried in order - the first one the repositories have is
               installed (Debian calls Steam steam-installer, Ubuntu
               used to call it steam)
    i386       Debian/Ubuntu: the i386 architecture is added first, for
               the 32-bit libraries the launcher pulls in
    ppa        Ubuntu and its derivatives only: added when the
               repositories don't have the package
    copr       Fedora: a COPR repository, added likewise
    rpmfusion  Fedora: Steam lives in RPM Fusion, added likewise
    """
    packages: Tuple[str, ...]
    i386: bool = False
    ppa: str = ""
    copr: str = ""
    rpmfusion: bool = False


NATIVE_RECIPES: Dict[str, Dict[str, NativeRecipe]] = {
    "debian": {
        "Steam": NativeRecipe(("steam-installer", "steam"), i386=True),
        "Lutris": NativeRecipe(("lutris",)),
        "Faugus": NativeRecipe(("faugus-launcher",), i386=True, ppa="faugus/faugus-launcher"),
    },
    "fedora": {
        "Steam": NativeRecipe(("steam",), rpmfusion=True),
        "Lutris": NativeRecipe(("lutris",)),
        "Faugus": NativeRecipe(("faugus-launcher",), copr="faugus/faugus-launcher"),
    },
    "suse": {
        "Steam": NativeRecipe(("steam",)),
        "Lutris": NativeRecipe(("lutris",)),
    },
}

ARCH_AUR_PACKAGES: Dict[str, str] = {
    "Steam": "steam",
    "Heroic": "heroic-games-launcher-bin",
    "Faugus": "faugus-launcher",
    "Lutris": "lutris",
    "Bottles": "bottles",
}

# The package manager behind NATIVE_RECIPES, named in the install-method
# dialog.
NATIVE_PACKAGE_TOOLS: Dict[str, str] = {
    "debian": "apt",
    "fedora": "dnf",
    "suse": "zypper",
}

# The launchers' logos bundled with PlunderHub (assets/launchers) - used
# before the icon theme, so they're there even when the launcher isn't.
LAUNCHER_ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "launchers"


def bundled_launcher_icon(name: str) -> Optional[Path]:
    path = LAUNCHER_ICON_DIR / f"{name.lower()}.svg"
    return path if path.is_file() else None


# Icon-theme names, native first, then the Flatpak's app id - the
# fallback for a launcher without a bundled logo.
LAUNCHER_ICON_NAMES: Dict[str, List[str]] = {
    "Steam": ["steam", "com.valvesoftware.Steam"],
    "Heroic": ["heroic", "com.heroicgameslauncher.hgl"],
    "Faugus": ["faugus-launcher", "io.github.Faugus.faugus-launcher"],
    "Lutris": ["lutris", "net.lutris.Lutris"],
    "Bottles": ["com.usebottles.bottles", "bottles"],
}

LAUNCHER_SEARCH_NAMES: Dict[str, str] = {
    "Steam": "Steam",
    "Heroic": "Heroic Games Launcher",
    "Faugus": "Faugus Launcher",
    "Lutris": "Lutris",
    "Bottles": "Bottles",
}
