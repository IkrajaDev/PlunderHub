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

"""What Steam is doing while it launches a game - updating itself,
downloading a game update, processing shaders, syncing the cloud - read
from its own logs, so a launch that takes a while says why.

Steam writes each step of a launch to logs/console_log.txt
("GameAction [AppID 892970, ActionID 1] : LaunchApp changed task to
DownloadingDepots"), the game's state to content_log.txt ("AppID 892970
state changed : Fully Installed,App Running,") and its own update to
bootstrap_log.txt. Only lines written since the launch count.

A download says how far it is: content_log.txt has its size when it
starts ("AppID 892970 update started : download 744191728/1019655664,
... stage 2633151984/2909185306") and whether it's the game itself or
its shader cache ("AppID 892970 Shader update changed : ..."), the
game's appmanifest has the bytes downloaded so far, and the staging
folder grows as it goes."""

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from plunderhub.core.steam_heroic import parse_vdf_text, steam_libraries, steam_roots
from plunderhub.logging_setup import logger


@dataclass
class SteamLaunchStage:
    # steam_starting, steam_updating, updating, shaders_download,
    # shaders, setup, cloud, waiting, stalled, preparing, starting,
    # running or failed
    stage: str
    # when Steam last logged anything about this launch (time.time())
    since: float
    error: str = ""
    # a download's bytes so far and in all, when Steam says
    done: int = 0
    total: int = 0


_TAIL_BYTES = 256 * 1024
_LINE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$")
_GAME_ACTION = re.compile(r"GameAction \[AppID (\d+), ActionID \d+\] : LaunchApp (.*)$")
_STATE = re.compile(r"AppID (\d+) state changed : (.*)$")
_UPDATE_KIND = re.compile(r"AppID (\d+) (Shader|App|Workshop) update changed : ")
_UPDATE_STARTED = re.compile(r"AppID (\d+) update started : download (\d+)/(\d+), .*stage (\d+)/(\d+)")
# A launch that stays on its download this long after Steam finished it
# has stalled - Steam sometimes never goes on to start the game.
STALL_SECONDS = 15.0

_TASK_STAGES = {
    "DownloadingDepots": "updating",
    "UpdatingAppInfo": "updating",
    "ProcessingShaderCache": "shaders",
    "RunningInstallScript": "setup",
    "ProcessingInstallScript": "setup",
    "SynchronizingCloud": "cloud",
    "CreatingProcess": "starting",
    "WaitingGameWindow": "starting",
    "Completed": "starting",
}
# Steam waits on these for the player, in a window of its own.
_ASKS_THE_PLAYER = {"ShowLaunchOption", "ShowEula", "ShowCloudSyncConflict", "ShowCDKey",
                    "ShowGameArgs", "ShowDuplicateLaunch"}
_BOOTSTRAP_UPDATING = ("Downloading update", "Extracting package", "Installing update",
                       "Verifying installation")


def _log_dirs() -> List[Path]:
    dirs = [root / "logs" for root in steam_roots()]
    return [d for d in dirs if d.is_dir()]


def _tail_lines(path: Path, since: float) -> List[Tuple[float, str]]:
    """The file's lines written at or after `since`, newest last."""
    try:
        if path.stat().st_mtime < since - 1:
            return []
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            text = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    lines = []
    for raw in text.splitlines():
        match = _LINE.match(raw)
        if match is None:
            continue
        try:
            stamp = time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            continue
        # Steam's stamps are whole seconds
        if stamp >= int(since):
            lines.append((stamp, match.group(2)))
    return lines


def _manifest_bytes(library: Path, appid: str) -> Tuple[int, int]:
    """(BytesDownloaded, BytesToDownload) of the game's appmanifest."""
    try:
        text = (library / "steamapps" / f"appmanifest_{appid}.acf").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0, 0
    state = parse_vdf_text(text).get("AppState") or {}
    try:
        return int(state.get("BytesDownloaded") or 0), int(state.get("BytesToDownload") or 0)
    except (TypeError, ValueError):
        return 0, 0


def _folder_bytes(folder: Path, limit: int = 20000) -> int:
    """What's written to disk under the folder (allocated blocks, so a
    preallocated file counts only as far as it's filled)."""
    total = 0
    seen = 0
    for base, _dirs, files in os.walk(folder):
        for name in files:
            seen += 1
            if seen > limit:
                return total
            try:
                st = os.lstat(os.path.join(base, name))
            except OSError:
                continue
            total += min(st.st_size, st.st_blocks * 512)
    return total


