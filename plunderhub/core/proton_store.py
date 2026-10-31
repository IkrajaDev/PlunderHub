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

"""PlunderHub's own Proton builds (Proton-GE and Proton-CachyOS), for every
launcher at once.

One real copy of each build lives in the store,
~/.local/share/plunderhub/proton/<name>. Every place a launcher looks for
Proton gets the same files as hardlinks (no extra space, and Flatpak
sandboxes see them - a symlink pointing out of a sandbox they would not
follow); across filesystems a reflink copy, then a plain copy:

  ~/.local/share/Steam/compatibilitytools.d   Steam, Lutris, Faugus, Bottles
                                              (native and Flatpak) - made
                                              even without Steam installed
  Flatpak Steam's compatibilitytools.d        Steam Flatpak
  ~/.config/heroic/tools/proton (+ Flatpak)   Heroic: it reads Steam's folder
                                              only when Steam is installed
  Flatpak Bottles' runners                    Bottles Flatpak (no home access)

They are made for launchers that aren't installed yet too, so one
installed later sees the builds straight away (PlunderHubNotes/
proton-manager-plan.md has the research behind each place).

A family's "Latest" (Proton-GE Latest - the same name ProtonPlus uses, folder
and internal name in compatibilitytool.vdf, so games set to it keep working)
always holds the newest release: a newer one is downloaded, its sha512
checked, unpacked beside the old one and swapped in at the end - never while
a game runs on it (the swap waits: finish_pending()).

Every function here blocks: the UI runs them on a worker thread."""

import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from plunderhub.constants import CACHE_DIR
from plunderhub.logging_setup import logger

MARKER = ".plunderhub-proton.json"
STAGING = ".plunderhub-staging-"
RETIRED = ".plunderhub-retired-"
DOWNLOADS = ".downloads"
RELEASES_TTL_S = 6 * 3600
USER_AGENT = "PlunderHub (https://github.com/IkrajaDev/PlunderHub)"
# A stalled / dropped download goes on from where it got to this many times
# in all, this many seconds apart.
_FETCH_TRIES = 4
_FETCH_PAUSE_S = 3
# Held while the store changes (an install, a swap, a removal): reconcile()
# and finish_pending() skip a round rather than wait - or sweep away a
# download in progress.
_BUSY = threading.Lock()


@dataclass(frozen=True)
class Family:
    id: str
    repo: str
    latest_name: str
    auto: bool              # its Latest downloads and updates by itself


FAMILIES: Dict[str, Family] = {
    "ge": Family("ge", "GloriousEggroll/proton-ge-custom", "Proton-GE Latest", True),
    "cachyos": Family("cachyos", "CachyOS/proton-cachyos", "Proton-CachyOS Latest", False),
}


@dataclass
class Release:
    family: str
    tag: str
    date: str               # YYYY-MM-DD
    url: str                # the archive for this machine
    size: int               # bytes, the download
    sha_url: Optional[str]  # its .sha512sum


@dataclass
class Build:
    family: str
    name: str               # the folder, as launchers list it
    tag: str                # the release it holds
    latest: bool
    path: Path


class Cancelled(Exception):
    pass


class ProtonError(Exception):
    """kind says what went wrong, for the UI to tell in words:
    rate_limit (minutes: till GitHub answers again), offline, http
    (code), space, checksum, archive, in_use - or "" (the message)."""

    def __init__(self, message: str, kind: str = "", **detail):
        super().__init__(message)
        self.kind = kind
        self.detail = detail


