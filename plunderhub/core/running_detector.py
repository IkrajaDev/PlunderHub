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

"""Notices installed games that run without PlunderHub having started
them - from Steam, Heroic, Lutris, Faugus or Bottles directly.

A background thread walks /proc once every few seconds and matches each
process against every installed game:

  - Steam: Steam's own registry.vdf "Running" flag, when it can be read
    (exact, and it ignores shader compilation Steam keeps doing under the
    game's id after it quit); processes carrying the game's SteamAppId
    are what gets stopped.
  - a native game: its executable lives in the install folder.
  - a Wine/Proton game: one of its command-line arguments is a program
    file (.exe, .sh, ...) inside the install folder - Wine shows it as
    Z:\\path\\to\\game.exe - or is the game's known executable, also when
    that sits inside the prefix (C:\\...). A bare folder argument never
    counts: Heroic's and Lutris's own download/verify processes get the
    install folder passed, and that's not the game running.
  - Bottles: the bottle's prefix plus the program's file name (as for
    games PlunderHub launches itself).

A process merely sitting in a game's folder (a terminal, a file manager)
never counts - only executables and program arguments do."""

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from plunderhub.core.utils import WINE_SERVICE_PROCESSES, GameProcessMatch, _pid_matches, _resolve_match
from plunderhub.logging_setup import logger

_STEAM_APPID_ENV_KEYS = ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID")
_PROGRAM_SUFFIXES = (".exe", ".sh", ".x86_64", ".x86", ".bin", ".appimage", ".bat")
_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


@dataclass(frozen=True)
class GameTarget:
    uid: str
    install_dir: Optional[str] = None          # realpath
    exe_paths: FrozenSet[str] = frozenset()    # lowercased realpaths of known executables
    steam_appid: Optional[str] = None
    bottles_match: Optional[GameProcessMatch] = None


@dataclass
class Detection:
    pids: List[int] = field(default_factory=list)
    age_seconds: float = 0.0                   # how long the oldest matched process has run