def _download_progress(appid: str, lines: List[Tuple[float, str]], shaders: bool) -> Tuple[int, int]:
    """(bytes downloaded, bytes in all) of the game's running download,
    (0, 0) when nothing says."""
    started = None
    for _stamp, text in lines:
        match = _UPDATE_STARTED.search(text)
        if match is not None and match.group(1) == appid:
            started = tuple(int(g) for g in match.groups()[1:])
    library = next((lib for lib in steam_libraries()
                    if (lib / "steamapps" / f"appmanifest_{appid}.acf").exists()), None)
    if library is None:
        return 0, 0
    done, total = _manifest_bytes(library, appid)
    if started is None:
        return (done, total) if 0 < done < total else (0, 0)
    got, size, staged_at_start, stage_size = started
    if size <= 0:
        return 0, 0
    staging = (library / "steamapps" / "shadercache" / appid / "downloads" if shaders
               else library / "steamapps" / "downloading" / appid)
    if stage_size > staged_at_start:
        # the staging folder grows from what was there at the start
        staged = _folder_bytes(staging) if staging.is_dir() else 0
        part = (staged - staged_at_start) / (stage_size - staged_at_start)
        got = max(got, int(got + (size - got) * min(max(part, 0.0), 1.0)))
    if total == size and got < done < size:
        got = done
    return min(got, size), size


def steam_launch_stage(appid: str, since: float) -> Optional[SteamLaunchStage]:
    """Where Steam is with launching `appid`, which was asked for at
    `since` (time.time()); None without any Steam logs to read."""
    dirs = _log_dirs()
    if not dirs:
        return None
    try:
        # the install written to last is the one launching it
        logs = max(dirs, key=lambda d: max((p.stat().st_mtime for p in d.glob("*_log.txt")), default=0))
        stage = SteamLaunchStage("steam_starting", since)
        for stamp, text in _tail_lines(logs / "bootstrap_log.txt", since):
            if text.startswith(_BOOTSTRAP_UPDATING):
                stage = SteamLaunchStage("steam_updating", stamp)
        task_at = since
        for stamp, text in _tail_lines(logs / "console_log.txt", since):
            match = _GAME_ACTION.search(text)
            if match is None or match.group(1) != appid:
                continue
            action = match.group(2)
            if action.startswith("changed task to "):
                task = action[len("changed task to "):].split(" ", 1)[0]
                task_at = stamp
                if task == "Failed":
                    stage = SteamLaunchStage("failed", stamp, stage.error)
                else:
                    stage = SteamLaunchStage(_TASK_STAGES.get(task, "preparing"), stamp)
            elif action.startswith("waiting for user response to "):
                asked = action[len("waiting for user response to "):].split(" ", 1)[0]
                if asked in _ASKS_THE_PLAYER:
                    stage = SteamLaunchStage("waiting", stamp)
            elif action.startswith("failed with "):
                stage = SteamLaunchStage("failed", stamp, action[len("failed with "):].split(" ", 1)[0])
        if stage.stage == "failed":
            return stage
        # from a little before the launch: a download may have started
        # before it, and goes on in the launch's name
        content = _tail_lines(logs / "content_log.txt", since - 3600)
        kind = None
        finished_at = None
        for stamp, text in content:
            match = _STATE.search(text)
            if match is not None and match.group(1) == appid:
                if stamp >= int(since) and "App Running" in match.group(2):
                    return SteamLaunchStage("running", stamp)
                # nothing queued or running any more
                finished_at = None if "Update" in match.group(2) else stamp
                continue
            match = _UPDATE_KIND.search(text)
            if match is not None and match.group(1) == appid:
                kind = match.group(2)
        if stage.stage == "updating":
            if finished_at is not None and finished_at >= task_at:
                return SteamLaunchStage("stalled", finished_at)
            shaders = kind == "Shader"
            done, total = _download_progress(appid, content, shaders)
            return SteamLaunchStage("shaders_download" if shaders else "updating", stage.since,
                                    done=done, total=total)
        return stage
    except Exception as e:
        logger.debug(f"Couldn't read Steam's launch progress for {appid}: {e}")
        return None