def _network_error(e: Exception, what: str) -> ProtonError:
    """A failed GitHub call as a ProtonError: its hourly limit used up
    (60 calls an hour from one address without a token - the reply says
    when it resets), no connection, or another HTTP error."""
    if isinstance(e, urllib.error.HTTPError):
        headers = e.headers or {}
        if e.code == 429 or (e.code == 403 and (headers.get("X-RateLimit-Remaining") == "0"
                                                or headers.get("Retry-After"))):
            wait = 0.0
            try:
                if headers.get("Retry-After"):
                    wait = float(headers["Retry-After"])
                elif headers.get("X-RateLimit-Reset"):
                    wait = float(headers["X-RateLimit-Reset"]) - time.time()
            except ValueError:
                pass
            minutes = max(1, int(wait / 60 + 0.999)) if wait > 0 else 0
            return ProtonError(f"GitHub's rate limit is used up ({what}); resets in {minutes} min",
                               "rate_limit", minutes=minutes)
        return ProtonError(f"GitHub answered HTTP {e.code} ({what})", "http", code=e.code)
    return ProtonError(f"GitHub unreachable ({what}): {e}", "offline")


# --- where things are ---------------------------------------------------------------

def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or _home() / ".local/share")


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config")


def store_dir() -> Path:
    return _data_home() / "plunderhub" / "proton"


def targets() -> List[Path]:
    """Every folder a launcher looks in for Proton, each once, whether its
    launcher is installed or not (see the module's docstring)."""
    home = _home()
    flatpak = home / ".var/app"
    candidates = [
        _data_home() / "Steam/compatibilitytools.d",
        # Lutris and Bottles look here whatever XDG_DATA_HOME says
        home / ".local/share/Steam/compatibilitytools.d",
        flatpak / "com.valvesoftware.Steam/.local/share/Steam/compatibilitytools.d",
        _config_home() / "heroic/tools/proton",
        home / ".config/heroic/tools/proton",
        flatpak / "com.heroicgameslauncher.hgl/config/heroic/tools/proton",
        flatpak / "com.usebottles.bottles/data/bottles/runners",
    ]
    # a Steam that keeps its data elsewhere (Debian's package, an older
    # Flatpak's data/Steam): there too, when it's really there
    for root in (home / ".steam/debian-installation", flatpak / "com.valvesoftware.Steam/data/Steam",
                 flatpak / "io.github.heroicgameslauncher.hgl/config/heroic"):
        if root.is_dir() and not root.is_symlink():
            candidates.append(root / ("tools/proton" if root.name == "heroic" else "compatibilitytools.d"))
    found, seen = [], set()
    for path in candidates:
        key = _real(path)
        if key not in seen:
            seen.add(key)
            found.append(path)
    return found


def _real(path: Path) -> str:
    """The real path of path, or of what of it exists plus the rest."""
    parts = []
    probe = path
    while not probe.exists() and probe != probe.parent:
        parts.append(probe.name)
        probe = probe.parent
    try:
        real = probe.resolve()
    except OSError:
        real = probe
    for part in reversed(parts):
        real = real / part
    return str(real)


# --- what's installed -------------------------------------------------------------

