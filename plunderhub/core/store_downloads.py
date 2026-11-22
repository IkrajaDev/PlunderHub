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

"""Games a store launcher in a Faugus prefix is downloading right now, for
the Loading Dock (11-22). Read from the launcher's own files, nothing
asked of it:

Battle.net - its client asks its Agent for every install's state about
once a second and the Agent logs the answer whole
(ProgramData/Battle.net/Agent/Agent.<n>/Logs/Agent-*.log):
    [I 2026-11-22 10:17:53.0786] Request GET /install/osi
    Response 200 (0.3350 ms): { ..."progress": 0.208, "download_rate": 78068240,
    "download_total": [29588861925, 0, 0], "paused": false, "installed": false ... }
(its stamps are UTC)."""

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

_TAIL = 256 * 1024
_REQUEST = re.compile(r"^\[[A-Z] (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[.\d]*\] Request GET /install/([A-Za-z0-9_]+)\s*$")
# the Agent's answers stop when the client stops asking (closed, or not on a page that asks)
STALE_SECONDS = 15.0


@dataclass
class StoreDownload:
    kind: str           # the launcher's kind (faugus_add.LAUNCHERS)
    code: str           # the launcher's id of the game
    title: str
    frac: float
    rate: float         # bytes a second
    total: int          # bytes
    done_bytes: int
    paused: bool
    finished: bool
    install_dir: str = ""   # on the host, when known (finds the game's Faugus entry)
    installing: bool = False    # downloaded, its launcher unpacking / installing it now
    art: str = ""           # a picture of it the launcher itself keeps (before Steam's by its name)


