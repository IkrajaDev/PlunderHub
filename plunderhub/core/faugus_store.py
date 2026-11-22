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

"""Games installed by a store launcher that runs in a Faugus prefix
(Battle.net, EA app, Epic, GOG Galaxy, Ubisoft Connect, Rockstar, Amazon,
Wargaming): found in the launcher's own records inside the prefix - the
same on every distro, it's all Windows paths under drive_c - and given a
Faugus entry of their own, so a card, art and launch options work as for
any game.

Each entry runs the launcher's program with the game's launch link: most
of these games refuse to start without their launcher. Which folder is
the game's is kept beside the entry (Faugus drops fields it doesn't know
when it writes its list back), and says whether the game itself runs."""

import json
import os
import re
import shlex
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.constants import CONFIG_DIR
from plunderhub.logging_setup import logger

SIDECAR = "faugus_store_games.json"

# Battle.net's product code -> the id its --exec takes, the name (Playnite's table)
BATTLE_GAMES = {
    "wow": ("WoW", "World of Warcraft"), "wow_classic": ("WoWC", "World of Warcraft Classic"),
    "diablo3": ("D3", "Diablo III"), "s2": ("S2", "StarCraft II"), "s1": ("S1", "StarCraft"),
    "hs_beta": ("WTCG", "Hearthstone"), "heroes": ("Hero", "Heroes of the Storm"),
    "prometheus": ("Pro", "Overwatch 2"), "viper": ("VIPR", "Call of Duty: Black Ops 4"),
    "odin": ("ODIN", "Call of Duty: Modern Warfare"), "w3": ("W3", "Warcraft III: Reforged"),
    "lazarus": ("LAZR", "Call of Duty: Modern Warfare 2 Campaign Remastered"),
    "zeus": ("ZEUS", "Call of Duty: Black Ops Cold War"), "wlby": ("WLBY", "Crash Bandicoot 4"),
    "osi": ("OSI", "Diablo II: Resurrected"), "rtro": ("RTRO", "Blizzard Arcade Collection"),
    "fore": ("FORE", "Call of Duty: Vanguard"), "anbs": ("ANBS", "Diablo Immortal"),
    "auks": ("AUKS", "Call of Duty: Modern Warfare II"), "fen": ("Fen", "Diablo IV"),
    "d1": ("D1", "Diablo"), "w1r": ("W1R", "Warcraft: Remastered"), "w2r": ("W2R", "Warcraft II: Remastered"),
    "w1": ("W1", "Warcraft: Orcs & Humans"),
}
_BATTLE_NOT_GAMES = {"agent", "bna", "battle.net"}


@dataclass(frozen=True)
class StoreGame:
    store_id: str
    title: str
    install_dir: str                # on the host
    launch_args: Tuple[str, ...]    # after the launcher's program


# --- reading a prefix --------------------------------------------------------

def _follow(root: Path, parts: List[str]) -> Path:
    """root/parts, each step matched without case, as Windows does."""
    here = root
    for part in parts:
        exact = here / part
        if exact.exists():
            here = exact
            continue
        try:
            here = next((c for c in here.iterdir() if c.name.lower() == part.lower()), exact)
        except OSError:
            here = exact
    return here


def host_path(prefix: Path, windows: str) -> Optional[Path]:
    """A Windows path in this prefix, on the host (C: is drive_c, the rest
    the prefix's dosdevices)."""
    text = str(windows or "").strip().strip('"').replace("\\", "/")
    if len(text) < 3 or text[1] != ":":
        return None
    parts = [p for p in text[3:].split("/") if p]
    letter = text[0].lower()
    root = prefix / "drive_c" if letter == "c" else prefix / "dosdevices" / f"{letter}:"
    return _follow(root, parts)


_REG_KEY = re.compile(r"^\[(.+?)\](?:\s+\d+)?\s*$")
_REG_VALUE = re.compile(r'^"((?:[^"\\]|\\.)*)"=(?:str\(\d+\):)?"((?:[^"\\]|\\.)*)"\s*$')


