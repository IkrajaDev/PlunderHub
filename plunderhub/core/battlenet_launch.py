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

"""Starting a Battle.net game in a Faugus prefix (11-22, D2R live-tested).

Battle.net reads `--exec="launch XYZ"` only from a second copy of itself
that hands it over to the running one: a Battle.net that is just starting
(signing in) takes the switch and does nothing with it. And a second copy
started through Faugus' runner is no good either: when it exits (at once,
after the hand-over) the runner kills every process with the same Faugus
id - Battle.net with it, the game half started.

So: Battle.net already open -> only the hand-over is sent. Closed -> it is
started (Faugus' runner, the game's own entry: its settings reach the game),
and once its log says it signed in, the hand-over follows. The hand-over is
the running launcher's own umu-run command line and environment, the
switch in place of its arguments, with UMU_CONTAINER_NSENTER so umu enters
the running container instead of starting another one, and the "run" verb
so Proton doesn't wait for the prefix to go quiet first. Takes ~1 s."""

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.logging_setup import logger

_LOGS = "drive_c/users/steamuser/AppData/Local/Battle.net/Logs"
_EXE_TAIL = "battle.net/battle.net.exe"
_SIGNED_IN = "Logged into Battle.net successfully"
_LAUNCHED = "LaunchBinary:"
_STAMP = re.compile(r"^[A-Z] (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")

# after the sign-in line, Battle.net still loads its game list for a moment
SETTLE_SECONDS = 1.0
# a hand-over Battle.net let go (too early) is sent again after this, a few times
RESEND_SECONDS = 6.0
MAX_SENDS = 3
# a sign-in by hand (new account, 2FA) may take a while
GIVE_UP_SECONDS = 600.0


def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _read(pid: str, name: str) -> bytes:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as f:
            return f.read()
    except OSError:
        return b""


def _env(pid: str) -> Dict[str, str]:
    env = {}
    for item in _read(pid, "environ").split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def _in_prefix(env: Dict[str, str], prefix: str) -> bool:
    value = env.get("WINEPREFIX")
    if not value:
        return False
    real, root = _real(value), _real(prefix)
    return real == root or real == os.path.join(root, "pfx")


def launcher_umu(prefix: str, exe_tail: Optional[str] = _EXE_TAIL) -> Optional[Tuple[List[str], Dict[str, str]]]:
    """The umu-run that keeps this prefix's Battle.net (exe_tail None: any
    program) running: its command line up to the program (umu-run itself
    and the program) and its environment; None when it isn't open there."""
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        argv = [a.decode("utf-8", "replace") for a in _read(pid, "cmdline").split(b"\0") if a]
        umu_at = next((i for i, a in enumerate(argv) if os.path.basename(a) == "umu-run"), None)
        if umu_at is None or umu_at + 1 >= len(argv):
            continue
        exe_at = umu_at + 1 if exe_tail is None else next(
            (i for i, a in enumerate(argv) if i > umu_at and a.replace("\\", "/").lower().endswith(exe_tail)), None)
        if exe_at is None:
            continue
        env = _env(pid)
        if _in_prefix(env, prefix):
            return argv[:exe_at + 1], env
    return None


def handover_to_open_launcher(game_id: str) -> Optional[subprocess.Popen]:
    """Any store launcher's game (EA app, Epic...) while its launcher is
    already open: the game's launch link handed straight to it, inside its
    running container - not a second Faugus run (a new container, ~5 s,
    and on exit Faugus kills every process of that entry: a launcher the
    game's own card had started died with it). None when it isn't open or
    the game's entry can't be read: then the usual run."""
    from plunderhub.core import faugus_store
    from plunderhub.core.scan_paths import faugus_games_path
    owner = faugus_store.known().get(game_id) or {}
    install_dir = str(owner.get("install_dir") or "")
    if not owner or owner.get("kind") == "battle" or "/drive_c/" not in install_dir:
        return None
    prefix = install_dir.split("/drive_c/")[0]
    if launcher_umu(prefix, None) is None:
        return None
    games = faugus_games_path()
    try:
        import json
        entry = next((g for g in json.loads(games.read_text(encoding="utf-8"))
                      if isinstance(g, dict) and g.get("gameid") == game_id), None) if games else None
    except (OSError, ValueError):
        return None
    if not entry or not entry.get("path"):
        return None
    try:
        args = shlex.split(str(entry.get("game_arguments") or ""))
    except ValueError:
        return None
    proc = _run_inside(prefix, [os.path.expanduser(str(entry["path"]))] + args,
                       f"{game_id}'s launch", exe_tail=None)
    if proc is not None:
        logger.info(f"Store launcher game: {game_id}'s launch handed to its open launcher")
    return proc


