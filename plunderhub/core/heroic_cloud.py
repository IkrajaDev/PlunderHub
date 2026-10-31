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

"""Heroic's "Manual Sync Saves" for a GOG or Epic game, without Heroic.

Heroic syncs through the store tools it ships: gogdl save-sync for GOG
(once per save location, with the last sync's timestamp from
gog_store/saveTimestamps.json, which it then updates) and legendary
sync-saves for Epic. PlunderHub runs the same commands with Heroic's
own login, settings and save folders, the way Heroic's Sync button does."""

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.core.heroic_launch import _config_dirs, _load, heroic_main_pids, runner_of
from plunderhub.core.utils import which
from plunderhub.logging_setup import logger

# Heroic's four choices, in its order, with the flag each one passes
SYNC_MODES: List[Tuple[str, str]] = [
    ("download", "--skip-upload"),
    ("upload", "--skip-download"),
    ("force_download", "--force-download"),
    ("force_upload", "--force-upload"),
]

_FLATPAK_IDS = ("com.heroicgameslauncher.hgl", "io.github.heroicgameslauncher.hgl")
_FLATPAK_RESOURCES = "/app/bin/heroic/resources"


@dataclass
class CloudSaves:
    runner: str                     # gog or legendary
    app_name: str
    config_dir: Path                # Heroic's config folder
    platform: str = "windows"
    # (name, folder): GOG may have several, Epic has one
    locations: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def flatpak(self) -> bool:
        return "/.var/app/" in str(self.config_dir)


def cloud_saves_all(uid: str) -> List[CloudSaves]:
    """The game's cloud saves as each Heroic has them set up - a native
    and a Flatpak Heroic side by side each have their own. Empty when
    the game has none (or isn't a GOG or Epic game)."""
    runner = runner_of(uid)
    if runner not in ("gog", "legendary"):
        return []
    app_name = uid.split(":", 2)[2]
    found = []
    for d in _config_dirs():
        library = _load(d / "store_cache" / f"{runner}_library.json") or {}
        games = library.get("games") or library.get("library") or []
        info = next((g for g in games if isinstance(g, dict) and g.get("app_name") == app_name), None)
        if info is None or not info.get("cloud_save_enabled"):
            continue
        settings = (_load(d / "GamesConfig" / f"{app_name}.json") or {}).get(app_name, {})
        saves = CloudSaves(runner, app_name, d)
        if runner == "gog":
            saves.locations = [(str(loc.get("name") or "saves"), str(loc["location"]))
                               for loc in settings.get("gogSaves") or []
                               if isinstance(loc, dict) and loc.get("location")]
            installed = (_load(d / "gog_store" / "installed.json") or {}).get("installed", [])
            game = next((g for g in installed if g.get("appName") == app_name), {})
            saves.platform = str(game.get("platform") or "windows")
        elif settings.get("savesPath"):
            saves.locations = [("saves", str(settings["savesPath"]))]
        found.append(saves)
    return found


def cloud_saves(uid: str) -> Optional[CloudSaves]:
    """The first of cloud_saves_all: whether the game has any."""
    found = cloud_saves_all(uid)
    return next((s for s in found if s.locations), found[0] if found else None)


def _resources_dirs() -> List[Path]:
    """Where a host Heroic's app.asar may be: beside the running one
    (an AppImage is only mounted while it runs), the one its launcher
    script runs (Arch: electron on /usr/lib/heroic-games-launcher/
    app.asar), the folder of its binary (.deb/.rpm: /opt/Heroic), or the
    usual install places."""
    dirs: List[Path] = []
    for pid in heroic_main_pids():
        try:
            exe = Path(os.path.realpath(f"/proc/{pid}/exe"))
        except OSError:
            continue
        dirs += [exe.parent / "resources", exe.parent]
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                words = f.read().replace(b"\0", b" ").decode(errors="ignore").split()
            dirs += [Path(w).parent for w in words if w.endswith("app.asar")]
        except OSError:
            pass
    launcher = which("heroic")
    if launcher:
        try:
            text = Path(launcher).read_text(errors="ignore")[:4096]
            dirs += [Path(w).parent for w in shlex.split(text.replace("\n", " "), comments=True)
                     if w.endswith("app.asar")]
        except (OSError, ValueError, UnicodeDecodeError):
            pass
        dirs.append(Path(os.path.realpath(launcher)).parent / "resources")
    dirs += [Path(p) for p in ("/usr/lib/heroic-games-launcher", "/opt/Heroic/resources",
                               "/opt/heroic/resources", "/usr/lib/heroic/resources",
                               "/usr/share/heroic/resources")]
    return dirs