def _read_marker(folder: Path) -> Optional[Dict]:
    try:
        data = json.loads((folder / MARKER).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def installed() -> List[Build]:
    builds = []
    root = store_dir()
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return []
    for folder in entries:
        if folder.name.startswith(".") or not folder.is_dir():
            continue
        marker = _read_marker(folder)
        if marker is None:
            continue
        builds.append(Build(marker.get("family", ""), folder.name, marker.get("tag", ""),
                            bool(marker.get("latest")), folder))
    return builds


def find(name: str) -> Optional[Build]:
    return next((b for b in installed() if b.name == name), None)


def latest_build(family: str) -> Optional[Build]:
    return find(FAMILIES[family].latest_name)


def build_size(build: Build) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(build.path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


# --- releases (GitHub) --------------------------------------------------------------

def _cpu_level_v3() -> bool:
    """x86-64-v3: what CachyOS's _v3 build needs (AVX2 and friends)."""
    try:
        text = Path("/proc/cpuinfo").read_text(errors="replace")
    except OSError:
        return False
    match = re.search(r"^flags\s*:(.*)$", text, re.MULTILINE)
    if not match:
        return False
    flags = set(match.group(1).split())
    return {"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "movbe", "xsave"} <= flags and (
        "abm" in flags or "lzcnt" in flags)


def _machine() -> str:
    return os.uname().machine


def pick_asset(family: str, assets: List[Dict]) -> Optional[Tuple[Dict, Optional[Dict]]]:
    """The archive for this machine, and its .sha512sum."""
    arm = _machine() in ("aarch64", "arm64")
    names = {a.get("name", ""): a for a in assets}
    archives = [n for n in names if n.endswith((".tar.gz", ".tar.xz", ".tar.zst"))]

    def arch_ok(name: str) -> bool:
        is_arm = "aarch64" in name or "arm64" in name
        return is_arm == arm

    archives = [n for n in archives if arch_ok(n)]
    if family == "cachyos" and not arm:
        v3 = [n for n in archives if "x86_64_v3" in n]
        plain = [n for n in archives if "x86_64_v3" not in n]
        archives = (v3 if _cpu_level_v3() and v3 else plain) or archives
    if not archives:
        return None
    archive = archives[0]
    stem = re.sub(r"\.tar\.(gz|xz|zst)$", "", archive)
    sha = names.get(stem + ".sha512sum")
    return names[archive], sha


def _api(path: str) -> object:
    """A GitHub API answer; raises ProtonError when there's none (a
    used-up rate limit isn't tried again - it wouldn't help)."""
    url = f"https://api.github.com/repos/{path}"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": USER_AGENT})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                return json.load(resp)
        except (urllib.error.URLError, OSError, ValueError) as e:
            problem = _network_error(e, path) if not isinstance(e, ValueError) else \
                ProtonError(f"GitHub's answer isn't JSON ({path})", "http", code=0)
            # a client error (404, a blocked 403) answers the same again
            client_error = isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 \
                and e.code not in (408, 429)
            if problem.kind == "rate_limit" or client_error or attempt == 1:
                logger.warning(str(problem))
                raise problem from e
            time.sleep(1)
    raise ProtonError(f"GitHub unreachable ({path})", "offline")


def _release_from(family: str, data: Dict) -> Optional[Release]:
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    picked = pick_asset(family, data.get("assets") or [])
    if picked is None:
        return None
    archive, sha = picked
    return Release(family, str(data.get("tag_name", "")), str(data.get("published_at", ""))[:10],
                   archive.get("browser_download_url", ""), int(archive.get("size") or 0),
                   sha.get("browser_download_url") if sha else None)


def newest_release(family: str) -> Optional[Release]:
    """The family's newest release (one API call), None when it has no
    build for this machine; raises ProtonError when GitHub doesn't answer."""
    data = _api(f"{FAMILIES[family].repo}/releases/latest")
    return _release_from(family, data) if data else None


def releases(family: str, refresh: bool = False) -> List[Release]:
    """Every release (cached for RELEASES_TTL_S; GitHub allows 60 calls an
    hour without a token). Raises ProtonError when GitHub doesn't answer."""
    cache = CACHE_DIR / f"proton-releases-{family}.json"
    if not refresh:
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if time.time() - cached.get("at", 0) < RELEASES_TTL_S:
                return [Release(**r) for r in cached.get("releases", [])]
        except (OSError, ValueError, TypeError):
            pass
    found: List[Release] = []
    for page in range(1, 11):
        try:
            data = _api(f"{FAMILIES[family].repo}/releases?per_page=100&page={page}")
        except ProtonError:
            if page == 1:
                raise
            break
        if not isinstance(data, list):
            if page == 1:
                return []
            break
        found += [r for r in (_release_from(family, d) for d in data) if r is not None]
        if len(data) < 100:
            break
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"at": time.time(), "releases": [r.__dict__ for r in found]}),
                         encoding="utf-8")
    except OSError as e:
        logger.debug(f"Couldn't cache the {family} release list: {e}")
    return found


# --- download, check, unpack -----------------------------------------------------------

Progress = Callable[[str, int, int], None]      # (stage, done, total)


