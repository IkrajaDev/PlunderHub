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

"""The game's Wine prefix and the three tools Launch options opens on it:
winecfg, Winetricks (Protontricks for a Steam game) and running an .exe
inside it - the way Heroic's Wine tab offers them.

Each launcher's backend (core/launch_options.py) says where the prefix
is and which Wine runs it, as a PrefixInfo; command() turns that into
what to run. Three ways to get there:

    steam  Protontricks finds the prefix and the Proton from the App ID
    wine   a Wine binary (a Proton build's own, or a Wine runner's) with
           WINEPREFIX - what Heroic does for its Winetricks button
    umu    umu-run with WINEPREFIX / PROTONPATH, as Faugus runs its games
"""
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.core.scan_paths import flatpak_app_installed
from plunderhub.core.utils import which
from plunderhub.logging_setup import logger

PROTONTRICKS_FLATPAK = "com.github.Matoking.protontricks"

WINECFG, TRICKS, RUN_EXE = "winecfg", "tricks", "run"


@dataclass
class PrefixInfo:
    """mode is "steam", "wine" or "umu"; problem, when set, is the i18n
    key saying why the tools can't be used yet (no prefix, no runner)."""
    mode: str
    prefix: Optional[str] = None      # the folder with drive_c in it
    runner: str = ""                  # what runs it, as shown
    wine: Optional[str] = None        # wine mode: the wine binary
    appid: Optional[str] = None       # steam mode
    umu: Optional[str] = None         # umu mode: umu-run
    env: Dict[str, str] = field(default_factory=dict)   # umu mode: PROTONPATH, GAMEID
    problem: Optional[str] = None


class MissingTool(Exception):
    """The tool to install first: a Shipwright tool id."""


def wine_prefix(path) -> Optional[str]:
    """The folder holding drive_c: the path itself, or its pfx/ (Proton's
    layout - umu makes pfx a link back to the folder itself)."""
    if not path:
        return None
    p = Path(os.path.expanduser(str(path)))
    for candidate in (p, p / "pfx"):
        if (candidate / "drive_c").is_dir():
            return str(candidate)
    return None


def proton_wine(proton_dir) -> Optional[str]:
    """A Proton build's own wine: files/ since Proton 5.13, dist/ before."""
    for sub in ("files", "dist"):
        wine = Path(proton_dir) / sub / "bin" / "wine"
        if wine.exists():
            return str(wine)
    return None


def _version_key(name: str) -> List:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name.lower())]


def find_proton(name: str, dirs: List[Path]) -> Optional[Path]:
    """A Proton build by its folder name in dirs; a "latest" name (GE-Proton,
    UMU-Proton, "GE-Proton (Latest)") is the newest one starting with it."""
    if not name:
        return None
    entries = []
    for directory in dirs:
        try:
            entries += [e for e in directory.iterdir() if proton_wine(e)]
        except OSError:
            continue
    exact = next((e for e in entries if e.name == name), None)
    if exact is not None:
        return exact
    stem = re.sub(r"\s*\(latest\)\s*$", "", name, flags=re.I).strip()
    stem = re.sub(r"[-\s]*latest$", "", stem, flags=re.I).strip()
    newest = sorted((e for e in entries if e.name.lower().startswith(stem.lower())),
                    key=lambda e: _version_key(e.name))
    return newest[-1] if newest else None


PROGRAM_SUFFIXES = (".exe", ".msi", ".bat")
# Windows's own folders in a fresh prefix's Program Files - nothing to run there
_WINDOWS_OWN = {"common files", "internet explorer", "windows media player", "windows nt", "microsoft.net",
                "windows photo viewer", "windowspowershell", "windows defender", "microsoft", "msbuild",
                "reference assemblies"}


