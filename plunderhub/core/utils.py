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


import json
import math
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.logging_setup import logger


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
def run(cmd: List[str], env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None) -> subprocess.Popen:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             env=run_env, cwd=cwd, start_new_session=True)

@dataclass(frozen=True)
class GameProcessMatch:
    """What identifies a launched game's own processes in /proc.

    A process's working directory is deliberately never enough on its
    own: a terminal, editor or file manager that merely sits in the game
    folder would otherwise count as the game - kept "running" forever,
    and killed by the Stop button along with it."""
    # A process whose executable lives under here (native games).
    directory: Optional[str] = None
    # Steam sets these on every process of a game it launches, Proton
    # included - the reliable marker where the executable is Proton's
    # own wine binary rather than anything in the game folder.
    steam_appid: Optional[str] = None
    # A Wine prefix (a Bottles bottle) - shared by every program in it,
    # so narrowed down by exe_names (or, failing that, by leaving out
    # Wine's own background services).
    wineprefix: Optional[str] = None
    exe_names: Tuple[str, ...] = ()


_STEAM_APPID_ENV_KEYS = ("SteamAppId", "SteamGameId", "STEAM_COMPAT_APP_ID")

# Wine's own background processes - present in a prefix whether or not
# any game is running in it, so they never count as "the game".
WINE_SERVICE_PROCESSES = frozenset({
    "wineserver", "wine", "wine64", "wine-preloader", "wine64-preloader",
    "services.exe", "winedevice.exe", "plugplay.exe", "explorer.exe",
    "rpcss.exe", "svchost.exe", "tabtip.exe", "conhost.exe", "start.exe",
    "winemenubuilder.exe", "rundll32.exe", "wineboot.exe",
})

# (root, steam_appid, wineprefix, exe_names) - realpaths resolved once
# per scan rather than once per process.
_ResolvedMatch = Tuple[Optional[str], Optional[str], Optional[str], Tuple[str, ...]]