def wargaming_game(game_id: str):
    """A game Wargaming Game Center installed: (its prefix, the program Game
    Center itself starts for it). Game Center has no switch that starts a
    game - "--open" only shows its page, Play left to the player (11-22) -
    so its program is run inside Game Center's container, as Game Center
    does (no arguments; it signs in through the running Game Center).
    None for any other game."""
    from plunderhub.core import faugus_store
    owner = faugus_store.known().get(game_id) or {}
    folder = str(owner.get("install_dir") or "")
    if owner.get("kind") != "wargaming" or "/drive_c/" not in folder:
        return None
    exe = faugus_store.wargaming_exe(Path(folder))
    return (folder.split("/drive_c/")[0], exe) if exe is not None else None


def wargaming_ready(prefix: str, since: float) -> bool:
    """Game Center started after `since` has its window up (its log of this
    run says the main window got the focus)."""
    logs = Path(prefix) / "drive_c/ProgramData/Wargaming.net/GameCenter/logs"
    try:
        newest = max(logs.glob("wgc_*.log"), key=lambda p: p.stat().st_mtime)
        if newest.stat().st_mtime < since - 5:
            return False
        with open(newest, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 512 * 1024))
            text = f.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return False
    return "The main window has got focus" in text


def start_wargaming_game(prefix: str, exe: Path) -> Optional[subprocess.Popen]:
    proc = _run_inside(prefix, [str(exe)], "the Wargaming game", exe_tail=None)
    if proc is not None:
        logger.info(f"Wargaming: {exe.name} started inside Game Center's container")
    return proc


# a page of a store launcher to open where its games are uninstalled (11-22, Epic:
# its links have no uninstall; "com.epicgames.launcher://library" is an "invalid
# location", the store-relative one lands on the Library). Epic's log says it's
# ready for it once it has opened its first page.
LIBRARY_PAGE = {"epic": ("com.epicgames.launcher://store/library",
                         "drive_c/users/steamuser/AppData/Local/EpicGamesLauncher/Saved/Logs/EpicGamesLauncher.log",
                         "MainRouter] GoToUrl(")}


def open_library(prefix: str, kind: str, launcher_exe: str) -> bool:
    """Shows the open launcher's library page (see LIBRARY_PAGE)."""
    page = LIBRARY_PAGE.get(kind)
    if page is None:
        return False
    proc = _run_inside(prefix, [launcher_exe, page[0]], "its library page", exe_tail=None)
    if proc is not None:
        logger.info(f"Store launcher ({kind}): its library page opened for an uninstall")
    return proc is not None


def library_ready_since(prefix: str, kind: str, since: float) -> bool:
    """Whether the launcher, started at `since`, has shown its first page."""
    page = LIBRARY_PAGE.get(kind)
    if page is None:
        return True
    log = Path(prefix) / page[1]
    try:
        if log.stat().st_mtime < since:
            return False
        return page[2] in log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def plan(game_id: str) -> Optional[Tuple[str, str]]:
    """(prefix, product code) when this Faugus game is one Battle.net
    installed, else None."""
    from plunderhub.core import faugus_store
    entry = faugus_store.known().get(game_id) or {}
    install_dir = str(entry.get("install_dir") or "")
    if entry.get("kind") != "battle" or "/drive_c/" not in install_dir:
        return None
    code = str(entry.get("store_id") or "")
    if not code:
        return None
    product = faugus_store.BATTLE_GAMES.get(code.lower(), (code.upper(), ""))[0]
    return install_dir.split("/drive_c/")[0], product


def _log_lines_since(prefix: str, since: float) -> List[str]:
    """The sign-in and launch lines of Battle.net's logs written at
    `since` or later (its stamps are UTC)."""
    folder = Path(prefix) / _LOGS
    try:
        logs = [p for p in folder.glob("battle.net-*.log") if p.stat().st_mtime >= since - 1]
    except OSError:
        return []
    lines = []
    for log in logs:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if _SIGNED_IN not in line and _LAUNCHED not in line:
                continue        # only these two are looked for (polled every second)
            m = _STAMP.match(line)
            if not m:
                continue
            try:
                at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue
            if at >= int(since):
                lines.append(line)
    return lines


