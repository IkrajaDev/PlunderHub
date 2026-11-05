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

"""Bilge cleaning: what launchers leave behind after a game is gone.
No GTK; everything here reads the disk and blocks - a worker thread.

    steam_prefix  steamapps/compatdata/<appid> of a game no Steam library has
    shader_cache  steamapps/shadercache/<appid>, the same
    prefix        a Wine prefix in Heroic's / Lutris' / Faugus' prefix folder
                  that no installed game of that launcher uses
    proton        a Proton build PlunderHub downloaded that no game and no
                  launcher's default points at (never the Latest)

Rule one: when in doubt, it isn't rubbish. A launcher whose games
couldn't be read, a Steam library that isn't mounted - that launcher's
leftovers aren't looked for at all (`skipped` says why), rather than its
games' folders taken for orphans. Nothing here deletes by itself:
`delete()` does what the person picked, one folder at a time.
"""

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from plunderhub.core.scan_paths import (HEROIC_CONFIG_ROOTS, LUTRIS_CONFIG_DIR_CANDIDATES,
                                        LUTRIS_DATA_DIR_CANDIDATES, faugus_games_path)
from plunderhub.core.steam_heroic import parse_appmanifest, steam_library_paths, steam_roots
from plunderhub.logging_setup import logger

KINDS = ("steam_prefix", "shader_cache", "prefix", "proton")
# Steam's own: compatdata/0 is Steam's default prefix for its tools; ids
# from 2^31 up are non-Steam games added to Steam (shortcuts.vdf)
_STEAM_SHORTCUT_FROM = 2 ** 31
# What of a prefix is the player's (player_files): when in doubt, it is.
# Checked against the Ludusavi manifest (PCGamingWiki's save locations of
# ~18,000 Windows games, 2026-11): see PlunderHubNotes/bilge-cleaning.md.
# In a user folder only what's surely a cache Wine / the driver makes again:
_CACHE_DIRS = {"d3dscache", "nvidia", "amd", "mesa_shader_cache", "crashdumps", "dxcache", "glcache",
               "vkcache", "shadercache"}
# the registry, and Wine's own .ini files games used to write into: these
# go back only into a prefix made again, never over one that's there
SYSTEM_FILES = {"user.reg", "system.reg", "userdef.reg", "win.ini", "system.ini"}
# drive_c/windows as Wine makes it: anything else in there is a game's
WINE_WINDOWS = {"command", "fonts", "globalization", "help", "inf", "installer", "logs", "microsoft.net", "mono",
                "performance", "resources", "security", "system", "system32", "syswow64", "tasks", "temp",
                "winsxs", "twain_32", "media", "cursors", "debug", "servicing", "assembly", "dotnet",
                "explorer.exe", "hh.exe", "notepad.exe", "regedit.exe", "rundll.exe", "twain.dll",
                "twain_32.dll", "winhelp.exe", "winhlp32.exe"}
# drive_c folders of Proton's own
PROTON_OWN = {"openxr", "vrclient"}
# a program's file changed this long after the rest of its folder: written
# by it after it was installed (a save, a setting)
CHANGED_AFTER = 3600
# saves by their folder's or their own name, wherever they are
_SAVE_DIR = re.compile(r"^(saves?|save ?games?|save ?data|saved|savs?|profiles?|userdata|user data|players?)$",
                       re.I)
_SAVE_FILE = re.compile(r"(\.(sav|save|savx|sl2|ess|fos|lsv|rvdata2?|sav\d*)$|^save)", re.I)


@dataclass(eq=False)
class Item:
    kind: str                   # one of KINDS
    launcher: str               # Steam, Heroic, Lutris, Faugus - PlunderHub for a Proton build
    path: Path
    title: str = ""             # the game's name ("" when not known), the build's for a Proton
    appid: str = ""             # Steam's
    size: int = -1              # bytes on disk; -1 until measured
    changed: float = 0.0        # the newest file's time
    saves: int = 0              # player files in it (prefixes only, see player_files)
    saves_newest: float = 0.0
    player: List[Tuple[Path, str]] = field(default_factory=list)   # (file, path in the prefix)
    left_out: List[Tuple[str, int]] = field(default_factory=list)  # what a backup leaves: (drive_c folder, bytes)

    @property
    def safe(self) -> bool:
        """Nothing of the person's can be in it: picked to start with."""
        return self.kind in ("shader_cache", "proton")


@dataclass
class Found:
    items: List[Item] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)   # (launcher, why: unreadable / unmounted:<path>)


# --- Steam -------------------------------------------------------------------------------