def _fetch(url: str, dest: Path, progress: Optional[Progress], cancel: Optional[threading.Event],
           expected: int = 0):
    """A download that stalls or drops (a slow or flaky line: a read that
    waits 30 s) goes on from where it got to, a few times, before it
    counts as failed - not from zero 15 minutes later."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    done = 0
    total = expected or 0
    tries = _FETCH_TRIES
    with open(part, "wb") as out:
        while True:
            headers = {"User-Agent": USER_AGENT}
            if done:
                headers["Range"] = f"bytes={done}-"
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                            timeout=30) as resp:
                    if done and resp.status != 206:      # no resume: start over
                        out.seek(0)
                        out.truncate()
                        done = 0
                    if not done:
                        total = int(resp.headers.get("Content-Length") or expected or 0)
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress("download", done, total)
                if total and done < total:
                    raise http.client.IncompleteRead(b"", total - done)
                break
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException,
                    ssl.SSLError) as e:
                tries -= 1
                if tries <= 0:
                    raise
                logger.info(f"Download of {dest.name} broke off at {done} bytes ({e}) - going on")
                for _ in range(_FETCH_PAUSE_S * 10):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    time.sleep(0.1)
    os.replace(part, dest)


def _sha512(path: Path, cancel: Optional[threading.Event]) -> str:
    digest = hashlib.sha512()
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = f.read(4 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _unpack(archive: Path, into: Path, cancel: Optional[threading.Event] = None) -> Path:
    """Unpacks archive into into; returns the one folder it holds.
    Stopped (Cancelled) when cancel is set - tar is ended on the spot."""
    into.mkdir(parents=True)
    tar = shutil.which("tar")
    done = False
    if tar:
        proc = subprocess.Popen([tar, "-xf", str(archive), "-C", str(into), "--no-same-owner"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        while True:
            try:
                _out, err = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait()
                    raise Cancelled()
        done = proc.returncode == 0
        if not done:
            logger.info(f"tar couldn't unpack {archive.name} ({(err or '').strip()[:200]}), trying Python's")
            shutil.rmtree(into)
            into.mkdir(parents=True)
    if not done:
        with tarfile.open(archive) as tf:
            if hasattr(tarfile, "data_filter"):
                tf.extractall(into, filter="data")
            else:
                for member in tf.getmembers():
                    target = (into / member.name).resolve()
                    if not str(target).startswith(str(into.resolve()) + os.sep):
                        raise ProtonError(f"unsafe path in {archive.name}: {member.name}", "archive")
                tf.extractall(into)
    folders = [p for p in into.iterdir() if p.is_dir()]
    if len(folders) != 1 or not (folders[0] / "proton").exists():
        raise ProtonError(f"{archive.name} doesn't hold one Proton folder", "archive")
    return folders[0]


def _rename_tool(folder: Path, name: str):
    """Makes the build list as name: Steam (and the launchers reading its
    folder) take the tool's name from compatibilitytool.vdf, not from the
    folder - two releases under one name would clash."""
    vdf = folder / "compatibilitytool.vdf"
    try:
        text = vdf.read_text(encoding="utf-8")
    except OSError:
        return
    text = re.sub(r'("compat_tools"\s*\{\s*)"[^"]*"', lambda m: f'{m.group(1)}"{name}"', text, count=1)
    text = re.sub(r'("display_name"\s*)"[^"]*"', lambda m: f'{m.group(1)}"{name}"', text, count=1)
    # a new file, not written into the old one: it may be hardlinked
    tmp = vdf.with_name(vdf.name + ".new")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, vdf)


def _write_marker(folder: Path, family: str, tag: str, latest: bool):
    (folder / MARKER).write_text(json.dumps({"family": family, "tag": tag, "latest": latest,
                                             "installed": time.strftime("%Y-%m-%d %H:%M")}),
                                 encoding="utf-8")


def needed_space(release: Release) -> int:
    """Download plus unpacked, roughly (GE unpacks to ~3x, CachyOS's xz ~3.5x)."""
    return release.size * 5 + (200 << 20)


def free_space() -> int:
    probe = store_dir()
    while not probe.exists():
        probe = probe.parent
    return shutil.disk_usage(probe).free


def install(release: Release, latest: bool = False, progress: Optional[Progress] = None,
            cancel: Optional[threading.Event] = None) -> Tuple[Build, bool]:
    """Downloads, checks and unpacks release into the store - as the
    family's Latest with latest. Returns the build and whether it's in place
    (False: its old copy is in use by a game; finish_pending() swaps it in
    later). Raises Cancelled / ProtonError."""
    with _BUSY:
        return _install(release, latest, progress, cancel)


def build_name(release: Release) -> str:
    """The folder a release unpacks into - its archive's name: a GE tag
    and folder are one, a CachyOS one isn't (tag cachyos-11.0-...-slr,
    folder proton-cachyos-11.0-...-slr-x86_64_v3)."""
    archive = release.url.rsplit("/", 1)[-1]
    return re.sub(r"\.tar\.(gz|xz|zst)$", "", archive) or release.tag


def _install(release: Release, latest: bool, progress: Optional[Progress],
             cancel: Optional[threading.Event]) -> Tuple[Build, bool]:
    family = FAMILIES[release.family]
    if free_space() < needed_space(release):
        raise ProtonError("not enough free space", "space", need=needed_space(release))
    store = store_dir()
    store.mkdir(parents=True, exist_ok=True)
    archive = store / DOWNLOADS / release.url.rsplit("/", 1)[-1]
    try:
        try:
            _fetch(release.url, archive, progress, cancel, release.size)
            if release.sha_url:
                if progress is not None:
                    progress("check", 0, 1)
                expected = ""
                with urllib.request.urlopen(urllib.request.Request(release.sha_url,
                                                                   headers={"User-Agent": USER_AGENT}),
                                            timeout=30) as resp:
                    words = resp.read().decode("utf-8", "replace").split()
                    expected = words[0].strip().lower() if words else ""     # empty: a mismatch
        except (urllib.error.URLError, ConnectionError, TimeoutError, http.client.HTTPException,
                ssl.SSLError) as e:      # a dropped download, a broken TLS link
            raise _network_error(e, release.url.rsplit("/", 1)[-1]) from e
        if release.sha_url and _sha512(archive, cancel) != expected:
            raise ProtonError(f"{archive.name}: checksum doesn't match", "checksum")
        if progress is not None:
            progress("unpack", 0, 1)
        name = family.latest_name if latest else None
        unpack_dir = store / f"{STAGING}unpack-{os.getpid()}-{int(time.time())}"
        try:
            folder = _unpack(archive, unpack_dir, cancel)
            if cancel is not None and cancel.is_set():
                # the last moment to stop: from here on it goes in place
                raise Cancelled()
            name = name or folder.name
            staging = store / f"{STAGING}{name}"
            if staging.exists():
                shutil.rmtree(staging)
            os.replace(folder, staging)
        finally:
            shutil.rmtree(unpack_dir, ignore_errors=True)
        if latest:
            _rename_tool(staging, name)
        _write_marker(staging, release.family, release.tag, latest)
    finally:
        archive.unlink(missing_ok=True)
        archive.with_name(archive.name + ".part").unlink(missing_ok=True)
        try:
            archive.parent.rmdir()
        except OSError:
            pass
    placed = _swap_in(name)
    return Build(release.family, name, release.tag, latest, store / name), placed


# --- in place, safely -----------------------------------------------------------------------

def _copies(name: str) -> List[Path]:
    """Every folder holding the build name: the store's, the launchers'."""
    return [store_dir() / name] + [t / name for t in targets()]


def in_use(name: str) -> bool:
    """A process running from any copy of the build (a game on it)."""
    roots = [str(p) + os.sep for p in _copies(name) if p.exists()]
    roots += [_real(p) + os.sep for p in _copies(name) if p.exists()]
    if not roots:
        return False
    uid = os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().decode("utf-8", "replace").split("\0")
            try:
                args.append(os.readlink(f"/proc/{pid}/exe"))
            except OSError:
                pass
        except OSError:
            continue
        if any(arg.startswith(root) for arg in args for root in roots):
            return True
    return False


def pending() -> List[str]:
    """Builds downloaded and waiting to be swapped in."""
    try:
        return sorted(p.name[len(STAGING):] for p in store_dir().iterdir()
                      if p.name.startswith(STAGING) and not p.name.startswith(STAGING + "unpack-")
                      and (p / MARKER).exists())
    except OSError:
        return []


def _swap_in(name: str) -> bool:
    if in_use(name):
        logger.info(f"Proton: {name} is in use - the new one waits")
        return False
    store = store_dir()
    staging = store / f"{STAGING}{name}"
    current = store / name
    retired = store / f"{RETIRED}{name}-{int(time.time())}"
    if current.exists():
        os.replace(current, retired)
    os.replace(staging, current)
    try:
        link_everywhere(name)
    finally:
        shutil.rmtree(retired, ignore_errors=True)
    logger.info(f"Proton: {name} in place ({(_read_marker(current) or {}).get('tag', '?')})")
    return True


def finish_pending() -> List[str]:
    """Swaps in the builds that waited for a game to end; returns them."""
    if not _BUSY.acquire(blocking=False):
        return []
    try:
        return _finish_pending()
    finally:
        _BUSY.release()


def _finish_pending() -> List[str]:
    done = []
    for name in pending():
        try:
            if _swap_in(name):
                done.append(name)
        except OSError as e:
            logger.warning(f"Proton: couldn't put {name} in place: {e}")
    return done


# --- the launchers' folders --------------------------------------------------------------

def _link_tree(src: Path, dst: Path):
    """dst as src's twin: hardlinks, else a reflink copy, else a copy."""
    try:
        for dirpath, dirs, files in os.walk(src):
            rel = os.path.relpath(dirpath, src)
            out = dst / rel if rel != "." else dst
            out.mkdir(parents=True, exist_ok=True)
            for name in files + [d for d in dirs if os.path.islink(os.path.join(dirpath, d))]:
                s = os.path.join(dirpath, name)
                d = out / name
                if os.path.islink(s):
                    os.symlink(os.readlink(s), d)
                else:
                    os.link(s, d)
        return "hardlink"
    except OSError as e:
        logger.info(f"Proton: no hardlinks into {dst.parent} ({e}), copying")
        shutil.rmtree(dst, ignore_errors=True)
    cp = shutil.which("cp")
    if cp:
        result = subprocess.run([cp, "-a", "--reflink=auto", str(src), str(dst)], capture_output=True)
        if result.returncode == 0:
            return "copy"
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)
    return "copy"


