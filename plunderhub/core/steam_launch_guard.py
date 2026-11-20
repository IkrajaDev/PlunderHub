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

"""A cancelled Steam launch, kept from opening.

Steam has no way to call off a launch it was asked for: after an update
or its shader processing it starts the game anyway. Every Steam game
starts through `reaper SteamLaunch AppId=<id> -- ...`, so for a while
after the cancel new processes are looked at (only new ones, ten times a
second) and that start - with whatever it already started - is stopped
before the game gets a window up.

Only the cancelled launch is stopped: Steam logs each launch it's asked for
under a number of its own (console log: GameAction [AppID <id>, ActionID <n>]).
The start's launch began before the cancel: the cancelled one, stopped. It
began after it: the player starting the game again (from Steam itself) -
let be, and the guard ends; so is any start after Steam was restarted (the
cancelled launch went with the old Steam). Without the log, every start is
stopped."""

import os
import re
import signal
import threading
import time
from typing import Dict, Optional, Set

from plunderhub.logging_setup import logger

_POLL = 0.1
_AFTER_KILL = 5.0       # what the start still spawns is stopped for this long

_guards: Dict[str, threading.Event] = {}
_lock = threading.Lock()


def _read(pid: str, name: str) -> bytes:
    try:
        with open(f"/proc/{pid}/{name}", "rb") as f:
            return f.read()
    except OSError:
        return b""


def _parent(pid: str) -> Optional[str]:
    stat = _read(pid, "stat").decode("utf-8", "replace")
    try:
        return stat[stat.rfind(")") + 2:].split()[1]
    except IndexError:
        return None


_TAIL = 256 * 1024


_STAMP = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] GameAction \[AppID (\d+), ActionID (\d+)\]")


def launch_began(appid: str) -> Optional[float]:
    """When appid's newest launch in Steam's console log began (its first
    line's time), or None."""
    from plunderhub.core.steam_launch_status import _log_dirs
    best = None         # (log's time, began)
    for folder in _log_dirs():
        path = folder / "console_log.txt"
        try:
            mtime = path.stat().st_mtime
            with path.open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - _TAIL))
                text = f.read().decode("utf-8", errors="ignore")
        except OSError:
            continue
        began, last = None, None
        for line in text.splitlines():
            m = _STAMP.match(line)
            if m is None or m.group(2) != appid:
                continue
            if m.group(3) != last:              # a launch's first line
                last = m.group(3)
                try:
                    began = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
                except (ValueError, OverflowError):
                    began = None
        if began is not None and (best is None or mtime > best[0]):
            best = (mtime, began)
    return best[1] if best else None


def _steam_pids() -> Set[int]:
    from plunderhub.core.launch_options import launcher_pids
    try:
        return set(launcher_pids("Steam"))
    except Exception:
        return set()


def _is_start(cmdline: bytes, appid: str) -> bool:
    args = cmdline.split(b"\0")
    return b"SteamLaunch" in args and f"AppId={appid}".encode() in args


def _is_game(pid: str, appid: str) -> bool:
    env = _read(pid, "environ").split(b"\0")
    return f"SteamAppId={appid}".encode() in env or f"SteamGameId={appid}".encode() in env


def _kill_tree(root: str, pids) -> int:
    """root and every process below it."""
    parents = {p: _parent(p) for p in pids}
    tree = {root}
    grew = True
    while grew:
        grew = False
        for p, parent in parents.items():
            if parent in tree and p not in tree:
                tree.add(p)
                grew = True
    for p in tree:
        try:
            os.kill(int(p), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            pass
    return len(tree)


def block_next_start(appid: str, seconds: float) -> None:
    """For `seconds`, Steam starting appid is stopped as soon as it starts."""
    with _lock:
        old = _guards.pop(appid, None)
        if old is not None:
            old.set()
        stop = threading.Event()
        _guards[appid] = stop
    threading.Thread(target=_watch, args=(appid, seconds, stop, time.time(), _steam_pids()),
                     name=f"plunderhub-steam-guard-{appid}", daemon=True).start()


def cancel_block(appid: str) -> None:
    """The player starts it again on purpose: no longer kept from opening."""
    with _lock:
        stop = _guards.pop(appid, None)
    if stop is not None:
        stop.set()


def _anothers(appid: str, cancelled_at: float, steam: Set[int]) -> Optional[str]:
    """Why this start isn't the cancelled launch, or None when it is."""
    if steam and not (_steam_pids() & steam):
        return "Steam was restarted since"
    try:
        began = launch_began(appid)
    except Exception:
        began = None
    # Steam's times are whole seconds: one of slack
    if began is not None and began > cancelled_at + 1:
        return "a launch asked for after the cancel"
    return None


def _watch(appid: str, seconds: float, stop: threading.Event, cancelled_at: float = 0.0,
           steam: Optional[Set[int]] = None) -> None:
    deadline = time.monotonic() + seconds
    try:
        seen: Set[str] = {e for e in os.listdir("/proc") if e.isdigit()}
    except OSError:
        return
    killed_at: Optional[float] = None
    while not stop.is_set():
        now = time.monotonic()
        if (killed_at is not None and now - killed_at > _AFTER_KILL) or (killed_at is None and now > deadline):
            break
        try:
            current = [e for e in os.listdir("/proc") if e.isdigit()]
        except OSError:
            break
        new = [p for p in current if p not in seen]
        seen.update(new)
        for pid in new:
            started = _is_start(_read(pid, "cmdline"), appid)
            if started and killed_at is None:
                # the player's own start? Steam writes the launch's lines just before
                # it starts the game: a moment for them to be in the log
                why = _anothers(appid, cancelled_at, steam or set())
                if why is None:
                    stop.wait(0.2)
                    why = _anothers(appid, cancelled_at, steam or set())
                if why is not None:
                    logger.info(f"Steam started {appid} after a cancelled launch, but it's {why} - let be")
                    stop.set()
                    break
            if started or (killed_at is not None and _is_game(pid, appid)):
                count = _kill_tree(pid, current)
                if killed_at is None:
                    logger.info(f"Steam started {appid} after its launch was cancelled - stopped it at once "
                                f"({count} process(es))")
                killed_at = time.monotonic()
        stop.wait(_POLL)
    with _lock:
        if _guards.get(appid) is stop:
            _guards.pop(appid, None)