def _real(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def steam_leftovers(found: Found) -> None:
    """compatdata and shadercache folders of appids no library holds -
    across every Steam (native, Flatpak) and every library of each. A
    library the vdf lists but that isn't there (an unplugged drive):
    Steam isn't looked at, its games there would look gone."""
    libraries: Dict[Path, Path] = {}
    installed: Set[str] = set()
    for root in steam_roots():
        listed = steam_library_paths(root)
        for lib in listed:
            if not (lib / "steamapps").is_dir():
                found.skipped.append(("Steam", f"unmounted:{lib}"))
                return
        # not the vdf's "apps" lists: Steam updates them late - a game just
        # uninstalled stays listed there for a while; the manifests are
        # what's installed (Steam writes one as soon as an install starts)
        for lib in [root, *listed]:
            libraries.setdefault(_real(lib), lib)
    for lib in libraries.values():
        for manifest in (lib / "steamapps").glob("appmanifest_*.acf"):
            appid = manifest.name[len("appmanifest_"):-len(".acf")]
            data = parse_appmanifest(manifest)
            if data is None and manifest.stat().st_size > 0:
                # a manifest Steam is busy writing: it's installed
                logger.debug(f"Bilge: {manifest} unreadable, counted as installed")
            installed.add(appid)
    for lib in libraries.values():
        for kind, folder in (("steam_prefix", "compatdata"), ("shader_cache", "shadercache")):
            base = lib / "steamapps" / folder
            try:
                entries = list(base.iterdir())
            except OSError:
                continue
            for path in entries:
                name = path.name
                if not name.isdigit() or name == "0" or int(name) >= _STEAM_SHORTCUT_FROM:
                    continue
                if name in installed or path.is_symlink() or not path.is_dir():
                    continue
                found.items.append(Item(kind, "Steam", path, appid=name))


# --- Heroic, Lutris, Faugus: their prefix folders ----------------------------------------

def is_prefix(path: Path) -> bool:
    """A Wine prefix: drive_c in it, or in its pfx (a Proton one)."""
    return (path / "drive_c").is_dir() or (path / "pfx" / "drive_c").is_dir()


def heroic_prefix_folders() -> List[Path]:
    """Where Heroic puts new games' prefixes: the folder its default
    setting names, and that one's parent ("Prefixes/default" holds one
    per game; a person may have pointed it elsewhere)."""
    folders = []
    for root in HEROIC_CONFIG_ROOTS:
        config = root / "heroic" / "config.json"
        try:
            raw = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        value = ((raw or {}).get("defaultSettings") or {}).get("winePrefix")
        if value:
            base = Path(os.path.expanduser(str(value)))
            folders += [base, base.parent]
        # where each game's prefix was put (a library on another drive):
        # its config stays after an uninstall, so a left prefix is found
        for game_config in (root / "heroic" / "GamesConfig").glob("*.json"):
            try:
                data = json.loads(game_config.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for entry in (data.values() if isinstance(data, dict) else ()):
                if isinstance(entry, dict) and entry.get("winePrefix"):
                    folders.append(Path(os.path.expanduser(str(entry["winePrefix"]))).parent)
    folders.append(Path.home() / "Games/Heroic/Prefixes/default")
    return folders


def lutris_prefix_folders() -> List[Path]:
    """Lutris' game folder (system.yml's game_path, ~/Games by default):
    a Wine game's prefix goes into <game_path>/<slug>."""
    folders = []
    for base in (*LUTRIS_CONFIG_DIR_CANDIDATES, *LUTRIS_DATA_DIR_CANDIDATES):
        try:
            text = (base / "system.yml").read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"^\s*game_path:\s*['\"]?([^'\"\n]+)", text, re.M)
        if m:
            folders.append(Path(os.path.expanduser(m.group(1).strip())))
    for base in (*LUTRIS_CONFIG_DIR_CANDIDATES, *LUTRIS_DATA_DIR_CANDIDATES):
        for game_config in (base / "games").glob("*.yml"):
            try:
                text = game_config.read_text(encoding="utf-8")
            except OSError:
                continue
            for m in re.finditer(r"^\s*prefix:\s*['\"]?([^'\"\n]+)", text, re.M):
                folders.append(Path(os.path.expanduser(m.group(1).strip())).parent)
    folders.append(Path.home() / "Games")
    return folders


def faugus_prefix_folders() -> List[Path]:
    """Faugus' default-prefix (config.ini), ~/Faugus by default."""
    folders = []
    games = faugus_games_path()
    if games is not None:
        try:
            text = (games.parent / "config.ini").read_text(encoding="utf-8")
            m = re.search(r"^\s*default-prefix\s*=\s*['\"]?([^'\"\n]+)", text, re.M)
            if m:
                folders.append(Path(os.path.expanduser(m.group(1).strip())))
        except OSError:
            pass
        try:
            for entry in json.loads(games.read_text(encoding="utf-8")) or []:
                if isinstance(entry, dict) and entry.get("prefix"):
                    folders.append(Path(os.path.expanduser(str(entry["prefix"]))).parent)
        except (OSError, ValueError, TypeError):
            pass
    folders.append(Path.home() / "Faugus")
    return folders


def lutris_known() -> Tuple[Set[str], Set[Path]]:
    """What Lutris has ever had as its own: every game in its database
    (installed or not - a game uninstalled stays listed) by its slug (the
    folder Lutris installs into is <game_path>/<slug>) and its folder,
    and every prefix its game configs name. ~/Games is everyone's: a
    prefix there that Lutris never knew is the person's own, not a
    Lutris leftover."""
    from plunderhub.core.scan_paths import lutris_db_path
    names: Set[str] = set()
    paths: Set[Path] = set()
    db = lutris_db_path()
    if db is not None:
        from plunderhub.core.scanner import _query_lutris_db

        def query(connection):
            columns = {row[1] for row in connection.execute("PRAGMA table_info(games)")}
            wanted = [c if c in columns else "NULL" for c in ("slug", "directory")]
            return connection.execute(f"SELECT {', '.join(wanted)} FROM games").fetchall()
        try:
            for slug, directory in _query_lutris_db(db, query):
                if slug:
                    names.add(str(slug).lower())
                if directory:
                    paths.add(_real(Path(os.path.expanduser(str(directory)))))
        except Exception as e:
            logger.debug(f"Bilge: Lutris' database: {e}")
    for base in (*LUTRIS_CONFIG_DIR_CANDIDATES, *LUTRIS_DATA_DIR_CANDIDATES):
        for game_config in (base / "games").glob("*.yml"):
            try:
                text = game_config.read_text(encoding="utf-8")
            except OSError:
                continue
            for m in re.finditer(r"^\s*prefix:\s*['\"]?([^'\"\n]+)", text, re.M):
                paths.add(_real(Path(os.path.expanduser(m.group(1).strip()))))
    return names, paths


# a launcher's prefix folder that others use too: only what it knew is its leftover
PREFIX_KNOWN: Dict[str, Callable[[], Tuple[Set[str], Set[Path]]]] = {"Lutris": lutris_known}

PREFIX_FOLDERS: Dict[str, Callable[[], List[Path]]] = {
    "Heroic": heroic_prefix_folders, "Lutris": lutris_prefix_folders, "Faugus": faugus_prefix_folders}


def _shortcut_texts() -> str:
    """What starts games outside the launchers, as text: Steam's
    non-Steam games (userdata/*/config/shortcuts.vdf - the program's
    path, its folder, WINEPREFIX=... in its launch options) and the
    person's own .desktop starters. A prefix named in them is in use.
    Not Wine's own menu entries (applications/wine/): those stay behind
    when a launcher removes a game."""
    files: List[Path] = []
    for root in steam_roots():
        files.extend((root / "userdata").glob("*/config/shortcuts.vdf"))
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    files.extend((data / "applications").glob("*.desktop"))
    files.extend((Path.home() / "Desktop").glob("*.desktop"))
    texts = []
    for f in files:
        try:
            texts.append(f.read_bytes().decode("utf-8", "replace"))
        except OSError:
            continue
    return "\n".join(texts)


def _named_in(text: str, path: Path) -> bool:
    """path (as written or its real one) is in text, whole: followed by a
    /, a quote, a space or the end - ~/Games/x, not ~/Games/x2."""
    for p in {str(path), str(_real(path))}:
        if re.search(re.escape(p) + r"""(?=[/"'\s\x00;]|$)""", text):
            return True
    return False


def prefix_leftovers(found: Found, launcher: str, kept: Iterable[Path], shortcuts: str = "") -> None:
    """Prefixes in the launcher's prefix folders that aren't in `kept`
    (every installed game's prefix and folder, of every launcher), nor
    hold one of them, nor sit inside one."""
    kept_real = {_real(p) for p in kept if p}
    known = PREFIX_KNOWN[launcher]() if launcher in PREFIX_KNOWN else None
    seen: Set[Path] = set()
    home = _real(Path.home())
    for base in PREFIX_FOLDERS[launcher]():
        base_real = _real(base)
        # never the home folder or a drive's top: ~/.wine and the like are
        # a person's own Wine, not a launcher's leftover
        if base_real == home or len(base_real.parts) < 3 or base_real in home.parents:
            continue
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for path in entries:
            real = _real(path)
            if real in seen or real == base_real or path.is_symlink() or not path.is_dir():
                continue
            seen.add(real)
            if path.name.startswith(".") or path.name.lower() in ("umu", "heroic", "prefixes", "default", "shared") \
                    or not is_prefix(path):
                continue
            if any(k == real or real in k.parents or k in real.parents for k in kept_real):
                continue
            if known is not None and path.name.lower() not in known[0] and real not in known[1]:
                logger.debug(f"Bilge: {path} was never {launcher}'s, not looked at")
                continue
            if shortcuts and _named_in(shortcuts, path):
                logger.debug(f"Bilge: {path} is named by a shortcut, kept")
                continue
            found.items.append(Item("prefix", launcher, path, title=path.name))


# --- Proton builds -----------------------------------------------------------------------

def _launcher_setting_files() -> List[Path]:
    """Every file where a launcher keeps which Proton a game, or the
    launcher by default, runs on - read as text: a build named in any of
    them is in use."""
    files: List[Path] = []
    for root in steam_roots():
        files.append(root / "config" / "config.vdf")
    for root in HEROIC_CONFIG_ROOTS:
        files.append(root / "heroic" / "config.json")
        files.extend((root / "heroic" / "GamesConfig").glob("*.json"))
    for base in (*LUTRIS_CONFIG_DIR_CANDIDATES, *LUTRIS_DATA_DIR_CANDIDATES):
        files += [base / "system.yml", base / "runners" / "wine.yml"]
        files.extend((base / "games").glob("*.yml"))
    games = faugus_games_path()
    if games is not None:
        files += [games, games.parent / "config.ini"]
    return files


def proton_leftovers(found: Found, used_names: Iterable[str]) -> None:
    """PlunderHub's own builds that nothing runs on. `used_names`: what
    the games of the library run on (proton_page), on top of every
    launcher's settings files."""
    from plunderhub.core import proton_store
    used = set(used_names)
    texts = []
    for f in _launcher_setting_files():
        try:
            texts.append(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    blob = "\n".join(texts)
    for build in proton_store.installed():
        if build.latest or build.name in used or build.name in blob:
            continue
        found.items.append(Item("proton", "PlunderHub", build.path, title=build.name))


# --- measuring, saves, names -------------------------------------------------------------

def measure(item: Item) -> None:
    """The size on disk and the newest file's time, without following links."""
    total, newest = 0, 0.0
    stack = [str(item.path)]
    while stack:
        top = stack.pop()
        try:
            with os.scandir(top) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += getattr(st, "st_blocks", 0) * 512 or st.st_size
                    newest = max(newest, st.st_mtime)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
        except OSError:
            continue
    item.size, item.changed = total, newest or _mtime(item.path)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _size_of(path: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
                total += getattr(st, "st_blocks", 0) * 512 or st.st_size
            except OSError:
                pass
    return total


def _size_of_file(path: Path) -> int:
    try:
        st = path.lstat()
        return getattr(st, "st_blocks", 0) * 512 or st.st_size
    except OSError:
        return 0


def player_files(prefix: Path) -> Tuple[List[Tuple[Path, str]], List[Tuple[str, int]]]:
    """What in a Wine prefix is the player's - when in doubt, it is:
      - every user folder (drive_c/users/*, Public too), but for the
        shader / crash caches Wine and the driver make again
      - drive_c/ProgramData, all of it
      - the registry (user.reg, system.reg, userdef.reg)
      - in drive_c/windows: whatever isn't Wine's own (old games write
        .ini files there), and win.ini / system.ini
      - in Program Files and every other drive_c folder (a game installed
        into the prefix, a launcher like Ubisoft Connect): what looks
        like a save by name, and every file changed after the rest of its
        program's folder - written by the game after it was installed
    Links are never followed (a Wine prefix links Documents to the home
    folder: what's there isn't in the prefix, deleting it doesn't touch it).
    ([(file, its path in the prefix)], [(drive_c folder left out, bytes)])"""
    drive = _drive(prefix)
    found: List[Tuple[Path, str]] = []
    left: Dict[str, int] = {}

    def add(path: Path):
        found.append((path, path.relative_to(prefix).as_posix()))

    for name in ("user.reg", "system.reg", "userdef.reg"):
        reg = drive.parent / name
        if reg.is_file() and not reg.is_symlink():
            add(reg)
    try:
        tops = sorted(drive.iterdir())
    except OSError:
        tops = []
    for top in tops:
        if top.is_symlink():
            continue
        low = top.name.lower()
        if top.is_file():
            add(top)                                    # C:\hegames.ini and the like
        elif low in PROTON_OWN:
            continue
        elif low == "users":
            _walk_all(top, add, skip=lambda parts, d: d.lower() in _CACHE_DIRS)
        elif low == "programdata":
            _walk_all(top, add)
        elif low == "windows":
            _windows(top, add, left)
        else:
            _programs(top, add, left)
    return found, sorted(left.items(), key=lambda kv: -kv[1])


def _walk_all(top: Path, add, skip=None) -> None:
    for dirpath, dirnames, filenames in os.walk(top):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()
                       and not (skip and skip(here.relative_to(top).parts, d))]
        for f in filenames:
            path = here / f
            if not path.is_symlink() and path.is_file():
                add(path)


def _windows(top: Path, add, left: Dict[str, int]) -> None:
    try:
        entries = sorted(top.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        low = entry.name.lower()
        if low in ("win.ini", "system.ini"):
            add(entry)
        elif low in WINE_WINDOWS:
            left[top.name] = left.get(top.name, 0) + (_size_of(entry) if entry.is_dir() else _size_of_file(entry))
        elif entry.is_dir():
            _walk_all(entry, add)                       # windows/UbiSoft/ubi.ini
        elif entry.is_file():
            add(entry)                                  # windows/hegames.ini


def _programs(top: Path, add, left: Dict[str, int]) -> None:
    """A folder of installed programs: per program (its first two levels,
    "Program Files/<maker>" or "GOG Games/<game>"), the files changed
    later than nine in ten of its files were - written after the
    install - and the ones named like saves."""
    groups: Dict[str, List[Tuple[Path, float, int, bool]]] = {}
    for dirpath, dirnames, filenames in os.walk(top):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
        rel = here.relative_to(top).parts
        named = any(_SAVE_DIR.match(p) for p in rel)
        key = rel[0] if rel else ""
        for f in filenames:
            path = here / f
            if path.is_symlink() or not path.is_file():
                continue
            try:
                st = path.lstat()
            except OSError:
                continue
            groups.setdefault(key, []).append((path, st.st_mtime, getattr(st, "st_blocks", 0) * 512 or st.st_size,
                                               named or bool(_SAVE_FILE.search(f))))
    for key, files in groups.items():
        times = sorted(t for _p, t, _s, _n in files)
        installed = times[min(len(times) - 1, int(len(times) * 0.9))]
        for path, mtime, size, named in files:
            if named or mtime > installed + CHANGED_AFTER:
                add(path)
            else:
                left[top.name] = left.get(top.name, 0) + size


def look_for_saves(item: Item) -> None:
    """Fills in the item's player files (a prefix only)."""
    if item.kind not in ("steam_prefix", "prefix"):
        return
    files, left = player_files(item.path)
    # every prefix has the registry and win.ini: player files are what's beyond those
    real = [f for f in files if f[1].rsplit("/", 1)[-1].lower() not in SYSTEM_FILES]
    item.player, item.left_out = (files if real else []), left
    item.saves = len(real)
    item.saves_newest = max((_mtime(p) for p, _r in real), default=0.0)


def steam_name(appid: str, known: Dict[str, str]) -> str:
    """The game's name: as PlunderHub last saw it, else Steam's store
    (one small request), else ''."""
    if appid in known:
        return known[appid]
    from plunderhub.core.utils import HEADERS, http_get_json
    data = http_get_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=basic",
                         HEADERS, timeout=8, retries=1)
    try:
        return str(data[appid]["data"]["name"]) if data and data[appid].get("success") else ""
    except (KeyError, TypeError, AttributeError):
        return ""


def scan(kept_prefixes: Iterable[Path], healthy: Set[str], used_protons: Iterable[str]) -> Found:
    """Everything there is to clean. healthy: the launchers whose games
    were read without trouble - only theirs are looked at for prefixes."""
    found = Found()
    try:
        steam_leftovers(found)
    except Exception as e:
        logger.warning(f"Bilge: Steam leftovers: {e}")
        found.skipped.append(("Steam", "unreadable"))
    kept = list(kept_prefixes)
    try:
        shortcuts = _shortcut_texts()
    except Exception as e:
        logger.warning(f"Bilge: shortcuts: {e}")
        shortcuts = ""
    for launcher in PREFIX_FOLDERS:
        if launcher not in healthy:
            found.skipped.append((launcher, "unreadable"))
            continue
        try:
            prefix_leftovers(found, launcher, kept, shortcuts)
        except Exception as e:
            logger.warning(f"Bilge: {launcher} leftovers: {e}")
    try:
        proton_leftovers(found, used_protons)
    except Exception as e:
        logger.warning(f"Bilge: unused Protons: {e}")
    return found


# --- deleting ----------------------------------------------------------------------------

def delete(path: Path, to_trash: bool) -> Optional[str]:
    """None when it's gone; else why not - "trash" when the Trash can't
    take it (a drive without a Trash folder: then only deleting for good
    is left), or the error."""
    if to_trash:
        from plunderhub.gi_setup import Gio, GLib
        try:
            Gio.File.new_for_path(str(path)).trash(None)
            return None
        except GLib.Error as e:
            logger.warning(f"Bilge: {path} to the Trash: {e.message}")
            return "trash"
    try:
        _rmtree(path)
        return None
    except OSError as e:
        logger.warning(f"Bilge: deleting {path}: {e}")
        return str(e)


def _rmtree(path: Path) -> None:
    """shutil.rmtree that gets past a folder made read-only (some Windows
    installers do it to theirs, Wine turns that into no write bit): the
    folder is made writable for its owner and the step tried again."""
    import stat
    import sys

    def again(func, failed, _err):
        parent = os.path.dirname(failed)
        for folder in (parent, failed) if func is not os.rmdir else (parent,):
            try:
                st = os.lstat(folder)
                if stat.S_ISDIR(st.st_mode):
                    os.chmod(folder, st.st_mode | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRUSR)
            except OSError:
                pass
        func(failed)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=again)
    else:
        shutil.rmtree(path, onerror=lambda func, failed, info: again(func, failed, info[1]))


def now() -> float:
    return time.time()


# --- player files: backing up and restoring ----------------------------------------------
#
# A backup is a folder in backups_root()/bilge/<title>-<time>/:
#   backup.json  what it is: the prefix it came from, where drive_c sat in
#                it, the Windows user folder's name, every file with its
#                path in the prefix, size and sha256
#   files/       the files, by their path in the prefix
# Made in "<name>.partial" and renamed once every copy was read back and
# matched - a backup that's listed is a whole one.

BACKUP_FORMAT = 2


@dataclass
class Backup:
    path: Path
    title: str
    kind: str
    launcher: str
    source: Path                # the prefix it came from
    user: str                   # its Windows user folder's name ("" when it had none)
    created: float
    files: List[Dict] = field(default_factory=list)   # {"rel" (in the prefix), "size", "sha256"}
    drive: str = "drive_c"      # where drive_c sat in the prefix ("pfx/drive_c" in a Proton one)
    pfx_link: bool = False      # the prefix had pfx -> . (umu): made again so
    proton: bool = False        # a Proton prefix (its "version" file, or Steam's / pfx layout)
    # a restore's undo: what a restore of `parent` (a backup's folder name)
    # set aside, and where each file was ({rel: path}) - not a backup of its own
    parent: str = ""
    targets: Dict[str, str] = field(default_factory=dict)
    undos: List["Backup"] = field(default_factory=list)   # this one's, newest first

    @property
    def size(self) -> int:
        return sum(int(f.get("size", 0)) for f in self.files)


@dataclass
class RestorePlan:
    backup: Backup
    drive: Path                 # the drive_c it goes into
    user: str                   # the user folder there the backup's user's files go to
    folder_exists: bool         # the prefix is there (still, or again)
    source: Path = Path()       # where the prefix is now (restore_source)
    targets: Dict[str, Path] = field(default_factory=dict)
    new: List[str] = field(default_factory=list)       # rels not there yet
    replace: List[str] = field(default_factory=list)   # there, but different: set aside first
    same: List[str] = field(default_factory=list)      # there already, byte for byte
    kept: List[str] = field(default_factory=list)      # system files not put over a prefix that's there
    registry: Dict[str, List[str]] = field(default_factory=dict)   # rel -> the games' keys merged in
    # a Proton prefix not made yet: Proton makes it at the game's first
    # start and marks it as up to date - an old registry put in before
    # would never be updated by Wine. The games' keys are merged in by a
    # restore after that start. rel -> how many games' keys wait
    later: Dict[str, int] = field(default_factory=dict)


def backups_folder() -> Path:
    from plunderhub.core.save_backup import backups_root
    return backups_root() / "bilge"


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pfx_is_self(prefix: Path) -> bool:
    """umu / Proton-GE outside Steam (Heroic, Lutris, Faugus) make the
    prefix itself the Wine prefix, with "pfx" a link back to it (pfx -> .)."""
    pfx = prefix / "pfx"
    try:
        return pfx.is_symlink() and pfx.resolve() == prefix.resolve()
    except OSError:
        return False


def _drive(prefix: Path) -> Path:
    """The prefix's drive_c, by its real path - never through the pfx -> . link."""
    if _pfx_is_self(prefix):
        return prefix / "drive_c"
    return prefix / "pfx" / "drive_c" if (prefix / "pfx" / "drive_c").is_dir() else prefix / "drive_c"


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "game"


def _main_user(drive: Path) -> str:
    """The prefix's own user folder: steamuser (Proton) or the login name
    (Wine) - the one that isn't Public; "" when it isn't one clear one."""
    try:
        users = [u.name for u in (drive / "users").iterdir() if u.is_dir() and not u.is_symlink()
                 and u.name.lower() != "public"]
    except OSError:
        return ""
    return users[0] if len(users) == 1 else ("steamuser" if "steamuser" in users else "")


def _free_name(path: Path) -> Path:
    """path, or path-2, path-3... - two backups in the same second don't meet."""
    n, found = 2, path
    while found.exists() or found.with_name(found.name + ".partial").exists():
        found, n = path.with_name(f"{path.name}-{n}"), n + 1
    return found


class NoSpace(OSError):
    """Not room enough for the copies: (bytes needed, bytes free, where)."""
    def __init__(self, need: int, free: int, where: Path):
        super().__init__(f"{need} bytes needed, {free} free at {where}")
        self.need, self.free, self.where = need, free, where


class NotThere(OSError):
    """The folder a gone prefix would be made again in isn't there (its
    drive not plugged in?): nothing is written onto an empty mount point."""
    def __init__(self, where: Path):
        super().__init__(f"{where} isn't there")
        self.where = where


# room left over after the copies: the disk isn't filled to the brim
SPACE_MARGIN = 256 * 1024 * 1024


def _check_space(where: Path, need: int) -> None:
    """Raises NoSpace when the drive of `where` (its first folder that's
    there) hasn't `need` bytes free and the margin."""
    probe = where
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if free < need + SPACE_MARGIN:
        raise NoSpace(need, free, probe)


def restore_source(backup: "Backup") -> Path:
    """Where the backup's prefix is now. A Steam game installed again into
    another library has its compatdata there (Steam makes it in the
    game's library): that one, not the one it was backed up from."""
    appid = backup.source.name
    if backup.kind == "steam_prefix" and appid.isdigit():
        for root in steam_roots():
            for lib in [root, *steam_library_paths(root)]:
                if (lib / "steamapps" / f"appmanifest_{appid}.acf").is_file():
                    return lib / "steamapps" / "compatdata" / appid
    return backup.source


def _is_proton(item: Item) -> bool:
    """Proton (Steam's, or umu / Proton-GE outside Steam) writes a
    "version" file beside its pfx; Steam's prefixes are always Proton's."""
    return item.kind == "steam_prefix" or (item.path / "version").is_file() or _pfx_is_self(item.path) \
        or (item.path / "pfx" / "drive_c").is_dir()


def back_up(item: Item, title: str) -> Backup:
    """Copies the prefix's player files (player_files, read afresh) and
    checks every copy against its original. Raises on any trouble; then
    nothing is kept."""
    files_now, _left = player_files(item.path)
    if not files_now:
        raise ValueError("no player files")
    drive = _drive(item.path)
    _check_space(backups_folder(), sum(_size_of_file(src) for src, _rel in files_now))
    final = _free_name(backups_folder() / f"{_safe_name(title)}-{time.strftime('%Y%m%d-%H%M%S')}")
    work = final.with_name(final.name + ".partial")
    shutil.rmtree(work, ignore_errors=True)
    files: List[Dict] = []
    try:
        for src, rel in files_now:
            dst = work / "files" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            digest = _sha256(src)
            if _sha256(dst) != digest:
                raise IOError(f"the copy of {src} doesn't match it")
            files.append({"rel": rel, "size": dst.stat().st_size, "sha256": digest})
        meta = {"format": BACKUP_FORMAT, "title": title, "kind": item.kind, "launcher": item.launcher,
                "source": str(item.path), "drive": drive.relative_to(item.path).as_posix(),
                "user": _main_user(drive), "created": time.time(), "files": files,
                "pfx_link": _pfx_is_self(item.path), "proton": _is_proton(item)}
        (work / "backup.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
        work.rename(final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return _read_backup(final)


def _inside(rel: str) -> bool:
    parts = Path(rel).parts
    return bool(parts) and not Path(rel).is_absolute() and ".." not in parts


def _read_backup(path: Path) -> Optional[Backup]:
    try:
        meta = json.loads((path / "backup.json").read_text(encoding="utf-8"))
        drive = str(meta.get("drive") or "drive_c")
        user = str(meta.get("user") or "")
        files = [f for f in meta.get("files", []) if isinstance(f, dict) and f.get("rel")]
        # a path inside the prefix only: a backup.json changed by hand can't
        # send a file anywhere else (../, an absolute path)
        if any(not _inside(str(f["rel"])) for f in files) or not _inside(drive):
            raise ValueError("a file's path leads out of the prefix")
        if int(meta.get("format", 1)) < 2:
            # the first ones kept paths under drive_c/users/<user>/
            files = [{**f, "rel": f"{drive}/users/{user}/{f['rel']}"} for f in files]
            for f in files:
                old = path / "files" / f["rel"][len(f"{drive}/users/{user}/"):]
                if old.exists() and not (path / "files" / f["rel"]).exists():
                    (path / "files" / f["rel"]).parent.mkdir(parents=True, exist_ok=True)
                    old.rename(path / "files" / f["rel"])
        return Backup(path, str(meta["title"]), str(meta.get("kind", "")), str(meta.get("launcher", "")),
                      Path(meta["source"]), user, float(meta.get("created", 0)), files, drive,
                      bool(meta.get("pfx_link")),
                      bool(meta["proton"]) if "proton" in meta else (
                          meta.get("kind") == "steam_prefix" or bool(meta.get("pfx_link")) or drive.startswith("pfx/")),
                      str(meta.get("parent") or (path.name.split("-before-restore-")[0]
                                                 if "-before-restore-" in path.name else "")),
                      {str(k): str(v) for k, v in (meta.get("targets") or {}).items()})
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.debug(f"Bilge: {path} isn't a backup: {e}")
        return None


def backups() -> List[Backup]:
    """Every whole backup, newest first."""
    try:
        folders = [p for p in backups_folder().iterdir() if p.is_dir() and not p.name.endswith(".partial")]
    except OSError:
        return []
    found = sorted((b for b in (_read_backup(p) for p in folders) if b is not None), key=lambda b: -b.created)
    top = {b.path.name: b for b in found if not b.parent}
    for b in found:
        if b.parent and b.parent in top:
            top[b.parent].undos.append(b)
    # an undo whose backup was deleted by hand: shown as a backup, so nothing hides
    return [b for b in found if not b.parent or b.parent not in top]


def plan_restore(backup: Backup) -> RestorePlan:
    """Where each file would go, and which are new, different or the same
    there. Into the prefix as it is now (its drive_c where that is now,
    its own user folder for the backup's user's files - Proton calls it
    steamuser, Wine by the login name); a gone prefix is made again as it
    was. The registry goes back only into a prefix made again: over one
    that's there it'd undo that one's own settings - it stays in the
    backup (plan.kept)."""
    source = restore_source(backup)
    exists = is_prefix(source)
    if not exists and not source.parent.is_dir():
        raise NotThere(source.parent)
    drive = _drive(source) if exists else source / backup.drive
    user = (_main_user(drive) or backup.user) if exists else backup.user
    plan = RestorePlan(backup, drive, user, exists, source)
    for f in backup.files:
        rel = f["rel"]
        system = rel.rsplit("/", 1)[-1].lower() in SYSTEM_FILES and (
            not rel.startswith(backup.drive + "/") or rel.startswith(backup.drive + "/windows/"))
        is_reg = rel.endswith(".reg")
        if system and is_reg and backup.proton and (not exists or not (drive.parent / rel.rsplit("/", 1)[-1]).is_file()):
            plan.later[rel] = len(game_keys(backup.path / "files" / rel))
            continue
        if system and exists:
            target = (drive.parent / rel.rsplit("/", 1)[-1]) if is_reg else None
            keys = _game_keys_to_merge(backup.path / "files" / rel, target) if target is not None else []
            if keys:
                plan.registry[rel] = keys
                plan.targets[rel] = target
            else:
                plan.kept.append(rel)
            continue
        if rel.startswith(backup.drive + "/"):
            sub = rel[len(backup.drive) + 1:]
            head = f"users/{backup.user}/"
            if backup.user and sub.startswith(head) and user:
                sub = f"users/{user}/" + sub[len(head):]
            target = drive / sub
        else:
            # beside drive_c: user.reg
            parent = backup.drive.rsplit("/", 1)[0] if "/" in backup.drive else ""
            target = drive.parent / (rel[len(parent) + 1:] if parent else rel)
        plan.targets[rel] = target
        if not target.exists() and not target.is_symlink():
            plan.new.append(rel)
        elif target.is_file() and target.stat().st_size == int(f["size"]) and _sha256(target) == f["sha256"]:
            plan.same.append(rel)
        else:
            plan.replace.append(rel)
    return plan


def restore(plan: RestorePlan) -> Tuple[int, Optional[Path]]:
    """Puts the backup's files back as the plan says: a file there that's
    different is moved aside first (into "<backup>-before-restore-<time>",
    a backup of its own), the same ones are left, every copy is read
    back. (restored, the set-aside folder or None). Raises on trouble -
    what was set aside by then stays set aside."""
    backup = plan.backup
    for f in backup.files:
        if f["rel"] in plan.targets and _sha256(backup.path / "files" / f["rel"]) != f["sha256"]:
            raise IOError(f"the backup's {f['rel']} has changed since it was made")
    going = set(plan.new) | set(plan.replace)
    _check_space(plan.drive, sum(int(f.get("size", 0)) for f in backup.files if f["rel"] in going))
    aside = None
    aside_files: List[Dict] = []
    if plan.replace:
        aside = _free_name(backups_folder() / f"{backup.path.name}-before-restore-{time.strftime('%Y%m%d-%H%M%S')}")
    sha = {f["rel"]: f["sha256"] for f in backup.files}
    done = 0
    try:
        for rel in plan.replace + plan.new:
            target = plan.targets[rel]
            target.parent.mkdir(parents=True, exist_ok=True)
            if rel in plan.replace and (target.exists() or target.is_symlink()):
                keep = aside / "files" / rel
                keep.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(keep))
                if keep.is_file():
                    aside_files.append({"rel": rel, "size": keep.stat().st_size, "sha256": _sha256(keep)})
            shutil.copy2(backup.path / "files" / rel, target)
            if _sha256(target) != sha[rel]:
                raise IOError(f"{target} doesn't match the backup after copying")
            done += 1
        for rel, keys in plan.registry.items():
            target = plan.targets[rel]
            if aside is None:
                aside = _free_name(backups_folder() /
                                   f"{backup.path.name}-before-restore-{time.strftime('%Y%m%d-%H%M%S')}")
            keep = aside / "files" / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, keep)          # the whole registry as it was: undoable
            aside_files.append({"rel": rel, "size": keep.stat().st_size, "sha256": _sha256(keep)})
            merged = merge_registry(target.read_text(encoding="utf-8", errors="surrogateescape"),
                                    (backup.path / "files" / rel).read_text(encoding="utf-8",
                                                                            errors="surrogateescape"), keys)
            tmp = target.with_name(target.name + ".plunderhub-tmp")
            tmp.write_text(merged, encoding="utf-8", errors="surrogateescape")
            tmp.replace(target)
            now_keys = {k for k, _h, _b in _reg_sections(target.read_text(encoding="utf-8",
                                                                             errors="surrogateescape"))[1]}
            if not set(keys) <= now_keys:
                raise IOError(f"{target}: the games' keys aren't there after merging")
            done += len(keys)
        if backup.pfx_link and not plan.folder_exists:
            pfx = plan.source / "pfx"
            if not pfx.exists() and not pfx.is_symlink():
                pfx.symlink_to(".")         # as umu made it: pfx -> .
    finally:
        if aside is not None and aside.exists():
            meta = {"format": BACKUP_FORMAT, "title": backup.title + " (before restore)", "kind": backup.kind,
                    "launcher": backup.launcher, "source": str(plan.source), "drive": backup.drive,
                    "user": backup.user, "created": time.time(), "files": aside_files,
                    "pfx_link": backup.pfx_link, "proton": backup.proton,
                    "parent": backup.path.name,
                    "targets": {f["rel"]: str(plan.targets[f["rel"]]) for f in aside_files}}
            (aside / "backup.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    return done, aside


def waits(plan: RestorePlan) -> bool:
    """Some of the game's settings (its registry keys) go back only after
    its first start (plan.later)."""
    return any(plan.later.values())


def finish_waiting(backup: Backup) -> Optional[Tuple[Optional[Path], Dict[str, Path]]]:
    """The second half of a restore into a Proton prefix that wasn't made
    yet, done by itself once the game has started there: only the games'
    registry keys are merged in (files the game wrote since stay as they
    are). None while it still waits (no registry yet, or the game runs);
    else (the set-aside folder or None, {rel: the registry file})."""
    plan = plan_restore(backup)
    if waits(plan) or prefix_in_use(plan.source):
        return None
    plan.new, plan.replace, plan.same = [], [], []
    if not plan.registry:
        return None, {}
    aside = restore(plan)[1]
    return aside, {rel: plan.targets[rel] for rel in plan.registry}


def undo_restore(undo: Backup) -> int:
    """Puts back what a restore set aside (undo: one of Backup.undos),
    each file where it was, read back - then the undo is used up. Files
    the restore added are left (they're the backup's). How many went back."""
    if not undo.targets:
        # made before undos kept where each file was: where a restore would put it
        plan = plan_restore(undo)
        undo.targets = {rel: str(t) for rel, t in plan.targets.items()}
        for rel in [*plan.kept, *plan.later]:
            undo.targets[rel] = str(plan.drive.parent / rel.rsplit("/", 1)[-1])
        if any(f["rel"] not in undo.targets for f in undo.files):
            raise ValueError("no record of where the files were")
    # every file back into the prefix it came from, nowhere else
    homes = {str(undo.source.resolve()), str(restore_source(undo).resolve())}
    for rel, where in undo.targets.items():
        real = str(Path(where).resolve())
        if not any(real == h or real.startswith(h + os.sep) for h in homes):
            raise ValueError(f"{where} isn't in the game's folder")
    first = Path(next(iter(undo.targets.values())))
    for f in undo.files:
        if _sha256(undo.path / "files" / f["rel"]) != f["sha256"]:
            raise IOError(f"the set-aside {f['rel']} has changed")
    done = 0
    for f in undo.files:
        target = Path(undo.targets[f["rel"]])
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".plunderhub-tmp")
        shutil.copy2(undo.path / "files" / f["rel"], tmp)
        if _sha256(tmp) != f["sha256"]:
            tmp.unlink()
            raise IOError(f"{target} doesn't match after copying")
        tmp.replace(target)
        done += 1
    logger.info(f"Bilge: undid a restore into {first.parent}: {done} files back")
    _rmtree(undo.path)
    return done


# --- the registry: a game's own keys -------------------------------------------------------
#
# Wine keeps the registry as text: a header, then sections
#   [Software\\Hinterland\\TheLongDark] 1792860681
#   #time=...
#   "name"=dword:00000003        (a value may go on over lines ending in \)
# separated by blank lines. A game's keys are the ones under
# Software\<maker> (also Software\Wow6432Node\<maker>) where the maker
# isn't Windows', Wine's or a driver's / launcher's own.

_SYSTEM_MAKERS = {"microsoft", "wine", "classes", "policies", "valve", "khronos", "clients", "odbc", "intel",
                  "nvidia corporation", "amd", "ati technologies", "mozilla", "mozillaplugins", "google",
                  "registeredapplications", "registered applications", "wow6432node", "oem", "realtek",
                  "codeweavers", "steam", "gstreamer", "freedesktop.org", "python", "mono", "netscape",
                  "macromedia", "adobe", "javasoft", "nvidia", "logitech", "wintab", "openal", "dosbox"}


def _reg_sections(text: str) -> Tuple[str, List[Tuple[str, str, str]]]:
    """(the header, [(key lower-cased, its [..] line, its lines below)])."""
    header, sections = [], []
    current = None
    for line in text.splitlines(keepends=True):
        if line.startswith("["):
            end = line.rfind("]")
            key = line[1:end] if end > 0 else line[1:].strip()
            current = [key.lower(), line, []]
            sections.append(current)
        elif current is None:
            header.append(line)
        else:
            current[2].append(line)
    return "".join(header), [(k, h, "".join(b)) for k, h, b in sections]


def _is_game_key(key: str) -> bool:
    parts = [p for p in key.split("\\\\") if p]
    if len(parts) < 2 or parts[0] != "software":
        return False
    maker = parts[2] if parts[1] == "wow6432node" and len(parts) > 2 else parts[1]
    return maker not in _SYSTEM_MAKERS


def game_keys(reg: Path) -> List[str]:
    """The games' own keys in a registry file."""
    try:
        return [k for k, _h, _b in _reg_sections(reg.read_text(encoding="utf-8", errors="surrogateescape"))[1]
                if _is_game_key(k)]
    except OSError:
        return []


def _game_keys_to_merge(backup_reg: Path, current_reg: Path) -> List[str]:
    """The games' keys of the backup's registry that the prefix's own
    registry doesn't have, or has otherwise (a game reinstalled and
    started once writes its defaults)."""
    try:
        old = _reg_sections(backup_reg.read_text(encoding="utf-8", errors="surrogateescape"))[1]
        now = {k: b for k, _h, b in _reg_sections(current_reg.read_text(encoding="utf-8",
                                                                         errors="surrogateescape"))[1]}
    except OSError:
        return []
    return [k for k, _h, b in old if _is_game_key(k) and (k not in now or _values(now[k]) != _values(b))]


def _values(body: str) -> str:
    """A section's values, without Wine's #time= stamp."""
    return "".join(line for line in body.splitlines(keepends=True) if not line.startswith("#time="))


def merge_registry(current: str, backup: str, keys: List[str]) -> str:
    """The prefix's registry with the given keys as the backup has them:
    replaced where it has them, added at the end where it doesn't."""
    header, now = _reg_sections(current)
    old = {k: (h, b) for k, h, b in _reg_sections(backup)[1]}
    wanted = set(keys)
    out, done = [header], set()
    for k, h, b in now:
        if k in wanted and k in old:
            out.append(old[k][0] + old[k][1])
            done.add(k)
        else:
            out.append(h + b)
    for k in keys:
        if k not in done and k in old:
            text = old[k][0] + old[k][1]
            if out and not out[-1].endswith("\n\n"):
                out.append("\n")
            out.append(text)
    return "".join(out)


def prefix_in_use(prefix: Path) -> bool:
    """A program of this person's runs in the prefix (its WINEPREFIX is it,
    or its STEAM_COMPAT_DATA_PATH for Proton): Wine would write the
    registry back over a restore when it quits."""
    try:
        want = {str(prefix.resolve()), str(_drive(prefix).parent.resolve())}
    except OSError:
        want = {str(prefix)}
    uid = os.getuid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/environ", "rb") as f:
                env = f.read().split(b"\0")
        except OSError:
            continue
        for item in env:
            if item.startswith((b"WINEPREFIX=", b"STEAM_COMPAT_DATA_PATH=")):
                value = item.split(b"=", 1)[1].decode("utf-8", "replace").rstrip("/")
                try:
                    value = str(Path(value).resolve())
                except OSError:
                    pass
                if value in want:
                    return True
    return False


def delete_backup(backup: Backup) -> None:
    """The backup and its restores' undos."""
    for undo in backup.undos:
        _rmtree(undo.path)
    _rmtree(backup.path)