def _realpath(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def windows_path_to_candidates(arg: str) -> Tuple[Optional[str], Optional[str]]:
    """(host path, drive_c tail) for a Windows-style argument: Z:\\a\\b.exe
    is /a/b.exe on the host; C:\\x\\y.exe is "drive_c/x/y.exe" inside some
    prefix. (None, None) if it isn't a drive path."""
    if len(arg) < 3 or arg[1] != ":" or arg[2] not in "\\/" or not arg[0].isalpha():
        return None, None
    rest = arg[3:].replace("\\", "/")
    if arg[0].lower() == "z":
        return "/" + rest, None
    if arg[0].lower() == "c":
        return None, "drive_c/" + rest.lower()
    return None, None


class _Proc:
    __slots__ = ("pid", "root", "_exe", "_cmdline", "_env", "_start")

    def __init__(self, root: Path, pid: int):
        self.pid, self.root = pid, root
        self._exe = self._cmdline = self._env = self._start = None

    @property
    def exe(self) -> str:
        if self._exe is None:
            try:
                self._exe = os.path.realpath(os.readlink(self.root / str(self.pid) / "exe"))
            except OSError:
                self._exe = ""
        return self._exe

    @property
    def cmdline(self) -> List[str]:
        if self._cmdline is None:
            try:
                raw = (self.root / str(self.pid) / "cmdline").read_bytes()
                self._cmdline = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
            except OSError:
                self._cmdline = []
        return self._cmdline

    @property
    def env(self) -> Dict[str, str]:
        if self._env is None:
            self._env = {}
            try:
                raw = (self.root / str(self.pid) / "environ").read_bytes()
            except OSError:
                return self._env
            for item in raw.split(b"\0"):
                key, sep, value = item.partition(b"=")
                if sep:
                    self._env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
        return self._env

    @property
    def start_ticks(self) -> Optional[int]:
        if self._start is None:
            try:
                stat = (self.root / str(self.pid) / "stat").read_text()
                self._start = int(stat[stat.rfind(")") + 2:].split()[19])
            except (OSError, ValueError, IndexError):
                self._start = -1
        return None if self._start < 0 else self._start

    def program_args(self) -> List[str]:
        """Arguments that name a program file (and argv[0])."""
        args = self.cmdline
        return [a for i, a in enumerate(args) if i == 0 or a.lower().endswith(_PROGRAM_SUFFIXES)]

    def is_wine_service(self) -> bool:
        first = self.cmdline[0] if self.cmdline else ""
        return first.replace("\\", "/").rsplit("/", 1)[-1].lower() in WINE_SERVICE_PROCESSES


def _boot_seconds_now() -> float:
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


def match_processes(targets: List[GameTarget], proc_root: Path = Path("/proc"),
                    steam_running=None) -> Dict[str, Detection]:
    """One pass over proc_root -> {uid: Detection} for every target that
    has a matching process. steam_running(appid) -> True/False/None (None:
    unknown, fall back to processes)."""
    by_appid = {t.steam_appid: t.uid for t in targets if t.steam_appid}
    dirs = [(t.install_dir, t.uid) for t in targets if t.install_dir]
    exe_full: Dict[str, str] = {}
    exe_tails: Dict[str, str] = {}
    for t in targets:
        for exe in t.exe_paths:
            exe_full[exe] = t.uid
            if "/drive_c/" in exe:
                exe_tails["drive_c/" + exe.split("/drive_c/", 1)[1]] = t.uid
    bottles = [(t.uid, _resolve_match(t.bottles_match)) for t in targets if t.bottles_match]
    bottles = [(uid, resolved) for uid, resolved in bottles if resolved is not None]

    try:
        pids = [int(e) for e in os.listdir(proc_root) if e.isdigit()]
    except OSError:
        return {}
    own = os.getpid()
    found: Dict[str, Set[int]] = {}
    procs: Dict[int, _Proc] = {}

    def hit(uid: str, proc: _Proc) -> None:
        found.setdefault(uid, set()).add(proc.pid)
        procs[proc.pid] = proc

    for pid in pids:
        if pid == own:
            continue
        proc = _Proc(proc_root, pid)
        env = proc.env
        if not env and not proc.cmdline:
            continue                     # a kernel thread, or not ours to read
        # umu sets SteamAppId from a "umu-<id>" GAMEID too: a Heroic / Lutris /
        # Faugus game, matched below by its own folder, not as the Steam copy
        appid = None if env.get("UMU_ID") else \
            next((env[k] for k in _STEAM_APPID_ENV_KEYS if env.get(k) in by_appid), None)
        if appid is not None:
            hit(by_appid[appid], proc)
            continue
        exe = proc.exe
        matched = False
        if exe:
            for root, uid in dirs:
                if _under(exe, root):
                    hit(uid, proc)
                    matched = True
                    break
        if matched or proc.is_wine_service():
            continue
        for arg in proc.program_args():
            host, tail = windows_path_to_candidates(arg)
            candidate = _realpath(host) if host else (_realpath(arg) if arg.startswith("/") else None)
            uid = None
            if candidate:
                uid = exe_full.get(candidate.lower())
                if uid is None:
                    uid = next((u for root, u in dirs if _under(candidate, root)), None)
            elif tail:
                uid = exe_tails.get(tail)
            if uid is not None:
                hit(uid, proc)
                matched = True
                break
        if matched:
            continue
        for uid, resolved in bottles:
            if _pid_matches(pid, resolved):
                hit(uid, proc)
                break

    result: Dict[str, Detection] = {}
    now_boot = _boot_seconds_now()
    for uid, pid_set in found.items():
        starts = [procs[p].start_ticks for p in pid_set if procs[p].start_ticks is not None]
        age = max(0.0, now_boot - min(starts) / _CLK_TCK) if starts else 0.0
        result[uid] = Detection(pids=sorted(pid_set), age_seconds=age)

    if steam_running is not None:
        for t in targets:
            if not t.steam_appid:
                continue
            state = steam_running(t.steam_appid)
            if state is False:
                result.pop(t.uid, None)       # e.g. shader compilation after the game quit
            elif state is True and t.uid not in result:
                result[t.uid] = Detection()   # Steam says so, even if its processes can't be read
    return result


def _usable_install_dir(path: Optional[str]) -> Optional[str]:
    """The install folder as a matching rule - unless it's so broad (/, a
    top-level folder, the home folder or above) that unrelated programs
    would count as the game."""
    real = _realpath(path)
    if not real or not os.path.isdir(real):
        return None
    home = os.path.realpath(os.path.expanduser("~"))
    if real.count("/") < 2 or _under(home, real):
        return None
    return real


def build_targets(games) -> List[GameTarget]:
    """A GameTarget per installed game, from what each launcher records."""
    heroic_exes = _heroic_executables()
    targets: List[GameTarget] = []
    for game in games:
        uid = game.uid
        try:
            if uid.startswith("steam:"):
                targets.append(GameTarget(uid, _usable_install_dir(game.install_dir), steam_appid=uid.split(":", 1)[1]))
            elif uid.startswith("heroic:"):
                exe = heroic_exes.get(uid.split(":", 2)[2])
                targets.append(GameTarget(uid, _usable_install_dir(game.install_dir), _exe_set(exe)))
            elif uid.startswith("lutris:"):
                targets.append(GameTarget(uid, _usable_install_dir(game.install_dir), _exe_set(_lutris_executable(game))))
            elif uid.startswith("faugus:"):
                exe = _faugus_executable(game)
                targets.append(GameTarget(uid, _usable_install_dir(os.path.dirname(exe) if exe else None), _exe_set(exe)))
            elif uid.startswith("bottles:") and game.install_dir:
                from plunderhub.core.scanner import bottles_program_executables
                names = bottles_program_executables(game.install_dir, uid.split(":", 1)[1], game.title)
                targets.append(GameTarget(uid, bottles_match=GameProcessMatch(wineprefix=game.install_dir,
                                                                              exe_names=names)))
        except Exception as e:
            logger.debug(f"Couldn't prepare running-game detection for {uid}: {e}")
    return targets


def _exe_set(exe: Optional[str]) -> FrozenSet[str]:
    real = _realpath(exe)
    return frozenset({real.lower()}) if real else frozenset()


def _heroic_executables() -> Dict[str, str]:
    """app_name -> absolute executable, where Heroic's records name one:
    Legendary's installed.json ("executable" inside "install_path") and
    hand-added games (install.executable)."""
    from plunderhub.core.persistence import load_json
    from plunderhub.core.steam_heroic import heroic_roots
    exes: Dict[str, str] = {}
    for path in heroic_roots():
        data = load_json(path)
        if path.parent.name == "sideload_apps" and isinstance(data, dict):
            for item in data.get("games") or []:
                install = item.get("install") if isinstance(item, dict) else None
                if isinstance(install, dict) and install.get("executable") and item.get("app_name"):
                    exes[str(item["app_name"])] = str(install["executable"])
        elif path.parent.name == "legendary" and isinstance(data, dict):
            for app_name, info in data.items():
                if isinstance(info, dict) and info.get("executable") and info.get("install_path"):
                    exes[str(app_name)] = os.path.join(str(info["install_path"]), str(info["executable"]))
    return exes


def _lutris_executable(game) -> Optional[str]:
    from plunderhub.core.scan_paths import lutris_game_config_path
    from plunderhub.core.scanner import read_lutris_config_id
    import yaml
    path = lutris_game_config_path(read_lutris_config_id(game.uid.split(":", 1)[1]) or "")
    if path is None:
        return None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    exe = data.get("game", {}).get("exe") if isinstance(data, dict) and isinstance(data.get("game"), dict) else None
    return os.path.expanduser(str(exe)) if exe and str(exe).startswith(("/", "~")) else None


def _faugus_executable(game) -> Optional[str]:
    """The game file PlunderHub launches with umu-run (see _faugus_command)."""
    cmd = game.launch_cmd or []
    for i, word in enumerate(cmd[:-1]):
        if os.path.basename(word) == "umu-run":
            return cmd[i + 1]
    return None


class RunningDetector:
    """Runs match_processes() in the background; read with snapshot()."""

    def __init__(self, interval: float = 3.0, steam_running=None):
        self._interval = interval
        self._steam_running = steam_running
        self._lock = threading.Lock()
        self._targets: List[GameTarget] = []
        self._results: Dict[str, Detection] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="plunderhub-detector", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._stop.clear()

    def set_targets(self, targets: List[GameTarget]) -> None:
        with self._lock:
            self._targets = list(targets)
            known = {t.uid for t in targets}
            self._results = {u: d for u, d in self._results.items() if u in known}
        self._wake.set()

    def snapshot(self) -> Dict[str, Detection]:
        with self._lock:
            return dict(self._results)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                targets = list(self._targets)
            if targets:
                try:
                    results = match_processes(targets, steam_running=self._steam_running)
                except Exception as e:
                    logger.debug(f"Running-game detection failed: {e}")
                    results = None
                if results is not None:
                    with self._lock:
                        known = {t.uid for t in self._targets}
                        self._results = {u: d for u, d in results.items() if u in known}
            self._wake.wait(self._interval)
            self._wake.clear()