def _reg_text(raw: str) -> str:
    def one(m):
        code = m.group(1)
        if code[0] == "x":
            return chr(int(code[1:], 16))
        return {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(code, code)
    return re.sub(r"\\(x[0-9a-fA-F]{1,4}|.)", one, raw)


def read_reg(path: Path, under: str) -> Dict[str, Dict[str, str]]:
    """The text values of a Wine .reg file's keys under `under` (no case):
    {key after it: {value name: value}}."""
    wanted = under.lower().rstrip("\\") + "\\"
    keys: Dict[str, Dict[str, str]] = {}
    current = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("["):
                match = _REG_KEY.match(line)
                name = _reg_text(match.group(1)) if match else ""
                current = None
                if name.lower().startswith(wanted):
                    current = keys.setdefault(name[len(wanted):], {})
            elif current is not None:
                match = _REG_VALUE.match(line)
                if match:
                    current[_reg_text(match.group(1))] = _reg_text(match.group(2))
    return keys


def _protobuf(data: bytes):
    """A protobuf message's fields: (number, value), bytes or int."""
    i = 0

    def varint():
        nonlocal i
        value = shift = 0
        while True:
            byte = data[i]
            i += 1
            value |= (byte & 0x7f) << shift
            shift += 7
            if not byte & 0x80:
                return value
    while i < len(data):
        key = varint()
        number, wire = key >> 3, key & 7
        if wire == 0:
            yield number, varint()
        elif wire == 1:
            yield number, data[i:i + 8]
            i += 8
        elif wire == 2:
            size = varint()
            yield number, data[i:i + size]
            i += size
        elif wire == 5:
            yield number, data[i:i + 4]
            i += 4
        else:
            raise ValueError(f"protobuf wire type {wire}")


def _existing(prefix: Path, windows: str) -> Optional[Path]:
    path = host_path(prefix, windows)
    return path if path is not None and path.is_dir() else None


def _epic(prefix: Path) -> List[StoreGame]:
    games = []
    for item in _follow(prefix, "drive_c/ProgramData/Epic/EpicGamesLauncher/Data/Manifests".split("/")).glob("*.item"):
        data = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        if data.get("bIsIncompleteInstall") or "games" not in (data.get("AppCategories") or ["games"]):
            continue
        folder = _existing(prefix, data.get("InstallLocation"))
        app = data.get("AppName")
        if folder is None or not app:
            continue
        link = f"com.epicgames.launcher://apps/{data.get('CatalogNamespace')}%3A{data.get('CatalogItemId')}%3A{app}" \
               "?action=launch&silent=true"
        games.append(StoreGame(str(app), str(data.get("DisplayName") or folder.name), str(folder), (link,)))
    return games


def windows_uninstaller(prefix: Path, install_dir: str) -> Optional[List[str]]:
    """The game's own uninstaller as its installer wrote it into the
    prefix's Windows "Uninstall" list (the entry whose InstallLocation is
    the game's folder): the host path of the program and its arguments;
    None when there's none. Blizzard's (Blizzard Uninstaller --uid=...),
    EA's (Cleanup.exe uninstall_game: the EA app's own Uninstall did
    nothing on Wine - "Uninstall path ... is empty", it looks in the other
    registry view), Inno / GOG's unins000.exe..."""
    want = os.path.realpath(install_dir).rstrip("/").lower()
    if not want:
        return None
    # the machine's list, then the user's (Wargaming writes its games' there - HKCU, 11-22)
    for reg, under in ((_system_reg(prefix), "Software\\Wow6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall"),
                       (_system_reg(prefix), "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall"),
                       (prefix / "user.reg", "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall")):
        if not reg.is_file():
            continue
        try:
            keys = read_reg(reg, under)
        except OSError:
            continue
        for values in keys.values():
            location = host_path(prefix, values.get("InstallLocation") or "")
            line = values.get("UninstallString") or ""
            if location is None or not line or os.path.realpath(location).rstrip("/").lower() != want:
                continue
            try:      # a Windows command line: its backslashes aren't escapes
                words = shlex.split(line.replace("\\", "/"))
            except ValueError:
                continue
            exe = host_path(prefix, words[0]) if words else None
            if exe is not None and exe.is_file():
                return [str(exe)] + words[1:]
    return None


def _system_reg(prefix: Path) -> Path:
    return prefix / "system.reg"


def _gog(prefix: Path) -> List[StoreGame]:
    games = []
    for key, values in read_reg(_system_reg(prefix), r"Software\Wow6432Node\GOG.com\Games").items():
        folder = _existing(prefix, values.get("path"))
        if folder is None or "\\" in key:
            continue
        games.append(StoreGame(key, values.get("gameName") or folder.name, str(folder),
                               ("/command=runGame", f"/gameId={key}", f"/path={values.get('path')}")))
    return games


def downloading_ubisoft(folder: Path, product: str) -> bool:
    """Ubisoft Connect is still downloading this game: its download's
    state (a byte a slice) is in the game's uplay_download/<product>."""
    try:
        return any((folder / "uplay_download" / str(product)).glob("*.state"))
    except OSError:
        return False


def _ubisoft(prefix: Path) -> List[StoreGame]:
    games = []
    for key, values in read_reg(_system_reg(prefix), r"Software\Wow6432Node\Ubisoft\Launcher\Installs").items():
        folder = _existing(prefix, values.get("InstallDir"))
        if folder is not None and downloading_ubisoft(folder, key):
            continue        # its Installs key is written as its download starts: a card once it's done
        if folder is not None and "\\" not in key:
            games.append(StoreGame(key, folder.name, str(folder), (f"uplay://launch/{key}/0",)))
    return games


# Rockstar's service logs every piece it writes into a game's folder
# (ProgramData/Rockstar Games/Launcher/service_log.txt, local time):
#   [2026-11-22 15:41:35.404] [   996] [  3F4] [ 510] Wrote 10485760 bytes (C:\Program Files\...\x64c.rpf)
# and makes the game's files full size at once (sparse).
_ROCKSTAR_WROTE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[.\d]*\].*? Wrote \d+ bytes \((.+)\)\s*$")
ROCKSTAR_WRITING = 120.0        # written into this long ago: still downloading (or just paused)
ROCKSTAR_DONE_SHARE = 0.9       # less than this much of its files really there: not done


