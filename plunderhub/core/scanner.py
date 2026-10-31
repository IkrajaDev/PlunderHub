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


import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from plunderhub.core.models import GameItem
from plunderhub.core.persistence import load_hidden, load_json, save_hidden
from plunderhub.core.scan_paths import (
    LAUNCHER_FLATPAK_IDS, STEAM_NON_GAME_NAME_PREFIXES, STEAM_PROTON_NAME_PATTERN,
    STEAM_SOUNDTRACK_NAME_PATTERN, bottles_library_path, faugus_games_path, faugus_umu_path,
    invocation_for_data, lutris_db_path, resolve_launcher_invocation,
)
from plunderhub.core.steam_heroic import heroic_roots, parse_appmanifest, steam_library_paths, steam_roots
from plunderhub.core.utils import which
from plunderhub.launchers.registry import LAUNCHER_REGISTRY
from plunderhub.logging_setup import logger


def mtime_signature(paths: List[Path]) -> Tuple[int, str, float]:
    latest = 0.0
    existing: List[str] = []
    for p in paths:
        try:
            if p.exists():
                existing.append(str(p))
                latest = max(latest, p.stat().st_mtime)
        except OSError:
            pass
    digest = hashlib.sha256("\0".join(sorted(existing)).encode("utf-8")).hexdigest()
    return (len(existing), digest, latest)


_SCAN_CACHE: Dict[str, Tuple[Tuple[int, str, float], List[GameItem]]] = {}

_APPMANIFEST_RE = re.compile(r'^appmanifest_\d+\.acf$')


def _query_lutris_db(db_path: Path, query):
    """Runs query(connection) on a private copy of pga.db, so a running
    Lutris never sees its database locked by us."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="plunderhub-lutris-"))
    try:
        tmp_db = tmp_dir / db_path.name
        shutil.copy2(db_path, tmp_db)
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.with_name(db_path.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, tmp_dir / (tmp_db.name + suffix))
        connection = sqlite3.connect(str(tmp_db))
        try:
            return query(connection)
        finally:
            connection.close()
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


def _read_lutris_games(db_path: Path) -> List[Tuple]:
    def query(connection):
        existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(games)")}
        optional_columns = [
            col if col in existing_columns else "NULL"
            for col in ("directory", "playtime", "lastplayed")
        ]
        select_list = ", ".join(["id", "name", "slug", "runner"] + optional_columns)
        where = (
            "WHERE name IS NOT NULL AND slug IS NOT NULL AND configPath IS NOT NULL "
            "AND installed AND runner IS NOT 'steam'"
        )
        cursor = connection.execute(f"SELECT {select_list} FROM games {where}")
        return list(cursor.fetchall())
    return _query_lutris_db(db_path, query)


_LUTRIS_GAME_INFO: Tuple[Optional[tuple], Dict[str, Tuple[str, str]]] = (None, {})


def read_lutris_game_info(game_id: str) -> Optional[Tuple[str, str]]:
    """(configpath, runner) of a Lutris game - configpath names its config
    file under Lutris's games/ folder (see lutris_game_config_path). Every
    game is read in one go and kept until pga.db changes, since this runs
    on the UI thread each time a Lutris game's context menu opens."""
    global _LUTRIS_GAME_INFO
    db_path = lutris_db_path()
    if db_path is None:
        return None
    try:
        stamp = tuple(_file_stamp(f) for f in (db_path, db_path.with_name(db_path.name + "-wal")))
        key = (str(db_path), stamp)
        cached_key, info = _LUTRIS_GAME_INFO
        if cached_key != key:
            rows = _query_lutris_db(db_path, lambda c: c.execute(
                "SELECT id, configpath, runner FROM games").fetchall())
            info = {str(gid): (str(cfg), str(runner or "")) for gid, cfg, runner in rows
                    if gid is not None and cfg}
            _LUTRIS_GAME_INFO = (key, info)
    except Exception as e:
        logger.debug(f"Couldn't read Lutris game info ({db_path}): {e}")
        return None
    return info.get(str(game_id))


def read_lutris_config_id(game_id: str) -> Optional[str]:
    info = read_lutris_game_info(game_id)
    return info[0] if info else None


