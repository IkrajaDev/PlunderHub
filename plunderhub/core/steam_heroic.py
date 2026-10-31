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


import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.core.persistence import load_json
from plunderhub.core.scan_paths import FLATPAK_STEAM_HOME, HEROIC_CONFIG_ROOTS, STEAM_CANDIDATES
from plunderhub.logging_setup import logger


def steam_roots() -> List[Path]:
    """Each Steam install once: ~/.steam/steam and ~/.steam/root are
    normally symlinks to ~/.local/share/Steam, and listing all three
    scanned and watched the same library three times over."""
    roots = []
    seen = set()
    for p in STEAM_CANDIDATES:
        if not p.exists():
            continue
        try:
            real = p.resolve()
        except OSError:
            real = p
        if real in seen:
            continue
        seen.add(real)
        roots.append(p)
    return roots

def steam_libraries() -> set:
    roots = steam_roots()
    libraries = set(roots)
    for root in roots:
        libraries.update(steam_library_paths(root))
    return libraries


def steam_library_paths(root: Path) -> List[Path]:
    """The libraries a Steam install's libraryfolders.vdf lists, as host
    paths. Flathub's Steam writes them as seen inside its sandbox, where
    the home folder is FLATPAK_STEAM_HOME - so its own library reads as
    ~/.local/share/Steam, which on the host is native Steam's (or
    nothing). Paths outside home (/mnt, /media, ...) are the same on both
    sides."""
    libvdf = root / "steamapps" / "libraryfolders.vdf"
    if not libvdf.exists():
        return []
    try:
        libs = parse_libraryfolders_vdf(libvdf)
    except OSError:
        return []
    if not _is_flatpak_steam_root(root):
        return libs
    home = Path.home()
    mapped_libs = []
    for lib in libs:
        try:
            rel = lib.relative_to(home)
        except ValueError:
            mapped_libs.append(lib)
            continue
        if rel.parts[:2] == (".var", "app"):
            mapped_libs.append(lib)
            continue
        mapped = FLATPAK_STEAM_HOME / rel
        mapped_libs.append(mapped if mapped.exists() else lib)
    return mapped_libs


def _is_flatpak_steam_root(root: Path) -> bool:
    try:
        real_root, real_flatpak = root.resolve(), FLATPAK_STEAM_HOME.resolve()
    except OSError:
        return False
    return real_root == real_flatpak or real_flatpak in real_root.parents

def heroic_roots() -> List[Path]:
    roots = []
    for base in HEROIC_CONFIG_ROOTS:
        for rel in [
            # Heroic 2.x keeps its own Legendary config here; the plain
            # ~/.config/legendary below is only used by older Heroic
            # builds and by standalone Legendary. Both have a "legendary"
            # parent folder, so a game's uid is the same either way.
            "heroic/legendaryConfig/legendary/installed.json",
            "legendary/installed.json",
            "heroic/gog_store/installed.json",
            "heroic/nile_config/nile/installed.json",
            "heroic/zoom_store/installed.json",
            "heroic/sideload_apps/library.json",
        ]:
            p = base / rel
            if p.exists():
                roots.append(p)
    return roots

def parse_libraryfolders_vdf(path: Path) -> List[Path]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    libs: List[Path] = []
    for match in re.finditer(r'"path"\s*"([^"]+)"', text):
        libs.append(Path(match.group(1).replace('\\\\', '/')))
    return libs

def _vdf_unescape(value: str) -> str:
    return re.sub(r'\\(.)', lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), value)