def find_programs(root, depth: int = 4, limit: int = 400) -> List[Path]:
    """The .exe / .msi / .bat files under root, at most depth folders down -
    uninstallers last, then the shallowest first."""
    found: List[Path] = []
    try:
        base = Path(root)
        start = len(base.parts)
        for folder, dirs, files in os.walk(base):
            here = len(Path(folder).parts) - start
            dirs[:] = [d for d in dirs if here + 1 < depth and d.lower() not in _WINDOWS_OWN]
            for name in files:
                if name.lower().endswith(PROGRAM_SUFFIXES):
                    found.append(Path(folder) / name)
            if len(found) >= limit:
                break
    except OSError:
        pass

    def key(path: Path):
        stem = path.name.lower()
        return (stem.startswith(("unins", "uninst")) or "redist" in str(path).lower(),
                len(path.parts), stem)
    return sorted(found, key=key)


def prefix_programs(prefix: Optional[str]) -> List[Path]:
    """What's installed inside the prefix - its C:\\Program Files."""
    if not prefix:
        return []
    drive = Path(prefix) / "drive_c"
    out: List[Path] = []
    for folder in ("Program Files", "Program Files (x86)"):
        out += find_programs(drive / folder, depth=3)
    return out


def _protontricks(launch: bool = False) -> Optional[List[str]]:
    name = "protontricks-launch" if launch else "protontricks"
    path = which(name)
    if path:
        return [path]
    if shutil.which("flatpak") and flatpak_app_installed(PROTONTRICKS_FLATPAK):
        return ["flatpak", "run", f"--command={name}", PROTONTRICKS_FLATPAK]
    return None


def needed_tool(info: PrefixInfo, action: str) -> Optional[str]:
    """The Shipwright tool the action needs that isn't installed, if any."""
    if info.mode == "steam":
        return None if _protontricks(action == RUN_EXE) else "protontricks"
    if info.mode == "wine" and action == TRICKS and not which("winetricks"):
        return "winetricks"
    return None


def command(info: PrefixInfo, action: str, exe: Optional[str] = None) -> Tuple[List[str], Dict[str, str], Optional[str]]:
    """(argv, environment additions, working folder) for action on the
    prefix - raises MissingTool when its tool isn't installed."""
    missing = needed_tool(info, action)
    if missing:
        raise MissingTool(missing)
    cwd = str(Path(exe).parent) if exe else None
    if info.mode == "steam":
        # --no-term: errors come up as a dialog instead of on a terminal
        if action == RUN_EXE:
            return _protontricks(True) + ["--no-term", "--appid", str(info.appid), exe], {}, cwd
        verb = ["winecfg"] if action == WINECFG else ["--gui"]
        return _protontricks() + ["--no-term", str(info.appid)] + verb, {}, cwd
    if info.mode == "umu":
        env = {"WINEPREFIX": info.prefix or "", "GAMEID": "umu-default", **info.env}
        argv = {WINECFG: ["winecfg"], TRICKS: ["winetricks", "--gui"]}.get(action, [exe])
        return [info.umu] + argv, env, cwd
    wine = info.wine or which("wine") or "wine"
    env = {"WINEPREFIX": info.prefix or "", "WINE": wine}
    server = Path(wine).parent / "wineserver"
    if server.exists():
        env["WINESERVER"] = str(server)
    if action == WINECFG:
        return [wine, "winecfg"], env, cwd
    if action == TRICKS:
        return [which("winetricks"), "--gui"], env, cwd
    # start /unix opens .msi and .bat files too, not just .exe
    return [wine, "start", "/unix", exe], env, cwd


def run(info: PrefixInfo, action: str, exe: Optional[str] = None) -> None:
    """Starts the tool on its own; it outlives nothing of ours."""
    argv, extra, cwd = command(info, action, exe)
    env = {**os.environ, **extra}
    logger.info(f"Prefix tool: {' '.join(argv)} ({', '.join(f'{k}={v}' for k, v in extra.items())})")
    proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True, name="prefix-tool-reaper").start()   # no zombie