def _file_stamp(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _shell_words(text: str) -> List[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _faugus_default_runner(games_path: Path) -> str:
    """Faugus's "default-runner" (config.json) - what a game set to
    "Default" runs with."""
    parts = games_path.parts
    if ".var" in parts and "data" in parts:
        # Flatpak: ~/.var/app/<id>/data/... -> ~/.var/app/<id>/config/...
        root = Path(*parts[:parts.index("data")])
        config = root / "config" / "faugus-launcher" / "config.json"
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "faugus-launcher" / "config.json"
    try:
        value = json.loads(config.read_text(encoding="utf-8")).get("default-runner", "")
    except (OSError, ValueError, AttributeError):
        value = ""
    return str(value or "").strip('"')


def _faugus_command(umu_path: str, exe_path: str, item: Dict) -> Tuple[List[str], Dict[str, str]]:
    """The command and extra env Faugus itself would launch a game with
    (faugus/runner.py): "launch_arguments" is raw text placed *before*
    umu-run - VAR=value words and wrapper commands - while the game's own
    arguments are "game_arguments", appended after the executable. The
    "gamemode"/"mangohud" switches add their wrapper when it's installed."""
    env: Dict[str, str] = {}
    wrappers: List[str] = []
    if item.get("gamemode") and which("gamemoderun"):
        wrappers.append("gamemoderun")
    if item.get("mangohud") and which("mangohud"):
        wrappers.append("mangohud")
    words = _shell_words(str(item.get("launch_arguments") or ""))
    while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        key, _, value = words.pop(0).partition("=")
        env[key] = value
    wrappers.extend(words)
    game_args = _shell_words(str(item.get("game_arguments") or ""))
    return [*wrappers, umu_path, exe_path, *game_args], env


# A Heroic already running takes --no-gui from the first start only: a
# launch handed to it opens its window unless the URL itself says no.
HEROIC_NO_WINDOW = "?gui=false"


def _heroic_sideload_games(data, path: Path, heroic_invocation: List[str]) -> List[GameItem]:
    """Games added to Heroic by hand: sideload_apps/library.json holds
    {"games": [{"app_name", "title", "install": {"executable", ...},
    "folder_name", "is_installed", ...}]}. Heroic launches them as
    heroic://launch/sideload/<app_name>."""
    items = data.get("games") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    games: List[GameItem] = []
    for item in items:
        if not isinstance(item, dict) or item.get("is_installed") is False:
            continue
        install = item.get("install") if isinstance(item.get("install"), dict) else {}
        if install.get("is_dlc"):
            continue
        app_name = item.get("app_name")
        if not app_name:
            continue
        folder = item.get("folder_name")
        if not folder and install.get("executable"):
            folder = str(Path(install["executable"]).parent)
        uri = f"heroic://launch/sideload/{app_name}"
        games.append(GameItem(
            launcher="Heroic",
            title=str(item.get("title") or app_name),
            uid=f"heroic:{path.parent.name}:{app_name}",
            launch_cmd=[*heroic_invocation, "--no-gui", uri + HEROIC_NO_WINDOW],
            launch_label=uri,
            install_dir=folder or None,
            store_platform="sideload",
        ))
    return games


def read_lutris_playtimes() -> Dict[str, Tuple[int, Optional[int]]]:
    db_path = lutris_db_path()
    if db_path is None:
        return {}
    try:
        rows = _read_lutris_games(db_path)
    except Exception as e:
        logger.warning(f"Couldn't read Lutris playtimes ({db_path}): {e}")
        return {}
    result: Dict[str, Tuple[int, Optional[int]]] = {}
    for game_id, _name, _slug, _runner, _directory, playtime_hours, lastplayed in rows:
        if game_id is None:
            continue
        minutes = round((playtime_hours or 0) * 60)
        result[str(game_id)] = (minutes, int(lastplayed) if lastplayed else None)
    return result


def read_faugus_playtimes() -> Dict[str, Tuple[int, Optional[int]]]:
    games_path = faugus_games_path()
    if games_path is None:
        return {}
    data = load_json(games_path)
    if not isinstance(data, list):
        return {}
    result: Dict[str, Tuple[int, Optional[int]]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        game_id = item.get("gameid")
        seconds = item.get("playtime")
        if not game_id or not isinstance(seconds, (int, float)):
            continue
        result[str(game_id)] = (int(seconds) // 60, None)
    return result


def _escape_bottles_uri_segment(value: str) -> str:
    """Bottles' own bottles:run/"<bottle>"/"<name>" URI scheme quotes
    each segment with literal double quotes - a bottle or game name
    that itself contains a " would otherwise break that syntax
    outright (e.g. corrupting which bottle/game gets launched, or
    making the whole URI unparseable). Backslash-escaping just that
    character leaves the overwhelmingly common case (no quote in the
    name) completely unchanged, rather than percent-encoding the
    whole segment, which would alter every URI's formatting (spaces,
    etc.) for a scheme this project doesn't control the parser for."""
    return value.replace('"', '\\"')


def bottles_program_executables(bottle_dir: str, program_id: str, program_name: str) -> Tuple[str, ...]:
    """The executable file name(s) of one program in a bottle, from the
    bottle's own bottle.yml (External_Programs) - what tells this game's
    processes apart from everything else running in the same prefix.
    Empty if it can't be determined."""
    try:
        cfg = yaml.safe_load((Path(bottle_dir) / "bottle.yml").read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"Couldn't read bottle.yml in {bottle_dir}: {e}")
        return ()
    programs = cfg.get("External_Programs") if isinstance(cfg, dict) else None
    if not isinstance(programs, dict):
        return ()
    entry = None
    for key, program in programs.items():
        if not isinstance(program, dict):
            continue
        if str(key) == program_id or str(program.get("id")) == program_id:
            entry = program
            break
        if entry is None and program.get("name") == program_name:
            entry = program
    if entry is None:
        return ()
    names = []
    for value in (entry.get("executable"), entry.get("path")):
        if isinstance(value, str) and value.strip():
            name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
            if name and name not in names:
                names.append(name)
    return tuple(names)


class Scanner:
    def __init__(self, enabled_launchers: set):
        self.hidden = load_hidden()
        self.enabled = enabled_launchers
        self.scan_errors: Dict[str, str] = {}

    def _record_scan_error(self, launcher: str, detail: str) -> None:
        self.scan_errors[launcher] = detail

    def is_hidden(self, launcher: str, uid: str) -> bool:
        return uid in set(self.hidden.get(launcher, []))

    def hide(self, launcher: str, uid: str) -> None:
        self.hidden.setdefault(launcher, [])
        if uid not in self.hidden[launcher]:
            self.hidden[launcher].append(uid)
            save_hidden(self.hidden)

    def unhide(self, launcher: str, uid: str) -> None:
        if launcher in self.hidden and uid in self.hidden[launcher]:
            self.hidden[launcher].remove(uid)
            if not self.hidden[launcher]:
                del self.hidden[launcher]
            save_hidden(self.hidden)

    def unhide_all(self) -> None:
        self.hidden = {}
        save_hidden(self.hidden)

    def scan_steam(self) -> List[GameItem]:
        # each Steam's own libraries, launched by that Steam (native and
        # Flatpak side by side; a leftover one's are skipped)
        libraries: Dict[Path, List[str]] = {}
        for root in steam_roots():
            invocation = invocation_for_data(root, "steam", LAUNCHER_FLATPAK_IDS["Steam"])
            if invocation is None:
                continue
            for lib in [root, *steam_library_paths(root)]:
                libraries.setdefault(lib, invocation)
        games: List[GameItem] = []
        seen = set()
        for lib, steam_invocation in libraries.items():
            steamapps = lib / "steamapps"
            if not steamapps.exists():
                continue
            for manifest in steamapps.glob("appmanifest_*.acf"):
                data = parse_appmanifest(manifest)
                if not data:
                    continue
                name_lower = data["name"].strip().lower()
                if any(name_lower.startswith(p) for p in STEAM_NON_GAME_NAME_PREFIXES):
                    continue
                if STEAM_PROTON_NAME_PATTERN.match(data["name"].strip()):
                    continue
                if STEAM_SOUNDTRACK_NAME_PATTERN.search(data["name"].strip()):
                    continue
                uid = f"steam:{data['appid']}"
                if uid in seen:
                    continue
                seen.add(uid)
                install_dir = None
                if data.get("installdir"):
                    install_dir = str(lib / "steamapps" / "common" / data["installdir"])
                games.append(GameItem(
                    launcher="Steam",
                    title=data["name"],
                    uid=uid,
                    # -applaunch, not steam://rungameid: a launch by URL
                    # brings up Steam's main window even when it was
                    # started -silent; this way Steam stays in the tray.
                    launch_cmd=[*steam_invocation, "-silent", "-applaunch", data["appid"]],
                    launch_label=f"steam://rungameid/{data['appid']}",
                    install_dir=install_dir,
                    store_url=f"https://store.steampowered.com/app/{data['appid']}",
                ))
        games.sort(key=lambda g: g.title.lower())
        return games

    def scan_heroic(self) -> List[GameItem]:
        games: List[GameItem] = []
        seen = set()
        for path in heroic_roots():
            # the Heroic this file belongs to (native or Flatpak)
            heroic_invocation = invocation_for_data(path, "heroic", LAUNCHER_FLATPAK_IDS["Heroic"])
            if heroic_invocation is None:
                continue
            data = load_json(path)
            if not isinstance(data, (dict, list)):
                continue
            items = []
            store_dir = path.parent.name.lower()
            is_gog = store_dir == "gog_store"
            is_amazon = store_dir == "nile"
            is_zoom = store_dir == "zoom_store"
            if store_dir == "sideload_apps":
                games.extend(g for g in _heroic_sideload_games(data, path, heroic_invocation)
                             if g.uid not in seen and not seen.add(g.uid))
                continue
            if is_gog:
                store_platform = "gog"
            elif is_amazon:
                store_platform = "amazon"
            elif is_zoom:
                store_platform = "zoom"
            else:
                store_platform = "epic"
            if isinstance(data, dict) and "installed" in data and isinstance(data["installed"], list):
                items = data["installed"]
            elif isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                for game_id, info in data.items():
                    if isinstance(info, dict):
                        info["__game_id"] = game_id
                        items.append(info)
            else:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("is_dlc"):
                    continue    # Legendary lists installed DLCs as entries of their own
                if is_gog:
                    game_id = item.get("appName") or item.get("game_id") or item.get("id")
                else:
                    game_id = (item.get("__game_id") or item.get("app_name") or item.get("game_id")
                              or item.get("appName") or item.get("id"))
                if not game_id:
                    continue
                title = (
                    item.get("title")
                    or item.get("app_title")
                    or item.get("name")
                    or item.get("display_name")
                )
                if not title and "install_path" in item:
                    title = Path(item["install_path"]).name
                if not title:
                    title = str(game_id)
                uid = f"heroic:{path.parent.name}:{game_id}"
                if uid in seen:
                    continue
                seen.add(uid)
                if is_gog:
                    uri = f"heroic://launch/gog/{game_id}"
                else:
                    uri = f"heroic://launch/{game_id}"
                games.append(GameItem(
                    launcher="Heroic",
                    title=str(title),
                    uid=uid,
                    launch_cmd=[*heroic_invocation, "--no-gui", uri + HEROIC_NO_WINDOW],
                    launch_label=uri,
                    install_dir=item.get("install_path"),
                    store_platform=store_platform,
                ))
        games.sort(key=lambda g: g.title.lower())
        return games

    def scan_faugus(self) -> List[GameItem]:
        games_path = faugus_games_path()
        if games_path is None:
            return []
        umu_path = faugus_umu_path()
        if umu_path is None:
            return []
        data = load_json(games_path)
        if not isinstance(data, list):
            return []
        games: List[GameItem] = []
        seen = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            # Faugus expands ~ and $VARS in these itself (runner.py expand_path)
            item = {**item, **{k: os.path.expandvars(os.path.expanduser(item[k]))
                               for k in ("path", "prefix", "launch_arguments", "game_arguments")
                               if isinstance(item.get(k), str)}}
            game_id = item.get("gameid")
            title = item.get("title")
            exe_path = item.get("path")
            prefix_path = item.get("prefix")
            if not all([game_id, title, exe_path, prefix_path]):
                continue
            if item.get("hidden", False):
                continue
            uid = f"faugus:{game_id}"
            if uid in seen:
                continue
            seen.add(uid)
            # the runner as Faugus itself resolves it (faugus/runner.py)
            runner = item.get("runner") or ""
            if runner == "Default":
                runner = _faugus_default_runner(games_path)
            if runner == "Steam":
                continue    # a Steam game Faugus only hands to Steam: listed under Steam
            cmd, launch_env = _faugus_command(str(umu_path), exe_path, item)
            env: Dict[str, str] = {}
            if runner == "Linux-Native":
                if item.get("disable_umu"):
                    cmd.remove(str(umu_path))     # run as it is
                else:
                    env["PROTONPATH"] = item.get("runtime") or "umu-sniper"
            else:
                env["WINEPREFIX"] = prefix_path
                if runner == "Proton-CachyOS (System)":
                    runner = "/usr/share/steam/compatibilitytools.d/proton-cachyos-slr/"
                if runner:
                    env["PROTONPATH"] = runner
            protonfix = item.get("protonfix") or ""
            if protonfix:
                env["GAMEID"] = protonfix
            # Faugus puts launch_arguments after these on its command line,
            # so its own VAR=value words win over them.
            env.update(launch_env)
            install_dir = None
            try:
                install_dir = str(Path(exe_path).parent)
            except Exception as e:
                logger.debug(f"Couldn't resolve Faugus install dir from {exe_path!r}: {e}")
            games.append(GameItem(
                launcher="Faugus",
                title=str(title),
                uid=uid,
                launch_cmd=cmd,
                launch_label=shlex.join(cmd),
                env=env,
                install_dir=install_dir,
            ))
        games.sort(key=lambda g: g.title.lower())
        return games

    def scan_lutris(self) -> List[GameItem]:
        lutris_invocation = resolve_launcher_invocation("lutris", LAUNCHER_FLATPAK_IDS["Lutris"])
        if lutris_invocation is None:
            return []
        db_path = lutris_db_path()
        if db_path is None:
            return []
        try:
            rows = _read_lutris_games(db_path)
        except Exception as e:
            logger.warning(f"Couldn't read Lutris database ({db_path}): {e}")
            self._record_scan_error("Lutris", str(e))
            return []
        games: List[GameItem] = []
        seen = set()
        for game_id, title, _slug, _runner, directory, _playtime, _lastplayed in rows:
            if not game_id or not title:
                continue
            uid = f"lutris:{game_id}"
            if uid in seen:
                continue
            seen.add(uid)
            games.append(GameItem(
                launcher="Lutris",
                title=str(title),
                uid=uid,
                launch_cmd=[*lutris_invocation, f"lutris:rungameid/{game_id}"],
                launch_label=f"lutris:rungameid/{game_id}",
                install_dir=str(directory) if directory else None,
            ))
        games.sort(key=lambda g: g.title.lower())
        return games

    def scan_bottles(self) -> List[GameItem]:
        bottles_invocation = resolve_launcher_invocation("bottles", LAUNCHER_FLATPAK_IDS["Bottles"])
        if bottles_invocation is None:
            return []
        library_path = bottles_library_path()
        if library_path is None:
            return []
        try:
            library = yaml.safe_load(library_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Couldn't read Bottles library ({library_path}): {e}")
            self._record_scan_error("Bottles", str(e))
            return []
        if not isinstance(library, dict):
            return []
        bottles_root = library_path.parent / "bottles"
        data_yml = library_path.parent / "data.yml"
        if data_yml.exists():
            try:
                custom = yaml.safe_load(data_yml.read_text(encoding="utf-8"))
                if isinstance(custom, dict) and custom.get("custom_bottles_path"):
                    bottles_root = Path(custom["custom_bottles_path"])
            except Exception as e:
                logger.debug(f"Couldn't read Bottles data.yml ({data_yml}): {e}")
        games: List[GameItem] = []
        seen = set()
        for entry in library.values():
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            game_id = entry.get("id")
            bottle = entry.get("bottle") or {}
            bottle_name = bottle.get("name")
            bottle_path = bottle.get("path")
            if not name or game_id is None or not bottle_name:
                continue
            uid = f"bottles:{game_id}"
            if uid in seen:
                continue
            seen.add(uid)
            uri = f'bottles:run/"{_escape_bottles_uri_segment(bottle_name)}"/"{_escape_bottles_uri_segment(name)}"'
            install_dir = str(bottles_root / bottle_path) if bottle_path else None
            games.append(GameItem(
                launcher="Bottles",
                title=str(name),
                uid=uid,
                launch_cmd=["xdg-open", uri],
                launch_label=uri,
                install_dir=install_dir,
            ))
        games.sort(key=lambda g: g.title.lower())
        return games

    def _source_paths_for(self, name: str) -> Optional[List[Path]]:
        if name == "Steam":
            paths: List[Path] = []
            roots = steam_roots()
            libraries = set(roots)
            for root in roots:
                paths.append(root / "steamapps" / "libraryfolders.vdf")
                libraries.update(steam_library_paths(root))
            for lib in libraries:
                steamapps = lib / "steamapps"
                if steamapps.exists():
                    paths.extend(steamapps.glob("appmanifest_*.acf"))
            return paths
        if name == "Heroic":
            return heroic_roots()
        if name == "Faugus":
            path = faugus_games_path()
            return [path] if path else []
        if name == "Lutris":
            path = lutris_db_path()
            return [path] if path else []
        if name == "Bottles":
            path = bottles_library_path()
            return [path] if path else []
        return None

    def scan_all(self):
        self.scan_errors = {}
        launcher_list = []
        for ldef in LAUNCHER_REGISTRY:
            if ldef.name not in self.enabled:
                continue

            if ldef.scan_method is None:
                cmd = resolve_launcher_invocation(ldef.binary, LAUNCHER_FLATPAK_IDS.get(ldef.name, []))
                launcher_list.append({"name": ldef.name, "cmd": cmd, "games": []})
                continue

            source_paths = self._source_paths_for(ldef.name)
            sig = mtime_signature(source_paths) if source_paths is not None else None
            games: Optional[List[GameItem]] = None

            if sig is not None:
                cached = _SCAN_CACHE.get(ldef.name)
                if cached is not None and cached[0] == sig:
                    games = cached[1]

            if games is None:
                scan_fn = getattr(self, ldef.scan_method)
                try:
                    games = scan_fn()
                except Exception as e:
                    # A single launcher's own scan blowing up (e.g. a
                    # race with Steam updating/deleting a game mid-
                    # scan, an unexpected file format) must not take
                    # down every other launcher's scan with it - this
                    # already-existing error-reporting path (used by
                    # scan_lutris/scan_bottles for their own known
                    # risky reads) surfaces it to the person via a
                    # toast instead of leaving the whole app stuck on
                    # its startup loading screen forever, since
                    # nothing else here would ever let scan_all()
                    # actually finish and hand control back.
                    logger.warning(f"{ldef.name} scan failed unexpectedly: {e}")
                    self._record_scan_error(ldef.name, str(e))
                    games = []
                else:
                    # A scan that hit a read error (scan_lutris/scan_bottles
                    # report it and return []) isn't cached: the source
                    # file's mtime hasn't changed, so a cached empty result
                    # would keep the games missing while the error toast
                    # silently stopped appearing on every later refresh.
                    # nor an empty one: the launcher (or umu-run) may only be
                    # installed later, with its data files unchanged
                    if sig is not None and ldef.name not in self.scan_errors and games:
                        _SCAN_CACHE[ldef.name] = (sig, games)
                    elif not games:
                        _SCAN_CACHE.pop(ldef.name, None)
                    elif ldef.name in self.scan_errors:
                        _SCAN_CACHE.pop(ldef.name, None)

            cmd = resolve_launcher_invocation(ldef.binary, LAUNCHER_FLATPAK_IDS.get(ldef.name, []))
            launcher_list.append({"name": ldef.name, "cmd": cmd, "games": games})
        return launcher_list
