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

"""A game GOG Galaxy installed in a Faugus prefix, started straight from
its own files while Galaxy is closed (user's choice, 11-22). Galaxy itself
takes ~24 s under Wine before it starts a game: every start it asks its
service ten times, a second apart, to write Vulkan registry keys, which
the service refuses without elevation ("Cannot create Vulkan keys without
elevation and a registry elevator") - nothing outside can skip that. GOG
games are DRM-free: what Galaxy would run is in the game's
goggame-<id>.info (its primary playTask). Faugus' own runner still runs it
(the entry's Proton, gamemode, variables, its playtime), only with the
game's program instead of GalaxyClient.exe. While Galaxy is open the launch
goes through it as before (its cloud saves, achievements, time)."""

import json
import logging
import ntpath
import os
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# run by Faugus' own python: its runner with the entry's program swapped
# (argv: game id, program under drive_c, the program's arguments)
_CODE = (
    "import os, sys\n"
    "from faugus.runner import FaugusRun, build_launch_command, load_game_from_json\n"
    "gid, exe, args = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "game = load_game_from_json(gid)\n"
    "if game:\n"
    "    game = dict(game, path=os.path.join(os.path.expanduser(game.get('prefix') or ''), 'drive_c', exe),"
    " game_arguments=args, addapp_enabled='')\n"
    "    FaugusRun(build_launch_command(game), None, game.get('pre_launch', ''), game.get('post_launch', ''),"
    " gid, title=game.get('title', ''), exe=game['path']).run()\n"
)
_NEEDS = ("def build_launch_command", "def load_game_from_json", "class FaugusRun")


def _play_task(install_dir: Path, store_id: str):
    """(program relative to the game's folder, its arguments) of the
    game's primary play task, None when there's none to run."""
    try:
        info = json.loads((install_dir / f"goggame-{store_id}.info").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    tasks = [t for t in info.get("playTasks") or [] if isinstance(t, dict)
             and t.get("type") == "FileTask" and t.get("path") and (t.get("category") or "game") == "game"]
    task = next((t for t in tasks if t.get("isPrimary")), tasks[0] if tasks else None)
    if task is None:
        return None
    return str(task["path"]).replace("\\", "/"), str(task.get("arguments") or "")


def _runner_ok(own_runner: List[str]) -> bool:
    """Faugus' runner has what the swap uses - its runner.py read: a distro
    package's in the site folders, a Flatpak's in its installed files
    (per user or system-wide). Not found or not readable: False (the game
    goes through Galaxy as before)."""
    from plunderhub.core.scan_paths import FAUGUS_SITE_DIRS, FLATPAK_APP_DIRS
    if own_runner[:2] == ["flatpak", "run"]:
        app_id = next((w for w in own_runner[2:] if not w.startswith("-")), "")
        files = [p for root in FLATPAK_APP_DIRS
                 for p in (root / app_id).glob("current/active/files/lib/python3*/site-packages/faugus/runner.py")]
    else:
        files = [d / "faugus/runner.py" for d in FAUGUS_SITE_DIRS]
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        return all(n in text for n in _NEEDS)
    return False


def command(game_id: str) -> Optional[List[str]]:
    """The command that starts this GOG game without Galaxy, None when it
    goes through Galaxy (not a GOG game, Galaxy - or anything - open in
    its prefix, no play task, no Faugus runner that can do it)."""
    from plunderhub.core import faugus_store
    from plunderhub.core.battlenet_launch import launcher_umu
    owner = faugus_store.known().get(game_id) or {}
    install_dir = str(owner.get("install_dir") or "")
    if owner.get("kind") != "gog" or "/drive_c/" not in install_dir or not owner.get("store_id"):
        return None
    prefix = install_dir.split("/drive_c/")[0]
    if launcher_umu(prefix, None) is not None:
        return None         # Galaxy (or something of it) is open: through it
    task = _play_task(Path(install_dir), str(owner["store_id"]))
    if task is None:
        return None
    rel = ntpath.normpath(install_dir.split("/drive_c/", 1)[1] + "/" + task[0]).replace("\\", "/")
    if not (Path(prefix) / "drive_c" / rel).is_file():
        return None
    return _runner_command(game_id, rel, task[1])


def _runner_command(game_id: str, rel: str, args: str) -> Optional[List[str]]:
    """Faugus' runner for this game's entry, running the program at
    drive_c/<rel> with these arguments (raw text, as Faugus appends it)."""
    from plunderhub.core.scan_paths import faugus_games_path, faugus_own_runner
    games = faugus_games_path()
    runner = faugus_own_runner(games) if games is not None else None
    if not runner or runner[-2:] != ["-m", "faugus.runner"] or not _runner_ok(runner):
        return None
    return [*runner[:-2], "-c", _CODE, game_id, rel, args]


def uninstall_command(game_id: str, program: List[str]) -> Optional[List[str]]:
    """A GOG game's own uninstaller (unins000.exe - Inno Setup's, it needs
    no Galaxy) run on its own while Galaxy is closed: not Galaxy started
    first for it (~24 s, its window popping up after the question). None
    when it goes the usual way."""
    import shlex
    from plunderhub.core import faugus_store
    from plunderhub.core.battlenet_launch import launcher_umu
    owner = faugus_store.known().get(game_id) or {}
    install_dir = str(owner.get("install_dir") or "")
    if owner.get("kind") != "gog" or "/drive_c/" not in install_dir or not program \
            or "/drive_c/" not in program[0]:
        return None
    prefix = install_dir.split("/drive_c/")[0]
    if launcher_umu(prefix, None) is not None:
        return None
    return _runner_command(game_id, program[0].split("/drive_c/", 1)[1], shlex.join(program[1:]))