def rockstar_last_write(prefix: Path, folder: Path) -> Optional[float]:
    """When Rockstar's service last wrote into this game's folder (from
    its log's tail), None when its tail doesn't say."""
    log = _follow(prefix, "drive_c/ProgramData/Rockstar Games/Launcher".split("/")) / "service_log.txt"
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    want = os.path.realpath(folder).rstrip("/").lower() + "/"
    for line in reversed(lines):
        m = _ROCKSTAR_WROTE.match(line.strip())
        if not m:
            continue
        host = host_path(prefix, m.group(2))
        if host is None or not (os.path.realpath(host).lower() + "/").startswith(want):
            continue
        try:
            return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return None
    return None


def disk_share(folder: Path):
    """(bytes really on disk, length) of a folder's files - a launcher that
    makes its files full size at once has the first far below the second
    till it's done."""
    done = total = 0
    stack = [str(folder)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            total += st.st_size
                            done += min(st.st_size, st.st_blocks * 512)
                    except OSError:
                        continue
        except OSError:
            continue
    return done, total


_ROCKSTAR_INSTALLED = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[.\d]*\].*\[titlemanager\] Installed \S+ "
                                 r"version in registry updated")


def rockstar_installed_at(prefix: Path) -> Optional[float]:
    """When the launcher last wrote down a finished install ("Installed
    gta5_gen9 version in registry updated: -> 1.0.1158.16"), from its log's
    tail; None when it doesn't say."""
    log = _follow(prefix, "drive_c/users/steamuser/Documents/Rockstar Games/Launcher".split("/")) / "launcher.log"
    try:
        with open(log, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 512 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        m = _ROCKSTAR_INSTALLED.match(line.strip())
        if m:
            try:
                return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                return None
    return None


def rockstar_downloading(prefix: Path, folder: Path, version: str = "") -> bool:
    """Rockstar is still downloading (or has paused) this game: its key has
    no Version yet (written when the install is done) and its log hasn't
    said it's installed since it last wrote into the folder. (Wine writes
    its registry file down a little later: the log says it first.) A key
    with no Version from an install that's long done: most of its files there."""
    if version:
        return False        # installed (an update of it keeps its card)
    last = rockstar_last_write(prefix, folder)
    installed = rockstar_installed_at(prefix)
    if last is not None and installed is not None and installed >= last - 1:
        return False
    if last is not None and time.time() - last < ROCKSTAR_WRITING:
        return True
    done, total = disk_share(folder)
    return bool(total) and done / total < ROCKSTAR_DONE_SHARE


def _rockstar(prefix: Path) -> List[StoreGame]:
    games = []
    for key, values in read_reg(_system_reg(prefix), r"Software\Wow6432Node\Rockstar Games").items():
        if "\\" in key or key.lower() in ("launcher", "rockstar games social club", "social club"):
            continue
        folder = _existing(prefix, values.get("InstallFolder"))
        if folder is not None and rockstar_downloading(prefix, folder, str(values.get("Version") or "")):
            continue        # its key is written as its download starts: a card once it's done
        if folder is not None:
            # its folder has the game's full name ("GTAV Enhanced" -> "Grand Theft Auto V Enhanced")
            games.append(StoreGame(key, folder.name or key, str(folder),
                                   ("-launchTitleInFolder", values.get("InstallFolder"))))
    return games


_EA_LINK = "origin2://game/launch?offerIds="


def _ea(prefix: Path) -> List[StoreGame]:
    games = []
    for manifest in _follow(prefix, "drive_c/Program Files/EA Games".split("/")).glob("*/__Installer/installerdata.xml"):
        root = ET.parse(manifest).getroot()
        folder = manifest.parent.parent
        titles = {(t.get("locale") or "").lower(): (t.text or "").strip() for t in root.iter() if t.tag == "gameTitle"}
        title = titles.get("en_us") or next((t for t in titles.values() if t), folder.name)
        ids = [(c.text or "").strip() for c in root.iter() if c.tag == "contentID" and (c.text or "").strip()]
        if ids:
            # all of them: the first is often not the edition owned - the EA app then
            # said EntitlementNotFound, reloaded its list and found it only ~7 s later
            # (11-22 NFS Most Wanted: 71628 given, 71530 owned)
            games.append(StoreGame(ids[0], title, str(folder),
                                   (f"{_EA_LINK}{','.join(ids)}&autoDownload=1",)))
    return games


def _battle(prefix: Path) -> List[StoreGame]:
    db = _follow(prefix, "drive_c/ProgramData/Battle.net/Agent/product.db".split("/"))
    if not db.is_file():
        return []
    games = []
    for number, install in _protobuf(db.read_bytes()):
        if number != 1 or not isinstance(install, bytes):
            continue
        fields = dict(_protobuf(install))
        code = fields.get(2, b"").decode("utf-8", "replace")
        settings = dict(_protobuf(fields[3])) if isinstance(fields.get(3), bytes) else {}
        path = settings.get(1, b"").decode("utf-8", "replace")
        folder = _existing(prefix, path)
        if folder is None or not code or code.lower() in _BATTLE_NOT_GAMES:
            continue
        product, title = BATTLE_GAMES.get(code.lower(), (code.upper(), folder.name))
        games.append(StoreGame(code, title, str(folder), (f"--exec=launch {product}",)))
    return games


def _amazon(prefix: Path) -> List[StoreGame]:
    db = _follow(prefix, "drive_c/users/steamuser/AppData/Local/Amazon Games/Data/Games/Sql/GameInstallInfo.sqlite"
                 .split("/"))
    if not db.is_file():
        return []
    con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT Id, InstallDirectory, ProductTitle FROM DbSet WHERE Installed = 1").fetchall()
    finally:
        con.close()
    games = []
    for game_id, directory, title in rows:
        folder = _existing(prefix, directory)
        if folder is not None and game_id:
            games.append(StoreGame(str(game_id), str(title or folder.name), str(folder),
                                   (f"amazon-games://play/{game_id}",)))
    return games


def wargaming_exe(folder: Path) -> Optional[Path]:
    """The program Game Center itself starts for a game it installed (its
    game_metadata/metadata.xml <executables>: the 64-bit one first -
    win64\\WorldOfTanks.exe, run with no arguments; the game gets its
    sign-in from the running Game Center). None when there's none."""
    try:
        root = ET.parse(folder / "game_metadata" / "metadata.xml").getroot()
    except (OSError, ET.ParseError):
        return None
    nodes = list(root.iter("executable"))
    nodes.sort(key=lambda n: n.get("arch") != "x64")
    for node in nodes:
        rel = (node.text or "").strip().replace("\\", "/")
        if rel:
            exe = _follow(folder, [p for p in rel.split("/") if p])
            if exe.is_file():
                return exe
    return None


def wargaming_installed(folder: Path) -> bool:
    """Game Center has finished installing this game: its game_info.xml
    says <installed>true</installed> (false from the download's start,
    app_type.xml "incomplete" meanwhile). No game_info.xml: taken as there."""
    info = folder / "game_info.xml"
    try:
        node = ET.parse(info).getroot().find(".//game/installed")
    except (OSError, ET.ParseError):
        return not info.exists()
    return node is None or (node.text or "").strip().lower() != "false"


def _wargaming(prefix: Path) -> List[StoreGame]:
    prefs = _follow(prefix, "drive_c/ProgramData/Wargaming.net/GameCenter/preferences.xml".split("/"))
    if not prefs.is_file():
        return []
    games = []
    for node in ET.parse(prefs).getroot().iterfind(".//games_manager/games/game"):
        path = (node.findtext("working_dir") or "").strip()
        folder = _existing(prefix, path)
        if folder is not None and not wargaming_installed(folder):
            continue        # in its list as its download starts: a card once it's installed
        if folder is not None:
            # no launch switch of Game Center is documented: it opens, Play is there
            title = re.sub(r"[_ ](EU|NA|RU|ASIA|CN|KR|CT)$", "", folder.name, flags=re.I).replace("_", " ")
            games.append(StoreGame(folder.name, title, str(folder), ()))
    return games


_READERS = {"epic": _epic, "gog": _gog, "ubisoft": _ubisoft, "rockstar": _rockstar, "ea": _ea, "battle": _battle,
            "amazon": _amazon, "wargaming": _wargaming}


def found_games(kind: str, prefix: Path) -> Optional[List[StoreGame]]:
    """The launcher's installed games; None when its records can't be read
    (locked, half written): that says nothing about what's installed."""
    try:
        return _READERS[kind](prefix)
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.info(f"Faugus store games: {kind} in {prefix} not read: {e}")
        return None


# --- the entries -------------------------------------------------------------

def _sidecar_path() -> Path:
    return CONFIG_DIR / SIDECAR


def known() -> Dict[str, Dict]:
    """{gameid: {"launcher": its launcher's gameid, "kind", "store_id", "install_dir"}}"""
    try:
        data = json.loads(_sidecar_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: Dict[str, Dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _sidecar_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _sidecar_path())


def sync(site) -> bool:
    from plunderhub.core.file_lock import held
    with held(site.games_json):
        return _sync(site)


def _sync(site) -> bool:
    """Brings Faugus' list in line with what the launchers have installed.
    Not while Faugus is open: it writes its own list back when it closes.
    True when something changed. A worker thread."""
    from plunderhub.core import faugus_add as fa
    from plunderhub.core.launch_options import _write_with_backup
    if fa.faugus_running():
        return False
    try:
        games = json.loads(site.games_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(games, list):
        return False
    side = {gid: info for gid, info in known().items()
            if any(isinstance(g, dict) and g.get("gameid") == gid for g in games)}
    changed_side = len(side) != len(known())
    entries = [g for g in games if isinstance(g, dict)]
    have = {(info.get("launcher"), info.get("store_id")): gid for gid, info in side.items()}
    added, gone = False, []
    for launcher in [g for g in entries if g.get("gameid") not in side]:
        kind = fa.launcher_kind(str(launcher.get("path") or ""))
        prefix = Path(os.path.expanduser(str(launcher.get("prefix") or "")))
        if kind is None or not prefix.is_dir():
            continue
        found = found_games(kind, prefix)
        if found is None:
            continue
        lid = str(launcher.get("gameid"))
        ids = {g.store_id for g in found}
        gone += [gid for (owner, store_id), gid in have.items() if owner == lid and store_id not in ids]
        for game in found:
            gid = have.get((lid, game.store_id))
            if gid is not None:
                # an EA game added with the old one-id link gets the new one (not one changed by hand)
                entry = next((g for g in entries if g.get("gameid") == gid), None)
                new_args = shlex.join(game.launch_args)
                old_args = str(entry.get("game_arguments") or "") if entry else ""
                if kind == "ea" and entry is not None and old_args != new_args \
                        and re.fullmatch(r"'?" + re.escape(_EA_LINK) + r"[^,\s']+&autoDownload=1'?", old_args):
                    entry["game_arguments"] = new_args
                    added = True
                    logger.info(f"Faugus store games: {gid} gets every content id in its EA link")
                continue
            taken = {str(g.get("gameid")) for g in entries}
            title = re.sub(r"[™®©]", "", game.title).strip()
            gid = fa.unique_gameid(fa.format_title(title) or "game", taken)
            entries.append({**launcher, "gameid": gid, "title": title, "cover": "", "icon": "", "playtime": 0,
                            "last_played": "", "steamgriddb_id": "", "addapp_enabled": "", "addapp_bat": "",
                            "game_arguments": shlex.join(game.launch_args), "hidden": False})
            games.append(entries[-1])
            side[gid] = {"launcher": lid, "kind": kind, "store_id": game.store_id, "install_dir": game.install_dir}
            changed_side = added = True
            logger.info(f"Faugus store games: {game.title} found in {kind}, added as {gid}")
    launchers_left = {str(g.get("gameid")) for g in entries}
    gone += [gid for gid, info in side.items() if str(info.get("launcher")) not in launchers_left and gid not in gone]
    if added:
        _write_with_backup(site.games_json, json.dumps(games, indent=4, ensure_ascii=False))
    for gid in gone:
        ok, why = fa.remove_game(site, gid, False)
        if ok:
            side.pop(gid, None)
            changed_side = True
            logger.info(f"Faugus store games: {gid} is gone from its launcher, its entry too")
        else:
            logger.info(f"Faugus store games: {gid} stays: {why}")
    if changed_side:
        _save(side)
    return added or bool(gone)
