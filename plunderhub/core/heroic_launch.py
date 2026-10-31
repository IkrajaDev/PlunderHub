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

"""A Heroic launch that installs the game's update first, and says what
Heroic is doing meanwhile - read from Heroic's own log.

Heroic looks for game updates only when it starts, and a heroic://launch
starts the game long before that check is done, so the update - with
"Automatically update games" on - came in while the game was already
running. PlunderHub therefore starts a closed Heroic on its own first,
waits for that check (and the update it queues for this game), and only
then hands it the launch. A Heroic already running checked when it
started; an update it's still installing for the game is waited for.

Heroic's log (logs/heroic.log, a new one each time Heroic starts) has
the steps: "[Gog]: Found 1 game(s) to update", "[Gog]: Auto-Updating
<title>", "Progress for <title>: 41%/...", "[DownloadManager]: Finished
Update of <appName>", "SETUP: Installing redist ...", "[Backend]:
Launching <title> (<appName>)" and the runner's own "Launching <title>:
<command>" once the game itself starts."""

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from plunderhub.core.scan_paths import HEROIC_CONFIG_ROOTS

_FLATPAK_IDS = ("com.heroicgameslauncher.hgl", "io.github.heroicgameslauncher.hgl")

# the folder of a Heroic uid (heroic:<folder>:<appName>) -> Heroic's runner
_RUNNERS = {"gog_store": "gog", "legendary": "legendary", "nile": "nile"}
# how each runner's log lines start
_PREFIXES = {"gog": "[Gog]:", "legendary": "[Legendary]:", "nile": "[Nile]:"}


def _log_dirs() -> List[Path]:
    home = Path.home()
    state = Path(os.environ.get("XDG_STATE_HOME") or home / ".local/state")
    dirs = [state / "Heroic/logs", home / ".local/state/Heroic/logs"]
    for app_id in _FLATPAK_IDS:
        dirs += [home / ".var/app" / app_id / ".local/state/Heroic/logs",
                 home / ".var/app" / app_id / "config/heroic/logs"]
    dirs.append(home / ".config/heroic/logs")
    return dirs


def heroic_log_path() -> Optional[Path]:
    """The log Heroic last wrote to - heroic.log, or heroic.log.old: a
    second Heroic started to hand a launch over can move the running
    one's log aside, and that one carries on writing there."""
    found = []
    for d in _log_dirs():
        for name in ("heroic.log", "heroic.log.old"):
            try:
                found.append(((d / name).stat().st_mtime, d / name))
            except OSError:
                continue
    return max(found)[1] if found else None


def heroic_main_pids() -> List[int]:
    """Heroic's main processes - comm is "heroic" for the Flatpak, the
    AppImage and the .deb/.rpm, but the Arch package runs it as
    electronNN app.asar, and Electron rewrites its command line as one
    string."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    found = []
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().replace(b"\0", b" ").split()
        except OSError:
            continue
        if not args or any(a.startswith(b"--type=") for a in args):
            continue        # Electron's helper processes
        if os.path.basename(args[0]) == b"heroic" or any(
                a.endswith((b"heroic-games-launcher/app.asar", b"heroic/resources/app.asar")) for a in args[1:]):
            found.append(int(pid))
    return found


def heroic_running() -> bool:
    return bool(heroic_main_pids())


def _config_dirs() -> List[Path]:
    return [base / "heroic" for base in HEROIC_CONFIG_ROOTS if (base / "heroic/config.json").exists()]


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def runner_of(uid: str) -> Optional[str]:
    parts = uid.split(":", 2)
    return _RUNNERS.get(parts[1]) if len(parts) == 3 and parts[0] == "heroic" else None


def updates_before_launch(uid: str) -> bool:
    """Whether Heroic installs this game's updates on its own: the
    "Automatically update games" setting on, and the game's "Ignore game
    updates" off."""
    if runner_of(uid) is None:
        return False
    app_name = uid.split(":", 2)[2]
    for d in _config_dirs():
        settings = (_load(d / "config.json") or {}).get("defaultSettings", {})
        if not settings.get("autoUpdateGames"):
            continue
        game = (_load(d / "GamesConfig" / f"{app_name}.json") or {}).get(app_name, {})
        return not game.get("ignoreGameUpdates", False)
    return False