def _tail(path: Path) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _battle(prefix: Path) -> List[StoreDownload]:
    from plunderhub.core.faugus_store import BATTLE_GAMES, _BATTLE_NOT_GAMES, _follow
    agent = _follow(prefix, "drive_c/ProgramData/Battle.net/Agent".split("/"))
    try:
        logs = sorted(agent.glob("Agent.*/Logs/Agent-*.log"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return []
    if not logs or time.time() - logs[-1].stat().st_mtime > STALE_SECONDS:
        return []
    lines = _tail(logs[-1]).splitlines()      # it writes \r\n
    latest: Dict[str, Dict] = {}
    i = 0
    while i < len(lines):
        m = _REQUEST.match(lines[i])
        i += 1
        if not m or i >= len(lines) or not lines[i].startswith("Response 200") or not lines[i].rstrip().endswith("{"):
            continue
        code = m.group(2).lower()
        body = ["{"]
        i += 1
        while i < len(lines) and lines[i] != "}":
            body.append(lines[i])
            i += 1
        if i >= len(lines):
            break                                   # cut off at the end: the next look has it
        body.append("}")
        try:
            data = json.loads("\n".join(body))
        except ValueError:
            continue
        at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
        latest[code] = {"at": at, "data": data}
    out = []
    now = time.time()
    for code, got in latest.items():
        data = got["data"]
        if code in _BATTLE_NOT_GAMES or now - got["at"] > STALE_SECONDS:
            continue
        progress = float(data.get("progress") or 0.0)
        installed = bool(data.get("installed")) or bool(data.get("download_complete") and progress >= 1.0)
        total = (data.get("download_total") or [0])[0] or (data.get("extended_status") or {}).get("total") or 0
        current = (data.get("download_current") or [0])[0] or (data.get("extended_status") or {}).get("current") or 0
        if installed and not total:
            continue                                # an installed game it only looked at
        title = BATTLE_GAMES.get(code, (code.upper(), code.upper()))[1]
        out.append(StoreDownload("battle", code, title, min(1.0, progress), float(data.get("download_rate") or 0),
                                 int(total or 0), int(current or 0), bool(data.get("paused")),
                                 installed or progress >= 1.0))
    return out


# EA app (EADesktopVerbose.log, ~1/s while it downloads; stamps UTC ISO):
#   ...GamesManagerProxy::getDownloadProgress)\treceived download progress downloaded = [3155845698],
#      Kb/s = [95733.0], timeRemaining = [36.81148], offer = [OFB-EAST:52699]
#   ...ProgressManager::Impl::updateTaskbar)\tprogress: value=40, range=[0, 100], paused=false
# and EADesktop.log: initiateDownload() called for offerId=[..] slug[..] ... installPath=[C:\...]
# then, once in place: IS update: ... baseSlug=[slug] installedStatus=[Installed]
_EA_STAMP = re.compile(r"\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")
_EA_PROGRESS = re.compile(r"getDownloadProgress\)\s+received download progress downloaded = \[(\d+)\], "
                          r"Kb/s = \[([\d.]+)\], timeRemaining = \[([\d.]+)\], offer = \[([^\]]+)\]")
_EA_TASKBAR = re.compile(r"updateTaskbar\)\s+progress: value=(\d+), range=\[0, 100\], paused=(true|false)")


def _ea_at(line: str) -> float:
    m = _EA_STAMP.search(line)
    if not m:
        return 0.0
    return datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def _ea(prefix: Path) -> List[StoreDownload]:
    """The download the EA app's progress lines are about (one at a time):
    which game from any main-log line naming its offerId with its slug
    (initiateDownload / initiateUninstall / ...: a reinstall started from its
    game page logs no initiateDownload), done once the game's latest
    installedStatus, newer than the last progress line, is Installed."""
    from plunderhub.core.faugus_store import _follow, host_path
    logs = _follow(prefix, "drive_c/ProgramData/EA Desktop/Logs".split("/"))
    verbose, main = logs / "EADesktopVerbose.log", logs / "EADesktop.log"
    progress = taskbar = None
    for line in reversed(_tail(verbose).splitlines()):
        if progress is None and "getDownloadProgress" in line:
            m = _EA_PROGRESS.search(line)
            if m:
                progress = (_ea_at(line), m)
        elif taskbar is None and "updateTaskbar" in line:
            m = _EA_TASKBAR.search(line)
            if m:
                taskbar = m
        if progress is not None and taskbar is not None:
            break
    if progress is None or time.time() - progress[0] > 120:
        return []
    at, m = progress
    offer = m.group(4)
    slug, path, status = "", "", None
    for line in _tail(main).splitlines():
        if f"offerId=[{offer}]" in line:
            got = re.search(r"slug=?\[([^\]]+)\]", line)
            if got:
                slug = got.group(1)
            where = re.search(r"installPath=\[([^\]]+)\]", line)
            if where:
                path = where.group(1)
        if slug and f"baseSlug=[{slug}]" in line:
            st = re.search(r"installedStatus=\[([^\]]+)\]", line)
            if st:
                status = (_ea_at(line), st.group(1))
    if not slug:
        return []
    folder = host_path(prefix, path) if path else None
    title = _ea_title(folder) or slug.replace("-", " ").title()
    if status is not None and status[1] == "Installed" and status[0] >= at:
        return [StoreDownload("ea", slug, title, 1.0, 0.0, 0, 0, False, True, str(folder or ""))]
    if time.time() - at > STALE_SECONDS:
        return []
    done, rate, left = int(m.group(1)), float(m.group(2)) * 1024, float(m.group(3))
    frac = int(taskbar.group(1)) / 100 if taskbar is not None else 0.0
    total = int(done + rate * left) if rate > 0 else (int(done / frac) if frac > 0 else 0)
    paused = taskbar is not None and taskbar.group(2) == "true"
    return [StoreDownload("ea", slug, title, frac, rate, total, done, paused, False, str(folder or ""))]


def _ea_title(folder: Optional[Path]) -> str:
    """The game's own title once its installer data has landed."""
    if folder is None:
        return ""
    import xml.etree.ElementTree as ET
    data = folder / "__Installer" / "installerdata.xml"
    try:
        root = ET.parse(data).getroot()
    except (OSError, ET.ParseError):
        return ""
    titles = {(t.get("locale") or "").lower(): (t.text or "").strip() for t in root.iter() if t.tag == "gameTitle"}
    title = titles.get("en_us") or next((t for t in titles.values() if t), "")
    return re.sub(r"[™®©]|\(TM\)", "", title).strip()


# Epic Games Launcher: an install in progress has its manifest in
# Data/Manifests/Pending/<id>.item (DisplayName, InstallSize, InstallLocation,
# bIsIncompleteInstall); once done it's in Data/Manifests/ itself. It logs no
# progress, so how far it is = the size of the install folder (its .egstore
# staging included) against InstallSize; the speed from how that grows (rate -1:
# worked out by the caller between looks).
def _folder_size(folder: Path) -> int:
    total = 0
    stack = [str(folder)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _epic(prefix: Path) -> List[StoreDownload]:
    from plunderhub.core.faugus_store import _follow, host_path
    manifests = _follow(prefix, "drive_c/ProgramData/Epic/EpicGamesLauncher/Data/Manifests".split("/"))
    out = []
    pending = {}
    for item in (manifests / "Pending").glob("*.item"):
        try:
            data = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if "games" not in (data.get("AppCategories") or ["games"]) or not data.get("AppName"):
            continue
        pending[str(data["AppName"])] = data
    for app, data in pending.items():
        folder = host_path(prefix, data.get("InstallLocation") or "")
        total = int(data.get("InstallSize") or 0)
        done = _folder_size(folder) if folder is not None and folder.is_dir() else 0
        frac = min(0.99, done / total) if total else 0.0
        out.append(StoreDownload("epic", app.lower(), str(data.get("DisplayName") or app), frac, -1.0, total, done,
                                 False, False, str(folder or "")))
    # finished: its manifest moved out of Pending, complete
    for item in manifests.glob("*.item"):
        try:
            data = json.loads(item.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        app = str(data.get("AppName") or "")
        if app and app not in pending and not data.get("bIsIncompleteInstall") \
                and time.time() - item.stat().st_mtime < 120:
            folder = host_path(prefix, data.get("InstallLocation") or "")
            out.append(StoreDownload("epic", app.lower(), str(data.get("DisplayName") or app), 1.0, 0.0,
                                     int(data.get("InstallSize") or 0), int(data.get("InstallSize") or 0),
                                     False, True, str(folder or "")))
    return out


# GOG Galaxy (2.1.13, 11-22): its jobs.db keeps the download's job (gameName, productId) but its
# progress columns stay 0 till it's paused; GalaxyClient.log says the rest:
#   InstallProductTask for product '1552771812' (...) starting installation to 'C:\...\A Plague Tale Requiem'
#   Initializing download for product 1552771812 (...), files to download 2897 (uncompressed size 53153325182); ...
#   Job 'ProductInstall (1552771812) id: 1 state: Running' state changed
# Its files are made full size at once (sparse): how far = the blocks its folder really holds.
_GOG_TARGET = re.compile(r"InstallProductTask for product '(\d+)' .*? starting installation to '([^']+)'")
_GOG_SIZE = re.compile(r"Initializing download for product (\d+) .*?uncompressed size (\d+)\)")
_GOG_STATE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)[.\d]* .*?Job '(?:ProductInstall|ProductUpdate)"
                        r" \((\d+)\) id: \d+ state: (\w+)' state changed")
_gog_logs: Dict[str, dict] = {}     # log path -> {"at": bytes read, "games": {product: {...}}}


def _gog_log(path: Path) -> Dict[str, dict]:
    """What the log has said of each product's install so far (read on from
    where the last look stopped)."""
    try:
        size = path.stat().st_size
    except OSError:
        return {}
    seen = _gog_logs.get(str(path))
    if seen is None or size < seen["at"]:
        seen = _gog_logs[str(path)] = {"at": 0, "games": {}}
    if size > seen["at"]:
        with open(path, "rb") as f:
            f.seek(seen["at"])
            data = f.read(size - seen["at"])
        cut = data.rfind(b"\n") + 1         # a line still being written: read next time
        seen["at"] += cut
        for line in data[:cut].decode("utf-8", "replace").splitlines():
            if "product" not in line.lower() and "Product" not in line:
                continue
            m = _GOG_TARGET.search(line)
            if m:
                seen["games"].setdefault(m.group(1), {})["path"] = m.group(2)
                continue
            m = _GOG_SIZE.search(line)
            if m:
                seen["games"].setdefault(m.group(1), {})["total"] = int(m.group(2))
                continue
            m = _GOG_STATE.search(line)
            if m:
                game = seen["games"].setdefault(m.group(2), {})
                game["state"] = m.group(3).lower()
                try:
                    game["at"] = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    game["at"] = time.time()
    return seen["games"]


def _disk_used(folder: Path) -> int:
    total = 0
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
                            total += min(st.st_size, st.st_blocks * 512)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _gog(prefix: Path) -> List[StoreDownload]:
    import sqlite3
    from plunderhub.core.faugus_store import _follow, host_path
    galaxy = _follow(prefix, "drive_c/ProgramData/GOG.com/Galaxy".split("/"))
    games = _gog_log(galaxy / "logs" / "GalaxyClient.log")
    if not games:
        return []
    titles = {}
    try:
        con = sqlite3.connect(f"file:{galaxy / 'storage' / 'jobs.db'}?mode=ro", uri=True, timeout=1.0)
        try:
            for table in ("InstallProductJobs", "UpdateProductJobs"):
                for name, product in con.execute(f"SELECT gameName, productId FROM {table}"):
                    titles[str(product)] = str(name or "")
        finally:
            con.close()
    except sqlite3.Error:
        pass
    out = []
    for product, game in games.items():
        state, path = game.get("state"), game.get("path")
        if not state or not path:
            continue
        folder = host_path(prefix, path)
        title = titles.get(product) or path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        total = int(game.get("total") or 0)
        if state in ("finished", "succeeded", "completed", "done"):
            if time.time() - game.get("at", 0) < 120:
                out.append(StoreDownload("gog", product, title, 1.0, 0.0, total, total, False, True,
                                         str(folder or "")))
            continue
        if state not in ("running", "paused", "pending", "queued", "waiting") or product not in titles:
            continue            # failed / cancelled (its job gone from jobs.db too)
        done = _disk_used(folder) if folder is not None and folder.is_dir() else 0
        frac = min(0.99, done / total) if total else 0.0
        out.append(StoreDownload("gog", product, title, frac, -1.0, total, done, state != "running", False,
                                 str(folder or "")))
    return out


# Ubisoft Connect (11-22): its Installs key (InstallDir) is written as a download starts;
# while it runs, <game>/uplay_download/<product>/<build>.state holds a byte a slice (1 = done)
# and the game's files are made full size at once (sparse): size = their length, how far =
# the blocks they really hold. The state file stops changing when it's paused.
UBI_PAUSED_AFTER = 30.0


def _ubisoft(prefix: Path) -> List[StoreDownload]:
    from plunderhub.core.faugus_store import _existing, _system_reg, read_reg
    out = []
    for product, values in read_reg(_system_reg(prefix), r"Software\Wow6432Node\Ubisoft\Launcher\Installs").items():
        if "\\" in product:
            continue
        folder = _existing(prefix, values.get("InstallDir"))
        if folder is None:
            continue
        try:
            states = list((folder / "uplay_download" / product).glob("*.state"))
        except OSError:
            continue
        title = folder.name
        if not states:
            # done: its download folder gone a moment ago
            if time.time() - folder.stat().st_mtime < 120 and (folder / "uplay_install.state").is_file():
                out.append(StoreDownload("ubisoft", product, title, 1.0, 0.0, 0, 0, False, True, str(folder)))
            continue
        total = done = 0
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
        try:
            paused = time.time() - max(p.stat().st_mtime for p in states) > UBI_PAUSED_AFTER
        except OSError:
            paused = False
        frac = min(0.99, done / total) if total else 0.0
        out.append(StoreDownload("ubisoft", product, title, frac, -1.0, total, done, paused, False, str(folder)))
    return out


# Rockstar Games Launcher (11-22): its games' keys (InstallFolder) are written as a download
# starts; its service logs each piece it writes there (faugus_store.rockstar_last_write) and
# the files are full size at once: how far = the blocks they really hold. No pieces for a
# while: paused - or done, once (nearly) all of it is there.
ROCKSTAR_PAUSED_AFTER = 30.0
ROCKSTAR_DONE_SHARE = 0.9


def _rockstar(prefix: Path) -> List[StoreDownload]:
    from plunderhub.core import faugus_store as fs
    out = []
    for key, values in fs.read_reg(fs._system_reg(prefix), r"Software\Wow6432Node\Rockstar Games").items():
        if "\\" in key or key.lower() in ("launcher", "rockstar games social club", "social club"):
            continue
        folder = fs._existing(prefix, values.get("InstallFolder"))
        if folder is None:
            continue
        last = fs.rockstar_last_write(prefix, folder)
        if last is None or time.time() - last > 15 * 60:
            continue        # not written into lately: nothing of it going on
        done, total = fs.disk_share(folder)
        frac = done / total if total else 0.0
        quiet = time.time() - last > ROCKSTAR_PAUSED_AFTER
        installed = fs.rockstar_installed_at(prefix)
        if (installed is not None and installed >= last - 1) or (quiet and frac >= ROCKSTAR_DONE_SHARE):
            if time.time() - max(last, installed or 0) < 150:
                out.append(StoreDownload("rockstar", key.lower(), folder.name or key, 1.0, 0.0, total, total, False, True,
                                         str(folder)))
            continue
        out.append(StoreDownload("rockstar", key.lower(), folder.name or key, min(0.99, frac), -1.0, total, done,
                                 quiet, False,
                                 str(folder)))
    return out


# Amazon Games (11-22, NOT tested live - the user has no Amazon account): its
# Data/Games/Sql/GameInstallInfo.sqlite has a DbSet row per game (Id, ProductTitle,
# InstallDirectory, Installed); Playnite's library reads only "Installed = 1" with an
# existing folder as installed. A row not installed yet whose folder is being written
# into is taken for a download; no size is known, so only how much is there and its
# speed are shown. Installed, its folder written a moment ago: just finished.
AMAZON_ACTIVE = 60.0


def _newest_write(folder: Path) -> float:
    newest = 0.0
    stack = [str(folder)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        newest = max(newest, st.st_mtime)
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return newest


def _amazon(prefix: Path) -> List[StoreDownload]:
    import sqlite3
    from plunderhub.core import faugus_store as fs
    db = fs._follow(prefix, "drive_c/users/steamuser/AppData/Local/Amazon Games/Data/Games/Sql/GameInstallInfo.sqlite"
                    .split("/"))
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = con.execute("SELECT Id, InstallDirectory, ProductTitle, Installed FROM DbSet").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    now = time.time()
    for game_id, directory, title, installed in rows:
        folder = fs._existing(prefix, directory or "")
        if folder is None or not game_id:
            continue
        written = _newest_write(folder)
        title = re.sub(r"[™®©]", "", str(title or folder.name)).strip()
        if installed:
            if now - written < 120:
                done, _total = fs.disk_share(folder)
                out.append(StoreDownload("amazon", str(game_id).lower(), title, 1.0, 0.0, done, done, False, True,
                                         str(folder)))
            continue
        if now - written > 15 * 60:
            continue            # an old leftover, nothing going on
        done, _total = fs.disk_share(folder)
        out.append(StoreDownload("amazon", str(game_id).lower(), title, 0.0, -1.0, 0, done,
                                 now - written > AMAZON_ACTIVE, False, str(folder)))
    return out


# Wargaming Game Center (11-22): a game is in preferences.xml (games_manager/games/game/
# working_dir) from its download's start; its game_info.xml says <installed>false</installed>
# till it's done. Its size it asks its update server for - the same public list (no account,
# no personal data in it) it logs the address of:
#   https://wgus-woteu.wargaming.net/api/v1/patches_chain/?game_id=WOT.EU.PRODUCTION&...
# whose <file>s (name, size) are the packages it downloads into <game>/updates/<name>, full
# size at once: how far = the blocks they really hold; a part's packages gone once it's
# unpacked (game_info.xml part_versions installed = its version_to) count whole.
# Its log says the state: "[Download] state updated: wot_... [DOWNLOADING(2), ...".
_WGC_STATE = re.compile(r"\[Download\] [a-z ]+: \S+ \[([A-Z_]+)\(")
_WGC_PHASE = re.compile(r'Push to buffer "game_part_(download_start|download_finish|install_start|install_finish)_v1"')
_WGC_CHAIN = re.compile(r"https://wgus[^\s\"']*/api/v1/patches_chain/\?[^\s\"']+")
_wgc_chains: Dict[str, tuple] = {}      # chain address -> (when fetched, [(part, version_to, name, size)])
_wgc_seen: Dict[str, int] = {}          # game folder -> its size, while seen downloading


def _wgc_log(center: Path) -> Optional[Path]:
    try:
        return max((center / "logs").glob("wgc_*.log"), key=lambda p: p.stat().st_mtime)
    except (OSError, ValueError):
        return None


def _wgc_chain(log: Optional[Path], game_id: str) -> Optional[list]:
    """The packages of the game's download, from the list its update server
    gave Game Center (fetched once, again after an hour)."""
    import urllib.request
    import xml.etree.ElementTree as ET
    if log is None:
        return None
    urls = [u for u in _WGC_CHAIN.findall(_tail(log)) if f"game_id={game_id}&" in u + "&"]
    if not urls:
        return None
    url = urls[-1].replace("&amp;", "&")
    got = _wgc_chains.get(url)
    if got is not None and time.time() - got[0] < 3600:
        return got[1]
    try:
        from plunderhub.core.utils import HEADERS
        with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=10) as resp:
            root = ET.fromstring(resp.read())
    except Exception:
        _wgc_chains[url] = (time.time() - 3000, None)       # asked again in 10 min
        return None
    files = []
    for patch in root.iterfind(".//patches_chain/patch"):
        part, to = (patch.findtext("part") or "").strip(), (patch.findtext("version_to") or "").strip()
        for f in patch.iterfind("files/file"):
            try:
                files.append((part, to, (f.findtext("name") or "").strip(), int(f.findtext("size") or 0)))
            except ValueError:
                continue
    _wgc_chains[url] = (time.time(), files or None)
    return files or None


_wgc_art: Dict[str, str] = {}


def _wgc_picture(title: str) -> str:
    """Steam's capsule of the game (World of Tanks: the tank on its store
    page) - not its library hero, which shows whatever event runs (The
    Walking Dead, 11-22); Game Center keeps no picture of it on disk (its
    "background" is a bare texture). '' when there's none."""
    if title in _wgc_art:
        return _wgc_art[title]
    picture = ""
    try:
        from plunderhub.constants import CACHE_DIR
        from plunderhub.core import treasure
        from plunderhub.core.protondb import steam_appid_by_title
        appid = steam_appid_by_title(title)
        if appid is None:
            return ""           # Steam didn't answer: asked again next time
        if appid:
            got = treasure._download("https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
                                     f"{appid}/capsule_616x353.jpg", CACHE_DIR / "treasure-map")
            picture = str(got or "")
    except Exception:
        return ""
    _wgc_art[title] = picture
    return picture


def _wargaming(prefix: Path) -> List[StoreDownload]:
    import xml.etree.ElementTree as ET
    from plunderhub.core import faugus_store as fs
    center = fs._follow(prefix, "drive_c/ProgramData/Wargaming.net/GameCenter".split("/"))
    try:
        nodes = list(ET.parse(center / "preferences.xml").getroot().iterfind(".//games_manager/games/game"))
    except (OSError, ET.ParseError):
        return []
    log = _wgc_log(center)
    out = []
    for node in nodes:
        folder = fs._existing(prefix, (node.findtext("working_dir") or "").strip())
        if folder is None:
            continue
        title = re.sub(r"[_ ](EU|NA|RU|ASIA|CN|KR|CT)$", "", folder.name, flags=re.I).replace("_", " ")
        if fs.wargaming_installed(folder):
            if str(folder) in _wgc_seen:        # seen downloading: done now
                size = _wgc_seen.pop(str(folder))
                out.append(StoreDownload("wargaming", folder.name.lower(), title, 1.0, 0.0, size, size, False, True,
                                         str(folder)))
            continue
        updates = folder / "updates"
        try:
            if time.time() - max([updates.stat().st_mtime] + [p.stat().st_mtime for p in updates.iterdir()]) > 15 * 60:
                continue        # not installed, nothing going on (an abandoned download)
        except (OSError, ValueError):
            continue
        try:
            game = ET.parse(folder / "game_info.xml").getroot()
            game_id = (game.findtext(".//game/id") or "").strip()
            have = {v.get("name"): v.get("installed") for v in game.iterfind(".//part_versions/version")}
        except (OSError, ET.ParseError):
            game_id, have = "", {}
        chain = _wgc_chain(log, game_id) if game_id else None
        total = done = 0
        for part, to, name, size in chain or ():
            total += size
            path = updates / name
            try:
                st = path.stat()
                done += min(size, st.st_size, st.st_blocks * 512)
            except OSError:
                if have.get(part) == to:
                    done += size        # unpacked, its packages gone
        _wgc_seen[str(folder)] = total
        frac = min(0.99, done / total) if total else 0.0
        tail = _tail(log) if log is not None else ""
        states = _WGC_STATE.findall(tail)
        paused = bool(states) and "PAUS" in states[-1]
        # its own events say the phase: a part downloaded, then unpacked into the game's folder
        phases = _WGC_PHASE.findall(tail)
        installing = bool(phases) and phases[-1] == "install_start"
        out.append(StoreDownload("wargaming", folder.name.lower(), title, frac, -1.0, total, done, paused, False,
                                 str(folder), installing=installing and not paused, art=_wgc_picture(title)))
    return out


_READERS = {"battle": _battle, "ea": _ea, "epic": _epic, "gog": _gog, "ubisoft": _ubisoft,
            "rockstar": _rockstar, "amazon": _amazon, "wargaming": _wargaming}


def downloads(kind: str, prefix: str) -> List[StoreDownload]:
    """What this launcher in this prefix is downloading (and what it has just finished)."""
    reader = _READERS.get(kind)
    if reader is None:
        return []
    try:
        return reader(Path(prefix))
    except Exception:
        return []
