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


import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from plunderhub.core.utils import which


FAUGUS_DATA_DIR_CANDIDATES: List[Path] = [
    Path.home() / ".local/share/faugus-launcher",
    Path.home() / ".var/app/io.github.Faugus.faugus-launcher/data/faugus-launcher",
]

# Flathub's Steam runs with --persist=., so its sandboxed home is this
# folder on the host.
FLATPAK_STEAM_HOME = Path.home() / ".var/app/com.valvesoftware.Steam"

STEAM_CANDIDATES = [
    Path.home() / ".local/share/Steam",
    Path.home() / ".steam/steam",
    Path.home() / ".steam/root",
    # Where Flathub's Steam actually keeps its data (older builds used
    # data/Steam, now a symlink to it). Its .steam/steam link, below, holds
    # a sandbox path - on the host it dangles, or points at native Steam.
    FLATPAK_STEAM_HOME / ".local/share/Steam",
    FLATPAK_STEAM_HOME / "data/Steam",
    FLATPAK_STEAM_HOME / ".steam/steam",
]

STEAM_NON_GAME_NAME_PREFIXES = (
    "steam linux runtime",
    "steamworks common redistributables",
)

STEAM_PROTON_NAME_PATTERN = re.compile(
    r"^proton\s+(\d|experimental|hotfix|easyanticheat runtime|battleye runtime)",
    re.IGNORECASE,
)

STEAM_SOUNDTRACK_NAME_PATTERN = re.compile(
    r"(?:^|[\s:\-]|\()(the\s+)?(original\s+(game\s+)?)?(soundtrack|ost)\)?\s*$",
    re.IGNORECASE,
)

HEROIC_CONFIG_ROOTS = [
    Path.home() / ".config",
    Path.home() / ".var/app/com.heroicgameslauncher.hgl/config",
    Path.home() / ".var/app/io.github.heroicgameslauncher.hgl/config",
]

LUTRIS_DATA_DIR_CANDIDATES: List[Path] = [
    Path.home() / ".local/share/lutris",
    Path.home() / ".var/app/net.lutris.Lutris/data/lutris",
]

LUTRIS_CONFIG_DIR_CANDIDATES: List[Path] = [
    Path.home() / ".config/lutris",
    Path.home() / ".var/app/net.lutris.Lutris/config/lutris",
]

BOTTLES_DATA_DIR_CANDIDATES: List[Path] = [
    Path.home() / ".local/share/bottles",
    Path.home() / ".var/app/com.usebottles.bottles/data/bottles",
]


def _first_existing_data_dir(candidates: Sequence[Path], required_name: str) -> Optional[Path]:
    for base in candidates:
        if (base / required_name).exists():
            return base
    return None


def lutris_db_path() -> Optional[Path]:
    base = _first_existing_data_dir(LUTRIS_DATA_DIR_CANDIDATES, "pga.db")
    return (base / "pga.db") if base else None


def lutris_game_config_path(config_id: str) -> Optional[Path]:
    """config_id is the game's `configpath` from pga.db - Lutris names
    each game's config "<slug>-<unix time>.yml" after it, so it can't be
    derived from the numeric game id. Lutris reads ~/.config/lutris
    when that exists and ~/.local/share/lutris otherwise (newer
    installs), so both are searched in that order."""
    if not config_id or "/" in config_id or config_id in (".", ".."):
        return None
    for base in (*LUTRIS_CONFIG_DIR_CANDIDATES, *LUTRIS_DATA_DIR_CANDIDATES):
        candidate = base / "games" / f"{config_id}.yml"
        if candidate.is_file():
            return candidate
    return None


def bottles_library_path() -> Optional[Path]:
    base = _first_existing_data_dir(BOTTLES_DATA_DIR_CANDIDATES, "library.yml")
    return (base / "library.yml") if base else None


def faugus_games_path() -> Optional[Path]:
    base = _first_existing_data_dir(FAUGUS_DATA_DIR_CANDIDATES, "games.json")
    return (base / "games.json") if base else None


def faugus_umu_path() -> Optional[Path]:
    games_path = faugus_games_path()
    if games_path is None:
        return None
    umu = games_path.parent / "umu-run"
    return umu if umu.exists() else None


FLATPAK_APP_DIRS: List[Path] = [
    Path.home() / ".local/share/flatpak/app",
    Path("/var/lib/flatpak/app"),
]


def flatpak_app_installed(app_id: str) -> bool:
    return any((d / app_id).is_dir() for d in FLATPAK_APP_DIRS)


def resolve_launcher_invocation(
    binary_name: Optional[str], flatpak_app_ids: Sequence[str]
) -> Optional[List[str]]:
    if binary_name and shutil.which(binary_name):
        return [binary_name]
    # Debian's /usr/games, when it isn't on this session's PATH
    path = which(binary_name) if binary_name else None
    if path:
        return [path]
    for app_id in flatpak_app_ids:
        if flatpak_app_installed(app_id):
            return ["flatpak", "run", app_id]
    return None


def invocation_for_data(path: Path, binary_name: Optional[str],
                        flatpak_app_ids: Sequence[str]) -> Optional[List[str]]:
    """The launcher a data folder belongs to: one inside a Flatpak's own
    ~/.var/app/<id>/ runs as that Flatpak, any other as the native
    program - None when that one isn't installed (data left behind after
    a switch): its games aren't listed, nor launched through the other."""
    text = str(path)
    for app_id in flatpak_app_ids:
        if f"/.var/app/{app_id}/" in text + "/":
            return ["flatpak", "run", app_id] if flatpak_app_installed(app_id) else None
    if binary_name and shutil.which(binary_name):
        return [binary_name]
    found = which(binary_name) if binary_name else None
    return [found] if found else None


LAUNCHER_FLATPAK_IDS: Dict[str, List[str]] = {
    "Steam": ["com.valvesoftware.Steam"],
    "Heroic": ["com.heroicgameslauncher.hgl", "io.github.heroicgameslauncher.hgl"],
    "Faugus": ["io.github.Faugus.faugus-launcher"],
    "Lutris": ["net.lutris.Lutris"],
    "Bottles": ["com.usebottles.bottles"],
}