def heroic_title(uid: str) -> Optional[str]:
    """The game's title as Heroic logs it - not always the one PlunderHub
    shows (a GOG game's install folder, say)."""
    runner = runner_of(uid)
    if runner is None:
        return None
    app_name = uid.split(":", 2)[2]
    for d in _config_dirs():
        data = _load(d / "store_cache" / f"{runner}_library.json") or {}
        games = data.get("games") or data.get("library") or []
        for game in games if isinstance(games, list) else []:
            if isinstance(game, dict) and game.get("app_name") == app_name and game.get("title"):
                return str(game["title"])
    return None


@dataclass
class LogMark:
    """Where heroic.log was when the launch was asked for: what's
    written after it is this launch's."""
    path: Optional[Path]
    inode: Optional[int]
    offset: int
    time: float = 0.0


def mark_log() -> LogMark:
    path = heroic_log_path()
    try:
        st = path.stat() if path else None
    except OSError:
        st = None
    return LogMark(path, st.st_ino if st else None, st.st_size if st else 0, time.time())


def _read_since(mark: LogMark, whole: bool = False) -> List[str]:
    """The log's lines since the mark (all of it with whole). Heroic
    moves heroic.log aside to heroic.log.old now and then - when it
    starts, and at times again while running - so both count, oldest
    first: the marked file from the mark on, a newer one in full."""
    newest = heroic_log_path()
    if newest is None:
        return []
    files = []
    for name in ("heroic.log.old", "heroic.log"):
        try:
            st = (newest.parent / name).stat()
        except OSError:
            continue
        files.append((st.st_mtime, newest.parent / name, st))
    lines: List[str] = []
    # newest first, so whole can stop at the start of Heroic's session
    for _mtime, path, st in sorted(files, key=lambda f: f[0], reverse=True):
        if whole:
            start = 0
        elif st.st_ino == mark.inode and st.st_size >= mark.offset:
            start = mark.offset
        elif st.st_mtime >= mark.time - 1:
            start = 0
        else:
            continue
        try:
            with path.open("rb") as f:
                f.seek(start)
                part = f.read(4 * 1024 * 1024).decode("utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        lines = part + lines
        if whole and part and "System Information" in part[0]:
            break       # an earlier session's log says nothing of now
    return lines


@dataclass
class HeroicLaunchStage:
    # heroic_starting, checking_updates, updating, cloud, redist, prefix,
    # winetricks, preparing, launched or failed
    stage: str
    # how far an update is, "41%", when it says
    progress: str = ""
    # and the rest Heroic says of it: "123.45MB" downloaded, "12.3"
    # MB/s, "00:01:23" left
    downloaded: str = ""
    speed: str = ""
    eta: str = ""
    # set once Heroic is through its update check (or isn't doing one):
    # the launch can be handed over
    ready: bool = False


_LINE = re.compile(r"^\(\d\d:\d\d:\d\d\) \[\w+\]:\s+(\[\w+\]:)?\s*(.*)$")
# "Progress for <title>: 41.25%/123.45MB/00:01:23 Down: 12.3MB/s / Disk: 20.1MB/s"
_PROGRESS = re.compile(r":\s*([\d.]+)%(?:/([\d.]+\s*[KMGT]?i?B))?(?:/(\d[\d:]*))?(?:\s+Down:\s*([\d.]+)\s*MB/s)?")


class HeroicLaunch:
    """Follows one launch in heroic.log. started_heroic: PlunderHub
    started Heroic for it, so its update check is waited for."""

    # Heroic's "Auto-Updating" comes a moment after its "Found" line
    _AFTER_CHECK = 4.0

    def __init__(self, uid: str, title: str, started_heroic: bool, now: float):
        self.uid = uid
        self.runner = runner_of(uid) or ""
        self.app_name = uid.split(":", 2)[2] if uid.count(":") >= 2 else ""
        self.title = heroic_title(uid) or title
        self.started_heroic = started_heroic
        self.started_at = now
        self.mark = mark_log()
        self.handed_over = False
        self.handed_at: Optional[float] = None
        # the heroic://launch to hand over, and its environment
        self.cmd: List[str] = []
        self.env = None
        self._check_seen_at: Optional[float] = None

    def poll(self, now: float) -> HeroicLaunchStage:
        prefix = _PREFIXES.get(self.runner, "")
        app_name, title = self.app_name, self.title
        # A Heroic that was running already may be updating the game -
        # that is in its log from its start.
        lines = _read_since(self.mark, whole=not self.started_heroic and not self.handed_over)
        frontend_ready = check_done = nile_looking = updating = ours = False
        stage = HeroicLaunchStage("heroic_starting" if self.started_heroic else "preparing")
        for raw in lines:
            match = _LINE.match(raw)
            if match is None:
                continue
            source, text = match.group(1) or "", match.group(2)
            if text == "Frontend Ready":
                frontend_ready = True
                if self.started_heroic and not updating:
                    stage = HeroicLaunchStage("checking_updates")
            elif source == prefix and text.startswith("Found ") and "to update" in text:
                check_done = True
            elif source == "[Nile]:" and text.startswith("Looking for updates"):
                nile_looking = True     # a Found line follows only with updates
            elif source == prefix and text == f"Auto-Updating {title}":
                updating = True
                stage = HeroicLaunchStage("updating")
            elif text.startswith(f"Progress for {title}:") and (updating or not ours):
                updating = True
                found = _PROGRESS.search(text)
                stage = HeroicLaunchStage("updating")
                if found is not None:
                    stage.progress = f"{float(found.group(1)):.0f}%"
                    stage.downloaded = (found.group(2) or "").replace(" ", "")
                    stage.speed = found.group(4) or ""
                    stage.eta = found.group(3) or ""
            elif app_name and source == "[DownloadManager]:" and re.match(
                    rf"(Finished Update of|Update of) {re.escape(app_name)}\b", text):
                updating = False
                stage = HeroicLaunchStage("preparing")
            elif source == "[Backend]:" and text.startswith("Launching ") and text.endswith(f"({app_name})"):
                ours = True
                stage = HeroicLaunchStage("preparing")
            elif not ours:
                continue
            elif text.startswith(("Downloading saves for", "Uploading saves for")):
                stage = HeroicLaunchStage("cloud")
            elif "Installing redist" in text:
                stage = HeroicLaunchStage("redist")
            elif "createprefix" in text:
                stage = HeroicLaunchStage("prefix")
            elif "inetricks" in text:
                stage = HeroicLaunchStage("winetricks")
            elif source == prefix and text.startswith(f"Launching {title}:"):
                stage = HeroicLaunchStage("launched")
            elif text.startswith("Was not possible to launch"):
                stage = HeroicLaunchStage("failed")
        if (check_done or (self.runner == "nile" and nile_looking)) and self._check_seen_at is None:
            self._check_seen_at = now
        waited = now - self.started_at
        if self.handed_over or not self.started_heroic:
            stage.ready = not updating or self.handed_over
        elif updating:
            stage.ready = False
        elif self._check_seen_at is not None:
            wait = 12.0 if self.runner == "nile" and not check_done else self._AFTER_CHECK
            stage.ready = now - self._check_seen_at >= wait
        else:
            # no check coming (offline, logged out of the store): go ahead
            stage.ready = waited > (90 if frontend_ready else 60)
        return stage