def _run_inside(prefix: str, program_args: List[str], what: str,
                exe_tail: Optional[str] = _EXE_TAIL) -> Optional[subprocess.Popen]:
    """Runs a Windows program inside the container of the launcher open in
    this prefix (Battle.net's, or with exe_tail None any umu-run there) -
    as the hand-over does; None when nothing runs there."""
    found = launcher_umu(prefix, exe_tail)
    if found is None:
        return None
    argv, env = found
    if env.get("FLATPAK_ID") or env.get("container") == "flatpak":
        # a Flatpak Faugus: its umu-run and Python live in its sandbox - not runnable from
        # the host, and a new sandbox would start a second wineserver on the same prefix.
        # The caller goes the usual way (a Faugus run / the launcher opened).
        logger.info(f"Store launcher in a Flatpak sandbox: {what} not handed to it from outside")
        return None
    env = dict(env)
    env.pop("FAUGUSID", None)        # not one of Faugus' own: its clean-up leaves it be
    env.update({"UMU_CONTAINER_NSENTER": "1", "PROTON_VERB": "run"})
    try:
        return subprocess.Popen(argv[:-1] + program_args, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        logger.warning(f"Battle.net: couldn't start {what}: {e}")
        return None


def uninstaller(prefix: str, store_id: str) -> Optional[List[str]]:
    """Blizzard's own uninstaller for a game (its Windows "Uninstall"
    entry: Blizzard Uninstaller.exe --uid=<code> ...): the host path of the
    program and its arguments; None when the entry isn't there. It asks
    "Are you sure..." itself and removes the game through Battle.net."""
    from plunderhub.core import faugus_store
    reg = Path(prefix) / "system.reg"
    if not reg.is_file():
        return None
    want = f"--uid={store_id.lower()}"
    for under in ("Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
                  "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall"):
        try:
            keys = faugus_store.read_reg(reg, under)
        except OSError:
            return None
        for values in keys.values():
            line = values.get("UninstallString") or ""
            if want not in line.lower() or "blizzard uninstaller" not in line.lower():
                continue
            try:      # a Windows command line: its backslashes aren't escapes
                words = shlex.split(line.replace("\\", "/"))
            except ValueError:
                continue
            exe = faugus_store.host_path(Path(prefix), words[0]) if words else None
            if exe is not None and exe.is_file():
                return [str(exe)] + words[1:]
    return None


def start_uninstaller(prefix: str, command: List[str]) -> bool:
    """A game's own uninstaller, inside its open launcher's container."""
    proc = _run_inside(prefix, command, "the game's uninstaller", exe_tail=None)
    if proc is not None:
        logger.info(f"Store launcher game: its uninstaller started: {os.path.basename(command[0])} "
                    f"{' '.join(command[1:])}")
    return proc is not None


def signed_in_since(prefix: str, since: float) -> bool:
    return any(_SIGNED_IN in line for line in _log_lines_since(prefix, since))


def send(prefix: str, product: str) -> Optional[subprocess.Popen]:
    """Hands `launch <product>` to the open Battle.net: the hand-over's
    process (it exits in a second); None when Battle.net isn't open or it
    couldn't be started."""
    found = launcher_umu(prefix)
    if found is None:
        return None
    proc = _run_inside(prefix, [found[0][-1], f"--exec=launch {product}"], f"'launch {product}'")
    if proc is not None:
        logger.info(f"Battle.net: 'launch {product}' handed to the open launcher")
    return proc


@dataclass
class BattleNetLaunch:
    """One game waiting for its Battle.net to sign in."""
    prefix: str
    product: str
    started: float
    signed_in_at: Optional[float] = None
    done: bool = False
    sent_at: List[float] = field(default_factory=list)

    def poll(self, now: float) -> bool:
        """Sends the hand-over once Battle.net has signed in (again if no
        launch follows); True once there's nothing more to do (Battle.net
        launched the game, or given up)."""
        if self.done:
            return True
        if now - self.started >= GIVE_UP_SECONDS:
            logger.info(f"Battle.net: 'launch {self.product}' given up - no sign-in in {GIVE_UP_SECONDS:.0f} s")
            self.done = True
            return True
        lines = _log_lines_since(self.prefix, self.started)
        if any(_LAUNCHED in line for line in lines):
            self.done = True            # started by itself (or by hand)
            return True
        if self.signed_in_at is None:
            if any(_SIGNED_IN in line for line in lines):
                self.signed_in_at = now
            return False
        if now - self.signed_in_at < SETTLE_SECONDS:
            return False
        if self.sent_at and now - self.sent_at[-1] < RESEND_SECONDS:
            return False                # waiting for its LaunchBinary line
        if len(self.sent_at) >= MAX_SENDS:
            logger.info(f"Battle.net: 'launch {self.product}' sent {MAX_SENDS} times, no launch seen - left to Play")
            self.done = True
            return True
        if send(self.prefix, self.product) is not None:
            self.sent_at.append(now)
        return False