def _same(store_copy: Path, other: Path) -> bool:
    """other is up to date with the store's copy (same marker file, or the
    same contents of it)."""
    try:
        a, b = store_copy / MARKER, other / MARKER
        if os.stat(a).st_ino == os.stat(b).st_ino and os.stat(a).st_dev == os.stat(b).st_dev:
            return True
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _tag_key(tag: str) -> Tuple[int, ...]:
    """A release's numbers, to compare (GE-Proton11-11 -> 11, 11;
    cachyos-11.0-20261015-slr -> 11, 0, 20261015)."""
    return tuple(int(n) for n in re.findall(r"\d+", tag or ""))


def _folder_tag(folder: Path) -> str:
    """The release in a Proton folder: Proton's own version file
    ("<timestamp> <tag>")."""
    try:
        parts = (folder / "version").read_text(encoding="utf-8", errors="replace").split()
    except OSError:
        return ""
    return parts[1] if len(parts) > 1 else ""


def latest_tag(runner: Optional[str]) -> Optional[str]:
    """The release a family's Latest holds, when runner names one (as a
    launcher shows it: "Proton-GE Latest", "Proton - Proton-GE Latest",
    a path ending in it) - else None."""
    if not runner:
        return None
    for family in FAMILIES.values():
        name = family.latest_name
        if name not in runner:
            continue
        build = find(name)
        if build is not None and build.tag:
            return build.tag
        # someone else's Latest (ProtonPlus, Faugus): its version file
        for folder in [store_dir()] + targets():
            tag = _folder_tag(folder / name)
            if tag:
                return tag
    return None


