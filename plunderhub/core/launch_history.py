# PlunderHub - a GTK3 game launcher for Linux
# Copyright (C) 2026 IkrajaDev
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""What a game ran with, launch after launch - for "What changed?".

Every launch from PlunderHub writes a snapshot (record_start, worker
thread): the Proton it runs on (and the release a "Latest" held then),
its launch options, the game's version, the GPU driver, the kernel. When
the session is over record_end says how long it ran and how it ended:

    ok        it ran and ended (closed in the game, or by PlunderHub)
    failed    it didn't start (the launcher said so, or it was gone at once)
    crashed   it ended with an error code / a crash signal

A "good" launch is one that ran a while and ended ok - the one a failed
launch is compared with (diff)."""

import json
import mmap
import os
import platform
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.constants import APP_VERSION, CONFIG_DIR
from plunderhub.logging_setup import logger

HISTORY_DIR = CONFIG_DIR / "launch_history"
KEEP = 30
GOOD_SECONDS = 120          # ran at least this long and ended ok: a good launch
# the things a snapshot holds, in the order the window lists them
FIELDS = ("proton", "options", "game", "driver", "kernel", "plunderhub")

_LOCK = threading.Lock()


def _path(uid: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", uid)
    return HISTORY_DIR / f"{safe}.json"


def load(uid: str) -> List[Dict]:
    """The game's sessions, oldest first."""
    try:
        data = json.loads(_path(uid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return [s for s in sessions or [] if isinstance(s, dict) and isinstance(s.get("snap"), dict)]


def _save(uid: str, sessions: List[Dict]):
    path = _path(uid)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"sessions": sessions[-KEEP:]}, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def record_start(uid: str, started: float, snap: Dict):
    with _LOCK:
        try:
            sessions = [s for s in load(uid) if s.get("at") != started]
            sessions.append({"at": started, "snap": snap, "ran": None, "result": None})
            _save(uid, sessions)
        except OSError as e:
            logger.warning(f"Launch history of {uid} not written: {e}")


def record_end(uid: str, started: float, ran: float, result: str) -> Optional[Dict]:
    """Closes the session started at `started`; returns it (None when its
    snapshot never got written)."""
    with _LOCK:
        try:
            sessions = load(uid)
            for session in reversed(sessions):
                if session.get("at") == started:
                    session["ran"] = round(max(0.0, ran))
                    session["result"] = result
                    _save(uid, sessions)
                    return session
        except OSError as e:
            logger.warning(f"Launch history of {uid} not written: {e}")
    return None


def forget(uid: str):
    with _LOCK:
        try:
            _path(uid).unlink(missing_ok=True)
        except OSError:
            pass


def is_good(session: Dict) -> bool:
    return session.get("result") == "ok" and (session.get("ran") or 0) >= GOOD_SECONDS


def last_good(sessions: List[Dict], before: Optional[float] = None) -> Optional[Dict]:
    for session in reversed(sessions):
        if before is not None and session.get("at", 0) >= before:
            continue
        if is_good(session):
            return session
    return None


def diff(old: Optional[Dict], new: Dict) -> List[Tuple[str, str, str, bool]]:
    """(field, old text, new text, changed) for every field either has."""
    rows = []
    for name in FIELDS:
        now = describe(new, name)
        was = describe(old, name) if old is not None else ""
        if not now and not was:
            continue
        rows.append((name, was, now, old is not None and was != now))
    return rows


def describe(snap: Optional[Dict], name: str) -> str:
    if not snap:
        return ""
    if name == "proton":
        proton = snap.get("proton") or {}
        if proton.get("native"):
            return "native"
        label = proton.get("name") or ""
        tag = proton.get("tag") or ""
        if tag and tag not in label:
            return f"{label} ({tag})" if label else tag
        return label
    return str(snap.get(name) or "")


# --- what the machine runs -------------------------------------------------------------------

_driver_cache: Dict[str, Tuple[float, str]] = {}
_LIB_DIRS = ("/usr/lib", "/usr/lib64", "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu",
             "/lib/x86_64-linux-gnu", "/run/host/usr/lib", "/run/host/usr/lib64")


def gpu_driver() -> str:
    """The GPU driver games get: NVIDIA's version, or the Mesa release of
    the Vulkan drivers installed (read from the library itself - the same
    on every distro, no package manager asked)."""
    try:
        nvidia = Path("/sys/module/nvidia/version").read_text().strip()
        if nvidia:
            return f"NVIDIA {nvidia}"
    except OSError:
        pass
    names = []
    for icd_dir in ("/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d"):
        try:
            for icd in sorted(Path(icd_dir).glob("*.json")):
                lib = (json.loads(icd.read_text()).get("ICD") or {}).get("library_path") or ""
                if lib and lib not in names and ("lvp" not in icd.name and "lvp" not in lib):
                    names.append(lib)
        except (OSError, ValueError):
            continue
    for lib in names:
        candidates = [Path(lib)] if lib.startswith("/") else [Path(d) / lib for d in _LIB_DIRS]
        for path in candidates:
            found = _mesa_in(path)
            if found:
                return found
    return ""


_FLATPAK_ROOTS = (Path.home() / ".local/share/flatpak", Path("/var/lib/flatpak"))


def flatpak_gpu_driver(app_id: str) -> str:
    """The Mesa a Flatpak launcher's games get: its runtime's GL
    extension (org.freedesktop.Platform.GL.default, the runtime's branch),
    not the system's. NVIDIA's own driver is the system's version anyway.
    "" when it can't be told."""
    if Path("/sys/module/nvidia/version").exists():
        return ""
    runtime = ""
    for root in _FLATPAK_ROOTS:
        try:
            for line in (root / "app" / app_id / "current/active/metadata").read_text().splitlines():
                if line.startswith("runtime="):
                    runtime = line.split("=", 1)[1].strip()
                    break
        except OSError:
            continue
        if runtime:
            break
    parts = runtime.split("/")
    if len(parts) != 3 or parts[0] != "org.freedesktop.Platform":
        return ""       # a GNOME / KDE runtime: its GL branch isn't its own number
    arch, branch = parts[1], parts[2]
    for root in _FLATPAK_ROOTS:
        for variant in (f"{branch}-extra", branch):
            lib = root / "runtime/org.freedesktop.Platform.GL.default" / arch / variant / "active/files/lib"
            try:
                found = sorted(lib.glob("libvulkan_*.so")) + sorted(lib.glob("*/libvulkan_*.so"))
            except OSError:
                continue
            for path in found:
                if "lvp" in path.name:
                    continue
                version = _mesa_in(path)
                if version:
                    return version
    return ""


def _mesa_in(path: Path) -> str:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return ""
    cached = _driver_cache.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    found = ""
    try:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            # the release alone: a distro's suffix ("-0ubuntu0.24.04.4") is
            # kept in pieces in the library, read back garbled
            match = re.search(rb"Mesa (\d+\.\d+(?:\.\d+)?)", m)
            if match:
                found = "Mesa " + match.group(1).decode("ascii", "replace")
    except (OSError, ValueError):
        found = ""
    _driver_cache[str(path)] = (stamp, found)
    return found


def machine() -> Dict[str, str]:
    return {"driver": gpu_driver(), "kernel": platform.release(), "plunderhub": APP_VERSION}


def take(app, game) -> Dict:
    """The snapshot of what the game is about to run with (worker thread:
    files only)."""
    snap: Dict = dict(machine())
    snap["taken"] = time.time()
    try:
        from plunderhub.core.perf_log import flatpak_id
        app_id = flatpak_id(game)
        driver = flatpak_gpu_driver(app_id) if app_id else ""
        if driver:
            snap["driver"] = f"{driver} (Flatpak)"
    except Exception as e:
        logger.debug(f"Launch snapshot of {game.title}: Flatpak driver not read: {e}")
    try:
        from plunderhub.core import proton_store
        from plunderhub.core.launch_options import to_command_line
        from plunderhub.ui.proton_page import _game_proton
        entry = _game_proton(game)
        if entry is not None:
            name = entry.proton_name()
            tag = proton_store.latest_tag(name) or _build_tag(name)
            snap["proton"] = {"name": name, "tag": tag or "", "choice": entry.choice,
                              "native": bool(entry.native), "default": not entry.choice}
            try:
                snap["options"] = to_command_line(entry.backend.load(game)).strip()
            except Exception:
                pass
        if not snap.get("proton"):
            runner = app._resolve_runner_version(game)
            if runner:
                snap["proton"] = {"name": str(runner), "tag": "", "choice": None, "native": False}
    except Exception as e:
        logger.debug(f"Launch snapshot of {game.title}: Proton not read: {e}")
    try:
        snap["game"] = app._resolve_game_version(game) or ""
    except Exception:
        snap["game"] = ""
    if not snap["game"] and game.install_dir:
        try:
            # no version anywhere: when its files last changed (an update)
            when = os.stat(game.install_dir).st_mtime
            snap["game"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(when))
            snap["game_is_date"] = True
        except OSError:
            pass
    return snap


def _build_tag(name: str) -> str:
    """The release a Proton folder holds (its version file), wherever a
    launcher keeps it."""
    if not name:
        return ""
    from plunderhub.core import proton_store
    build = proton_store.find(name)
    if build is not None and build.tag:
        return build.tag
    for folder in [proton_store.store_dir()] + proton_store.targets():
        tag = proton_store._folder_tag(folder / name)
        if tag:
            return tag
    return ""


# --- Proton's own log ------------------------------------------------------------------------

_ERROR_LINE = re.compile(r"(\berr:|Unhandled exception|unhandled page fault|wine: |Assertion|"
                         r"segmentation fault|core dumped|\bfatal\b)", re.IGNORECASE)


def proton_log_since(since: float) -> Optional[Path]:
    """The Proton log (PROTON_LOG=1: ~/steam-<id>.log, umu's the same; a
    Flatpak launcher's in ~/.var/app/<id>/) the launch at `since` wrote -
    the newest one changed after it."""
    found = []
    # a Flatpak launcher's home is its own folder (Steam's --persist=.)
    homes = [Path.home()] + sorted((Path.home() / ".var/app").glob("*"))
    for pattern in ("steam-*.log", "umu-*.log", "proton-*.log"):
        for path in (p for home in homes for p in home.glob(pattern)):
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            if stamp >= since - 2:
                found.append((stamp, path))
    return max(found)[1] if found else None


def log_errors(path: Path, most: int = 14) -> List[str]:
    """The lines of a Proton log that say what went wrong - the last ones,
    each once."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 4 * 1024 * 1024))
            lines = f.read().splitlines()
    except OSError:
        return []
    picked: List[str] = []
    seen = set()
    for line in reversed(lines):
        if not _ERROR_LINE.search(line):
            continue
        text = re.sub(r"^[\d.:]+:?\s*", "", line.strip())[:220]
        key = re.sub(r"0x[0-9a-fA-F]+|\d+", "#", text)
        if key in seen:
            continue
        seen.add(key)
        picked.append(text)
        if len(picked) >= most:
            break
    return list(reversed(picked))