def _existing_dir_realpath(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        root = os.path.realpath(path)
    except OSError:
        return None
    if not root or not os.path.isdir(root):
        return None
    return root


def _resolve_match(match: GameProcessMatch) -> Optional[_ResolvedMatch]:
    root = _existing_dir_realpath(match.directory)
    prefix = _existing_dir_realpath(match.wineprefix)
    if root is None and match.steam_appid is None and prefix is None:
        return None
    exe_names = tuple(n.lower() for n in match.exe_names if n)
    return (root, match.steam_appid, prefix, exe_names)


def _is_under(target: str, root: str) -> bool:
    return target == root or target.startswith(root.rstrip(os.sep) + os.sep)


def _read_proc_environ(pid: int) -> Dict[str, str]:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    env: Dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep:
            env[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return env


def _read_proc_cmdline(pid: int) -> List[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _process_basename(arg: str) -> str:
    return arg.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _pid_matches(pid: int, resolved: _ResolvedMatch) -> bool:
    root, steam_appid, prefix, exe_names = resolved
    if root is not None:
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            exe = None
        if exe and _is_under(exe, root):
            return True
    if steam_appid is None and prefix is None:
        return False
    env = _read_proc_environ(pid)
    if not env:
        return False
    if steam_appid is not None and any(env.get(k) == steam_appid for k in _STEAM_APPID_ENV_KEYS):
        return True
    if prefix is not None:
        env_prefix = env.get("WINEPREFIX")
        if not env_prefix or _existing_dir_realpath(env_prefix) != prefix:
            return False
        cmdline = _read_proc_cmdline(pid)
        if not cmdline:
            return False
        if exe_names:
            joined = "\0".join(cmdline).lower()
            return any(name in joined for name in exe_names)
        return _process_basename(cmdline[0]) not in WINE_SERVICE_PROCESSES
    return False


def _proc_pids() -> Optional[List[str]]:
    try:
        return [entry for entry in os.listdir("/proc") if entry.isdigit()]
    except OSError:
        return None


def _scan_matching_pids(resolved: _ResolvedMatch, stop_at_first: bool) -> List[int]:
    pid_entries = _proc_pids()
    if pid_entries is None:
        return []
    own_pid = os.getpid()
    found: List[int] = []
    for entry in pid_entries:
        pid = int(entry)
        if pid == own_pid:
            continue
        if _pid_matches(pid, resolved):
            found.append(pid)
            if stop_at_first:
                return found
    return found


def find_game_pids(match: GameProcessMatch) -> List[int]:
    resolved = _resolve_match(match)
    if resolved is None:
        return []
    return _scan_matching_pids(resolved, stop_at_first=False)


def is_game_process_running(match: GameProcessMatch) -> Optional[bool]:
    resolved = _resolve_match(match)
    if resolved is None:
        return None
    return _is_running_cached(resolved, cache_key=resolved)


def pids_under_directory(directory: str) -> List[int]:
    return find_game_pids(GameProcessMatch(directory=directory))


def is_directory_process_running(directory: str) -> Optional[bool]:
    root = _existing_dir_realpath(directory)
    if root is None:
        return None
    return _is_running_cached((root, None, None, ()), cache_key=root)


def _is_running_cached(resolved: _ResolvedMatch, cache_key) -> Optional[bool]:
    if _proc_pids() is None:
        return None
    cached = _DIR_PID_CACHE.get(cache_key)
    if cached is not None:
        if _pid_matches(cached, resolved):
            return True
        _DIR_PID_CACHE.pop(cache_key, None)
    found = _scan_matching_pids(resolved, stop_at_first=True)
    if found:
        _DIR_PID_CACHE[cache_key] = found[0]
        return True
    return False


_DIR_PID_CACHE: Dict[object, int] = {}


def process_start_time(pid: int) -> Optional[int]:
    """The process's start time (clock ticks since boot) - together with
    the pid it identifies one process uniquely, so a pid the kernel has
    since handed to something else is never mistaken for the original."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
    except OSError:
        return None
    # The command name (field 2) may itself contain spaces or ')', so
    # count fields from the last ')' instead of splitting the whole line.
    fields = raw[raw.rfind(b")") + 2:].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def split_minutes_by_day(end: datetime, active_seconds: float) -> Dict[str, int]:
    """Whole minutes of a play session that ended at `end`, spread over
    the calendar days ("YYYY-MM-DD") it covered - so a session past
    midnight counts toward both days rather than all on the last. The
    per-day minutes always add up to the session's whole-minute total."""
    total_minutes = int(max(0.0, active_seconds) // 60)
    if total_minutes <= 0:
        return {}
    cursor = end - timedelta(minutes=total_minutes)
    day_seconds: Dict[str, float] = {}
    while cursor < end:
        next_midnight = datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time())
        step_end = min(end, next_midnight)
        key = cursor.strftime("%Y-%m-%d")
        day_seconds[key] = day_seconds.get(key, 0.0) + (step_end - cursor).total_seconds()
        cursor = step_end
    days = list(day_seconds)
    out: Dict[str, int] = {}
    assigned = 0
    for day in days[:-1]:
        minutes = int(day_seconds[day] // 60)
        if minutes:
            out[day] = minutes
            assigned += minutes
    last = total_minutes - assigned
    if last:
        out[days[-1]] = out.get(days[-1], 0) + last
    return out


def format_bytes(n: float) -> str:
    n = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def _cairo_rounded_rect(cr, x: float, y: float, w: float, h: float, r: float):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def http_get_json(url: str, headers: Dict[str, str], timeout: int = 10, retries: int = 3) -> Optional[dict]:
    delay = 0.5
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except Exception as e:
            last_exc = e
            # a client error (404, 403 rate limit...) answers the same
            # again - asking twice more only burns GitHub's 60 lookups an hour
            if isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code not in (408, 429):
                logger.warning(f"HTTP GET failed ({url}): {e}")
                return None
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    logger.warning(f"HTTP GET failed after {retries} attempts ({url}): {last_exc}")
    return None


# Debian and Ubuntu install games' programs here (steam, gamemoderun,
# gamescope...), and not every session has it on PATH.
GAME_BIN_DIRS = ("/usr/games", "/usr/local/games")


def which(name: str) -> Optional[str]:
    """shutil.which, also looking in GAME_BIN_DIRS."""
    return shutil.which(name) or shutil.which(name, path=os.pathsep.join(GAME_BIN_DIRS))


def immutable_system() -> Optional[str]:
    """The system's name when its packages can't be changed the usual
    way - Fedora Atomic (Silverblue, Kinoite, Bazzite, Bluefin) and other
    rpm-ostree systems, SteamOS, openSUSE's transactional ones (Aeon,
    Kalpa, MicroOS) - else None. /etc stays writable on all of them."""
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text = ""
    info: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k.strip()] = v.strip().strip('"')
    name = info.get("NAME") or info.get("ID") or "Linux"
    if os.path.exists("/run/ostree-booted"):
        return name
    if info.get("ID") == "steamos":
        return name
    variant = info.get("VARIANT_ID", "").lower()
    if os.path.exists("/usr/sbin/transactional-update") and (
            "microos" in f"{info.get('ID', '')} {variant}" or variant in ("aeon", "kalpa")):
        return name
    return None


# The package manager that's actually there, for when /etc/os-release names
# no known family: Garuda only says ID=garuda unless its own hook adds
# ID_LIKE, and small derivatives (Drauger OS...) may leave ID_LIKE out.
# apt-get / dnf only count with their package database tool next to them
# (ALT Linux has an apt-get that installs RPMs).
_FAMILY_TOOLS = (
    ("arch", ("pacman",)),
    ("debian", ("apt-get", "dpkg")),
    ("suse", ("zypper", "rpm")),
    ("fedora", ("dnf", "rpm")),
    ("alpine", ("apk",)),
    ("void", ("xbps-install",)),
)


def family_from_tools() -> str:
    """arch / debian / suse / fedora / alpine / void by the package
    manager on PATH, or "unknown"."""
    for family, tools in _FAMILY_TOOLS:
        if all(shutil.which(t) for t in tools):
            return family
    return "unknown"


def detect_linux_distro_family() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        text = ""
    info: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            info[k.strip()] = v.strip().strip('"')
    ident = f"{info.get('ID', '')} {info.get('ID_LIKE', '')}".lower()
    if "arch" in ident:
        return "arch"
    if "fedora" in ident or "rhel" in ident:
        return "fedora"
    if "suse" in ident:
        return "suse"
    if "debian" in ident or "ubuntu" in ident:
        return "debian"
    family = family_from_tools()
    # the callers only know these four
    return family if family in ("arch", "fedora", "suse", "debian") else "unknown"