def parse_appmanifest(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        # A manifest can genuinely vanish between the glob() that
        # found it and this read - Steam deleting/updating the game
        # mid-scan, a race rather than a bug - treated the same as any
        # other "couldn't make sense of this file" case: skip just
        # this one game, not the whole Steam scan around it.
        return None
    appid = re.search(r'"appid"\s*"(\d+)"', text)
    # Steam writes a " in a value as \" and a \ as \\ (KeyValues text)
    name = re.search(r'"name"\s*"((?:[^"\\]|\\.)+)"', text)
    installdir = re.search(r'"installdir"\s*"((?:[^"\\]|\\.)+)"', text)
    if not appid:
        return None
    # Steam writes the manifest when an install is queued: until it is
    # fully installed once (StateFlags bit 4, or installed depots kept
    # during an update) it isn't a game to play yet
    flags = re.search(r'"StateFlags"\s*"(\d+)"', text)
    if flags and not int(flags.group(1)) & 4 and not re.search(r'"InstalledDepots"\s*\{\s*"', text):
        return None
    return {
        "appid": appid.group(1),
        "name": _vdf_unescape(name.group(1)) if name else f"Steam app {appid.group(1)}",
        "installdir": _vdf_unescape(installdir.group(1)) if installdir else None,
    }


def steam_registry_path() -> Optional[Path]:
    """The registry.vdf of the Steam in use: the newest one - a native
    Steam's ~/.steam one stays behind (its Running keys frozen) beside a
    Flatpak Steam, or after native Steam was removed."""
    candidates = [Path.home() / ".steam" / "registry.vdf"]
    candidates += [root.parent / "registry.vdf" for root in STEAM_CANDIDATES]
    candidates += [root / "registry.vdf" for root in STEAM_CANDIDATES]
    best, best_mtime = None, -1.0
    for candidate in candidates:
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = candidate, mtime
    return best


_STEAM_REGISTRY_CACHE: Optional[Tuple[Tuple[str, float, int], dict]] = None


def _read_steam_registry_cached(path: Path) -> Optional[dict]:
    global _STEAM_REGISTRY_CACHE
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        return None
    if _STEAM_REGISTRY_CACHE is not None and _STEAM_REGISTRY_CACHE[0] == key:
        return _STEAM_REGISTRY_CACHE[1]
    try:
        data = parse_vdf_text(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as e:
        logger.debug(f"Couldn't read Steam registry.vdf at {path}: {e}")
        return None
    _STEAM_REGISTRY_CACHE = (key, data)
    return data


def is_steam_appid_running(appid: str) -> Optional[bool]:
    path = steam_registry_path()
    if path is None:
        return None
    data = _read_steam_registry_cached(path)
    if data is None:
        return None
    running = _vdf_get_ci(data, "HKCU", "Software", "Valve", "Steam", "apps", str(appid), "Running")
    if running is None:
        return None
    return str(running).strip() == "1"


def parse_vdf_text(text: str) -> dict:
    length = len(text)

    def skip_ws(p: int) -> int:
        while p < length:
            c = text[p]
            if c in ' \t\r\n':
                p += 1
                continue
            if c == '/' and p + 1 < length and text[p + 1] == '/':
                nl = text.find('\n', p)
                p = length if nl == -1 else nl + 1
                continue
            break
        return p

    def read_string(p: int) -> Tuple[str, int]:
        p += 1
        out: List[str] = []
        while p < length:
            c = text[p]
            if c == '\\' and p + 1 < length:
                out.append(text[p + 1])
                p += 2
                continue
            if c == '"':
                return ''.join(out), p + 1
            out.append(c)
            p += 1
        return ''.join(out), p

    def parse_object(p: int) -> Tuple[dict, int]:
        obj: dict = {}
        while True:
            p = skip_ws(p)
            if p >= length or text[p] == '}':
                return obj, p + 1
            if text[p] != '"':
                p += 1
                continue
            key, p = read_string(p)
            p = skip_ws(p)
            if p < length and text[p] == '{':
                value, p = parse_object(p + 1)
            elif p < length and text[p] == '"':
                value, p = read_string(p)
            else:
                value = ""
            obj[key] = value

    try:
        p = skip_ws(0)
        if p < length and text[p] == '"':
            _root_key, p = read_string(p)
            p = skip_ws(p)
            if p < length and text[p] == '{':
                root, _ = parse_object(p + 1)
                return root
    except Exception as e:
        logger.debug(f"parse_vdf_text: couldn't parse VDF text, returning empty dict: {e}")
    return {}


def _vdf_get_ci(obj, *keys: str):
    current = obj
    for key in keys:
        if not isinstance(current, dict):
            return None
        match = None
        for k, v in current.items():
            if k.lower() == key.lower():
                match = v
                break
        if match is None:
            return None
        current = match
    return current


def _steamid64_to_accountid(steamid64: str) -> Optional[int]:
    try:
        return int(steamid64) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return None


def steam_localconfig_path() -> Optional[Path]:
    candidates: List[Path] = []
    for root in STEAM_CANDIDATES:
        if not root.exists():
            continue
        loginusers = root / "config" / "loginusers.vdf"
        if loginusers.exists():
            try:
                data = parse_vdf_text(loginusers.read_text(encoding="utf-8", errors="ignore"))
                for steamid64, info in data.items():
                    if not isinstance(info, dict):
                        continue
                    if str(_vdf_get_ci(info, "MostRecent")) == "1":
                        accountid = _steamid64_to_accountid(steamid64)
                        if accountid is not None:
                            path = root / "userdata" / str(accountid) / "config" / "localconfig.vdf"
                            if path.exists():
                                return path
            except Exception as e:
                logger.debug(f"Couldn't read Steam loginusers.vdf at {loginusers}: {e}")
        userdata = root / "userdata"
        if userdata.exists():
            for sub in userdata.iterdir():
                cfg = sub / "config" / "localconfig.vdf"
                if cfg.exists():
                    candidates.append(cfg)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def read_steam_playtimes() -> Dict[str, Tuple[int, Optional[int]]]:
    path = steam_localconfig_path()
    if path is None:
        return {}
    try:
        data = parse_vdf_text(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    apps = _vdf_get_ci(data, "Software", "Valve", "Steam", "apps")
    if not isinstance(apps, dict):
        return {}
    result: Dict[str, Tuple[int, Optional[int]]] = {}
    for appid, info in apps.items():
        if not isinstance(info, dict):
            continue
        playtime_raw = _vdf_get_ci(info, "Playtime")
        if playtime_raw is None:
            continue
        try:
            minutes = int(float(playtime_raw))
        except (TypeError, ValueError):
            continue
        last_played = None
        last_played_raw = _vdf_get_ci(info, "LastPlayed")
        if last_played_raw is not None:
            try:
                ts = int(float(last_played_raw))
                if ts > 0:
                    last_played = ts
            except (TypeError, ValueError):
                pass
        result[str(appid)] = (minutes, last_played)
    return result


def read_heroic_playtimes() -> Dict[str, Tuple[int, Optional[int]]]:
    result: Dict[str, Tuple[int, Optional[int]]] = {}
    for base in HEROIC_CONFIG_ROOTS:
        path = base / "heroic" / "store" / "timestamp.json"
        data = load_json(path)
        if not isinstance(data, dict):
            continue
        for game_id, info in data.items():
            if not isinstance(info, dict):
                continue
            try:
                minutes = int(float(info.get("totalPlayed")))
            except (TypeError, ValueError):
                continue
            last_played = None
            last_played_raw = info.get("lastPlayed")
            if isinstance(last_played_raw, str) and last_played_raw:
                try:
                    dt = datetime.fromisoformat(last_played_raw.replace("Z", "+00:00"))
                    last_played = int(dt.timestamp())
                except ValueError:
                    pass
            result[str(game_id)] = (minutes, last_played)
    return result