def _host_tool(name: str) -> Optional[str]:
    for base in _resources_dirs():
        unpacked = base / "app.asar.unpacked" / "build" / "bin"
        # x64/linux (arm64/linux on ARM); older builds: linux
        for path in sorted(unpacked.glob(f"*/linux/{name}")) + [unpacked / "linux" / name]:
            if os.access(path, os.X_OK):
                return str(path)
    return None


def _command(name: str, config_dir: Path, variables: Dict[str, str]
             ) -> Optional[Tuple[List[str], Dict[str, str]]]:
    """How to run Heroic's copy of gogdl or legendary with these
    variables set: (the command to put its arguments after, the
    environment). The one chosen in Heroic's settings, else the Flatpak's
    - in its sandbox, which takes no variables from outside, so they go
    in as --env - or the host Heroic's."""
    flatpak_id = next((i for i in _FLATPAK_IDS if f"/.var/app/{i}/" in f"{config_dir}/"), None)
    alt = (_load(config_dir / "config.json") or {}).get("defaultSettings", {}).get(
        "altGogdlBin" if name == "gogdl" else "altLegendaryBin")
    host_env = {**os.environ, **variables}
    if alt and os.access(alt, os.X_OK) and flatpak_id is None:
        return [alt], host_env
    if flatpak_id is not None:
        # the tools' folder differs between Heroic versions
        script = (f'for d in {_FLATPAK_RESOURCES}/app.asar.unpacked/build/bin/*/linux '
                  f'{_FLATPAK_RESOURCES}/app.asar.unpacked/build/bin/linux; do '
                  f'[ -x "$d/{name}" ] && exec "$d/{name}" "$@"; done; '
                  f'echo "{name} not found in the Heroic Flatpak" >&2; exit 127')
        return (["flatpak", "run", *[f"--env={k}={v}" for k, v in variables.items()],
                 "--command=sh", flatpak_id, "-c", script, name], dict(os.environ))
    tool = _host_tool(name)
    return ([tool], host_env) if tool else None


def _timestamps_path(saves: CloudSaves) -> Path:
    return saves.config_dir / "gog_store" / "saveTimestamps.json"


def run_sync(saves: CloudSaves, mode: str) -> Tuple[bool, str]:
    """Runs one sync the way Heroic does; (worked, what the tool said).
    Blocking - call it off the main thread."""
    flag = dict(SYNC_MODES)[mode]
    if not saves.locations:
        return False, "no save folder"
    if saves.runner == "legendary":
        found = _command("legendary", saves.config_dir, {
            "LEGENDARY_CONFIG_PATH": str(saves.config_dir / "legendaryConfig" / "legendary")})
        if found is None:
            return False, "Heroic's sync tool wasn't found"
        tool, env = found
        _name, folder = saves.locations[0]
        ok, out, _ = _run([*tool, "sync-saves", saves.app_name, flag, "--save-path", folder, "-y"], env)
        return ok, out
    found = _command("gogdl", saves.config_dir, {"GOGDL_CONFIG_PATH": str(saves.config_dir / "gogdlConfig")})
    if found is None:
        return False, "Heroic's sync tool wasn't found"
    tool, env = found
    output = []
    ok = True
    stamps_path = _timestamps_path(saves)
    stamps: Dict = _load(stamps_path) or {}
    for name, folder in saves.locations:
        stamp = str((stamps.get(saves.app_name) or {}).get(name, "0"))
        cmd = [*tool, "--auth-config-path", str(saves.config_dir / "gog_store" / "auth.json"),
               "save-sync", folder, saves.app_name, "--os", saves.platform, "--ts", stamp,
               "--name", name, flag]
        worked, out, stdout = _run(cmd, env)
        output.append(out)
        ok = ok and worked
        if worked and stdout.strip():
            # the new timestamp, as Heroic keeps it for the next sync
            stamps.setdefault(saves.app_name, {})[name] = stdout.strip()
    # each location that worked keeps its new stamp, as Heroic does, even if another failed
    if stamps != (_load(stamps_path) or {}):
        try:
            tmp = stamps_path.with_suffix(".json.plunderhub-tmp")
            tmp.write_text(json.dumps(stamps, indent="\t"), encoding="utf-8")
            os.replace(tmp, stamps_path)
        except OSError as e:
            logger.warning(f"Couldn't save GOG's sync timestamps: {e}")
    return ok, "\n".join(output)


def _run(cmd: List[str], env: Dict[str, str]) -> Tuple[bool, str, str]:
    logger.info(f"Cloud sync: {shlex.join(cmd)}")
    try:
        done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e), ""
    out = (done.stderr or "") + (done.stdout or "")
    if done.returncode != 0:
        logger.warning(f"Cloud sync failed ({done.returncode}): {out[-2000:]}")
    return done.returncode == 0, out, done.stdout or ""