def _ours_or_replaceable(src: Path, folder: Path, name: str) -> bool:
    """A folder of that name that may go: ours, or another tool's copy of a
    Latest (ProtonPlus and Faugus name GE's the same) that isn't newer than
    ours - never someone's own build."""
    if _read_marker(folder) is not None:
        return True
    if not any(name == f.latest_name for f in FAMILIES.values()):
        return False
    theirs, mine = _tag_key(_folder_tag(folder)), _tag_key((_read_marker(src) or {}).get("tag", ""))
    # Faugus updates its Latest when a game starts: one newer than ours
    # stays, ours catches up at the next check
    return not theirs or theirs <= mine


def link_everywhere(name: str) -> Dict[str, str]:
    """Puts the store's build name into every launcher's folder (made if
    missing), replacing an older copy; returns {folder: how}."""
    src = store_dir() / name
    report = {}
    for target in targets():
        dst = target / name
        try:
            if dst.exists() and _same(src, dst):
                report[str(target)] = "up to date"
                continue
            if dst.exists() and not _ours_or_replaceable(src, dst, name):
                report[str(target)] = "someone else's - left alone"
                continue
            target.mkdir(parents=True, exist_ok=True)
            staging = target / f"{STAGING}{name}"
            shutil.rmtree(staging, ignore_errors=True)
            how = _link_tree(src, staging)
            if dst.exists() or dst.is_symlink():
                retired = target / f"{RETIRED}{name}-{int(time.time())}"
                os.replace(dst, retired)
                shutil.rmtree(retired, ignore_errors=True)
            os.replace(staging, dst)
            report[str(target)] = how
        except OSError as e:
            logger.warning(f"Proton: couldn't put {name} into {target}: {e}")
            report[str(target)] = f"failed: {e}"
    return report


def unlink_everywhere(name: str):
    for target in targets():
        dst = target / name
        if dst.is_dir() and _read_marker(dst) is not None:
            shutil.rmtree(dst, ignore_errors=True)


def reconcile() -> Dict[str, Dict[str, str]]:
    """Every build of the store in every launcher's folder - for one
    installed since, or a folder someone emptied. Cheap when nothing's to do."""
    if not _BUSY.acquire(blocking=False):
        return {}
    try:
        _finish_pending()
        _sweep()
        return {build.name: link_everywhere(build.name) for build in installed()}
    finally:
        _BUSY.release()


def _sweep():
    """Leftovers of an interrupted run."""
    for folder in [store_dir()] + targets():
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            # in the store, a staged build without its marker was cut off
            # between unpacking and being marked - never finished
            if entry.name.startswith(RETIRED) or entry.name.startswith(STAGING + "unpack-") or (
                    entry.name.startswith(STAGING) and (folder != store_dir() or not (entry / MARKER).exists())):
                shutil.rmtree(entry, ignore_errors=True)
    shutil.rmtree(store_dir() / DOWNLOADS, ignore_errors=True)


def remove(name: str):
    """Takes the build out of every launcher's folder and the store."""
    with _BUSY:
        if in_use(name):
            raise ProtonError(f"{name} is in use", "in_use")
        unlink_everywhere(name)
        shutil.rmtree(store_dir() / name, ignore_errors=True)


# --- keeping a Latest up to date ---------------------------------------------------------------

def latest_outdated(family: str) -> Optional[Release]:
    """The newer release the family's Latest should hold, or None (up to
    date). Raises ProtonError when GitHub doesn't answer."""
    release = newest_release(family)
    if release is None:
        return None
    current = latest_build(family)
    waiting = store_dir() / f"{STAGING}{FAMILIES[family].latest_name}"
    waiting_tag = (_read_marker(waiting) or {}).get("tag") if waiting.exists() else None
    if current is not None and current.tag == release.tag or waiting_tag == release.tag:
        return None
    return release
