#!/usr/bin/env python3
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

"""Treasure Map (demo) - every game the player owns, read from the
launchers' own files. Nothing is logged in to, no key, no web API:

  Steam   appcache/packageinfo.vdf (the account's licenses) x
          appcache/appinfo.vdf (what each app is) - rules in
          PlunderHubNotes/steam-owned-games.md; installed = appmanifest_*.acf;
          covers from appcache/librarycache (already on disk)
  Heroic  store_cache/{legendary,gog,nile}_library.json; signed in =
          its runners' user / auth files; covers = the art_square URLs
          (Heroic's own images-cache first, else downloaded once)

Only reads. Every reader fails soft: a format it can't parse gives a
status, never an exception."""

import hashlib
import json
import os
import re
import shutil
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.logging_setup import logger
from plunderhub.core import launcher_art
from plunderhub.core.scan_paths import HEROIC_CONFIG_ROOTS
from plunderhub.core.steam_heroic import steam_libraries, steam_roots

STORES = ("steam", "epic", "gog", "amazon")
STEAM_KINDS = ("game", "demo", "beta", "mod", "application")


@dataclass
class OwnedGame:
    uid: str                    # "steam:620", "epic:<app_name>", ...
    store: str
    title: str
    installed: bool
    cover: Optional[str]        # a file or an http(s) URL
    app_id: str                 # Steam appid / Heroic app_name
    runner: str = ""            # Heroic: legendary, gog, nile
    size: int = 0               # installed bytes when known
    last_played: float = 0.0    # unix time
    playtime: int = 0           # minutes
    released: float = 0.0       # unix time, 0 = not known (yet)
    extra: Dict = field(default_factory=dict)


@dataclass
class StoreState:
    store: str
    status: str                 # ok, no_launcher, signed_out, unreadable
    games: List[OwnedGame] = field(default_factory=list)
    note: str = ""


# --- Steam -----------------------------------------------------------------------

def _kv_text_keys(b: bytes, p: int):
    d = {}
    while True:
        t = b[p]
        p += 1
        if t == 8:
            return d, p
        e = b.index(b"\0", p)
        k = b[p:e].decode("utf-8", "replace")
        p = e + 1
        if t == 0:
            v, p = _kv_text_keys(b, p)
        elif t == 1:
            e = b.index(b"\0", p)
            v = b[p:e].decode("utf-8", "replace")
            p = e + 1
        elif t == 2:
            v = struct.unpack_from("<i", b, p)[0]
            p += 4
        elif t == 7:
            v = struct.unpack_from("<Q", b, p)[0]
            p += 8
        else:
            raise ValueError(f"kv type {t}")
        d[k] = v


def _licensed_apps(root: Path) -> set:
    """Every appid in a package the account holds, without expired free
    weekends (they stay in the file) and without package 0."""
    b = (root / "appcache" / "packageinfo.vdf").read_bytes()
    magic = struct.unpack_from("<I", b, 0)[0]
    if magic & 0xFFFFFF00 != 0x06565500:
        raise ValueError(f"packageinfo magic {magic:#x}")
    apps, p = set(), 8
    while p < len(b):
        sub = struct.unpack_from("<I", b, p)[0]
        if sub == 0xFFFFFFFF:
            break
        d, p = _kv_text_keys(b, p + 4 + 20 + 4 + 8)
        if sub == 0 or not d:
            continue
        info = next(iter(d.values()))
        if not isinstance(info, dict) or (info.get("extended") or {}).get("freeweekend"):
            continue
        apps |= {int(v) for v in (info.get("appids") or {}).values()}
    return apps


def _app_infos(root: Path, wanted: set) -> Dict[int, Dict]:
    """appinfo.vdf v29 (2024 on): names and kinds of the wanted apps."""
    b = (root / "appcache" / "appinfo.vdf").read_bytes()
    magic, _universe, stoff = struct.unpack_from("<IIq", b, 0)
    if magic != 0x07564429:
        raise ValueError(f"appinfo magic {magic:#x}")
    n = struct.unpack_from("<I", b, stoff)[0]
    strs, p = [], stoff + 4
    for _ in range(n):
        e = b.index(b"\0", p)
        strs.append(b[p:e].decode("utf-8", "replace"))
        p = e + 1

    def kv(p):
        d = {}
        while True:
            t = b[p]
            p += 1
            if t == 8:
                return d, p
            k = strs[struct.unpack_from("<I", b, p)[0]]
            p += 4
            if t == 0:
                v, p = kv(p)
            elif t == 1:
                e = b.index(b"\0", p)
                v = b[p:e].decode("utf-8", "replace")
                p = e + 1
            elif t == 2:
                v = struct.unpack_from("<i", b, p)[0]
                p += 4
            elif t == 7:
                v = struct.unpack_from("<Q", b, p)[0]
                p += 8
            else:
                raise ValueError(f"kv type {t}")
            d[k] = v
    out, p = {}, 16
    while p < stoff:
        appid, size = struct.unpack_from("<II", b, p)
        if appid == 0:
            break
        body = p + 8
        if appid in wanted:
            d, _ = kv(body + 4 + 4 + 8 + 20 + 4 + 20)
            out[appid] = d.get("appinfo", d)
        p = body + size
    return out


def _steam_installed() -> Dict[int, Tuple[str, int]]:
    """appid -> (name, bytes) of every fully installed game."""
    found = {}
    for lib in steam_libraries():
        for acf in (Path(lib) / "steamapps").glob("appmanifest_*.acf"):
            try:
                text = acf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            m = re.search(r'"appid"\s+"(\d+)"', text)
            if not m:
                continue
            flags = re.search(r'"StateFlags"\s+"(\d+)"', text)
            if flags and not int(flags.group(1)) & 4:
                continue
            name = re.search(r'"name"\s+"([^"]*)"', text)
            size = re.search(r'"SizeOnDisk"\s+"(\d+)"', text)
            found[int(m.group(1))] = (name.group(1) if name else m.group(1), int(size.group(1)) if size else 0)
    return found


def _steam_signed_in_root(roots: List[Path]) -> Optional[Path]:
    """The Steam install someone is signed in to - not merely the first one
    found: ~/.local/share/Steam can hold only the Protons PlunderHub put
    there for Lutris and Bottles while Steam itself lives elsewhere (Debian's
    and Ubuntu's package: ~/.steam/debian-installation). Several: the one
    signed in to last."""
    best, best_t = None, -1.0
    for root in roots:
        users = root / "config" / "loginusers.vdf"
        try:
            t = users.stat().st_mtime
        except OSError:
            continue
        if t > best_t:
            best, best_t = root, t
    return best


def steam_state() -> StoreState:
    roots = steam_roots()
    if not roots:
        return StoreState("steam", "no_launcher")
    root = _steam_signed_in_root(roots)
    if root is None:
        return StoreState("steam", "signed_out")
    installed = _steam_installed()
    try:
        apps = _licensed_apps(root)
        infos = _app_infos(root, apps | set(installed))
    except Exception as e:      # an unknown format: the installed ones only, and say so
        games = [OwnedGame(f"steam:{a}", "steam", n, True, launcher_art._steam(str(a)).get("cover"), str(a), size=s)
                 for a, (n, s) in installed.items()]
        return StoreState("steam", "unreadable", games, note=str(e))
    played = _steam_played(root)
    _STEAM_INFO.clear()
    _STEAM_INFO.update(infos)
    games = []
    for appid, info in infos.items():
        common = info.get("common") or {}
        kind = str(common.get("type", "")).lower()
        if kind not in STEAM_KINDS or appid not in apps and appid not in installed:
            continue
        if (info.get("extended") or {}).get("VisibleOnlyWhenInstalled") and appid not in installed:
            continue
        title = common.get("name") or installed.get(appid, (str(appid), 0))[0]
        last, minutes = played.get(appid, (0, 0))
        try:
            released = float(common.get("steam_release_date") or common.get("original_release_date") or 0)
        except (TypeError, ValueError):
            released = 0.0
        games.append(OwnedGame(f"steam:{appid}", "steam", title, appid in installed,
                               None, str(appid), size=installed.get(appid, ("", 0))[1],
                               last_played=last, playtime=minutes, released=released, extra={"kind": kind}))
    return StoreState("steam", "ok", games)


_STEAM_INFO: Dict[int, Dict] = {}
_HEROIC_ITEMS: Dict[str, Dict] = {}
_STEAM_INSTALL_INDEX = ["0"]


def _steam_default_library() -> Optional[Path]:
    """Where Steam's install window points first: the library it
    installed into last (LastInstallFolderIndex in the user's localconfig)."""
    from plunderhub.core.steam_heroic import parse_vdf_text
    for root in steam_roots():
        try:
            data = parse_vdf_text((root / "config" / "libraryfolders.vdf").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        folders = data.get("libraryfolders", data)
        entry = folders.get(_STEAM_INSTALL_INDEX[0]) or folders.get("0")
        if isinstance(entry, dict) and entry.get("path"):
            return Path(entry["path"])
    return None


def _steam_played(root: Path) -> Dict[int, Tuple[float, int]]:
    """appid -> (last played, minutes) of the most recent Steam user."""
    from plunderhub.core.steam_heroic import _vdf_get_ci, parse_vdf_text
    out = {}
    users = sorted((root / "userdata").glob("*/config/localconfig.vdf"), key=lambda p: p.stat().st_mtime)
    if not users:
        return out
    try:
        data = parse_vdf_text(users[-1].read_text(encoding="utf-8", errors="replace"))
        apps = (_vdf_get_ci(data, "Software", "Valve", "Steam", "apps")
                or _vdf_get_ci(data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps") or {})
        _STEAM_INSTALL_INDEX[0] = str(data.get("LastInstallFolderIndex") or "0")
    except Exception:
        return out
    for appid, v in apps.items():
        if isinstance(v, dict) and str(appid).isdigit():
            try:
                out[int(appid)] = (float(v.get("LastPlayed") or 0), int(v.get("Playtime") or 0))
            except ValueError:
                pass
    return out


def steam_cover(game: OwnedGame) -> Optional[str]:
    return launcher_art._steam(game.app_id).get("cover")


# --- Heroic ----------------------------------------------------------------------

def heroic_root() -> Optional[Path]:
    """The Heroic in use: a config folder Heroic itself has written (its
    config.json, its library caches) - the one used last when there are
    two (native and Flatpak). A folder with only tools/ in it is PlunderHub's
    own Proton for a Heroic that isn't installed (found on Bazzite: it hid
    the Flatpak Heroic's sign-in)."""
    found = []
    for base in HEROIC_CONFIG_ROOTS:
        root = base / "heroic"
        stamps = []
        for rel in ("config.json", "store_cache", "legendaryConfig", "gog_store", "nile_config", "nile_store"):
            try:
                stamps.append((root / rel).stat().st_mtime)
            except OSError:
                pass
        if stamps:
            found.append((max(stamps), root))
    if found:
        return max(found, key=lambda t: t[0])[1]
    # Heroic hasn't run yet: the folder of the Heroic that is installed
    # (both folders may hold only PlunderHub's Proton - seen on Ubuntu with
    # the Flatpak Heroic: the native folder came first)
    from plunderhub.core.scan_paths import LAUNCHER_FLATPAK_IDS, flatpak_app_installed
    if not shutil.which("heroic"):
        for app_id in LAUNCHER_FLATPAK_IDS["Heroic"]:
            if flatpak_app_installed(app_id):
                return Path.home() / ".var" / "app" / app_id / "config" / "heroic"
    for base in HEROIC_CONFIG_ROOTS:
        if (base / "heroic").is_dir():
            return base / "heroic"
    return None


def heroic_present() -> bool:
    return heroic_root() is not None or bool(shutil.which("heroic"))


_HEROIC = {"epic": ("legendary_library.json", "library", "legendary",
                    ("legendaryConfig/legendary/user.json",)),
           "gog": ("gog_library.json", "games", "gog", ("gog_store/auth.json",)),
           "amazon": ("nile_library.json", "library", "nile",
                      # nile writes current_user.json at sign-in (its
                      # tokens go to an encrypted file named by a hash);
                      # user.json is older nile's, migrated away on start
                      ("nile_config/nile/current_user.json", "nile_config/nile/user.json"))}


def heroic_state(store: str) -> StoreState:
    root = heroic_root()
    if root is None:
        return StoreState(store, "no_launcher")
    fname, key, runner, auth_files = _HEROIC[store]
    if not any(_non_empty(root / a) for a in auth_files):
        return StoreState(store, "signed_out")
    try:
        data = json.loads((root / "store_cache" / fname).read_text(encoding="utf-8"))
        items = data.get(key) or []
    except (OSError, ValueError, AttributeError):
        items = []
    if not [i for i in items if isinstance(i, dict) and i.get("app_name") != "gog-redist"]:
        # signed in, but Heroic hasn't written its list yet (GOG: only after
        # fetching every game's details, minutes): the runners' own lists
        items = _signed_in_library(root, store)
    played = {}
    try:
        stamps = json.loads((root / "store" / "timestamp.json").read_text(encoding="utf-8"))
        from datetime import datetime
        for k, v in stamps.items():
            when = v.get("lastPlayed") or ""
            ts = datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp() if when else 0
            played[k] = (ts, int(v.get("totalPlayed") or 0))
    except (OSError, ValueError, AttributeError):
        pass
    real = _heroic_installed(root, store)
    games, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name, title = str(item.get("app_name") or ""), str(item.get("title") or "")
        install = item.get("install") or {}
        if not name or not title or name in seen or item.get("is_dlc") or install.get("is_dlc"):
            continue
        if name == "gog-redist":
            continue
        seen.add(name)
        cover = next((item.get(k) for k in ("art_square", "art_cover") if isinstance(item.get(k), str)
                      and item.get(k).startswith("http")), None)
        # the runner's own installed list decides: Heroic's library cache keeps
        # is_installed False for GOG games it installed (seen 11-06)
        installed = name in real if real is not None else bool(item.get("is_installed"))
        size = _parse_size((real or {}).get(name, {}).get("install_size") or install.get("install_size")) \
            if installed else 0
        _HEROIC_ITEMS[f"{store}:{name}"] = item
        last, minutes = played.get(name, (0, 0))
        released = _epic_released(root, name) if store == "epic" else 0.0
        games.append(OwnedGame(f"{store}:{name}", store, title, installed, cover, name,
                               runner=runner, size=size, last_played=last, playtime=minutes, released=released))
    return StoreState(store, "ok", games)


_INSTALLED_FILES = {"epic": ("legendaryConfig/legendary/installed.json",),
                    "gog": ("gog_store/installed.json",),
                    "amazon": ("nile_config/nile/installed.json", "nile_store/installed.json")}


def _heroic_installed(root: Path, store: str) -> Optional[Dict[str, Dict]]:
    """app_name -> its entry in the runner's installed list (legendary's
    {app: {...}}, gogdl's {"installed": [{appName...}]}, nile's [{id...}]);
    None when there's no such file to go by."""
    for rel in _INSTALLED_FILES[store]:
        try:
            data = json.loads((root / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("installed"), list):
            data = data["installed"]
        if isinstance(data, list):
            out = {}
            for e in data:
                if isinstance(e, dict) and not e.get("is_dlc"):
                    key = str(e.get("appName") or e.get("app_name") or e.get("id") or "")
                    if key:
                        out[key] = e
            return out
        if isinstance(data, dict):
            return {str(k): (v if isinstance(v, dict) else {}) for k, v in data.items()}
    return None


def _epic_released(root: Path, app_name: str) -> float:
    """When the game came to the Epic store: its catalog item's
    creationDate in legendary's metadata (Epic gives no release date)."""
    from datetime import datetime
    try:
        meta = json.loads((root / "legendaryConfig" / "legendary" / "metadata" / f"{app_name}.json")
                          .read_text(encoding="utf-8")).get("metadata") or {}
        when = meta.get("creationDate") or ""
        return datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp() if when else 0.0
    except (OSError, ValueError, AttributeError):
        return 0.0


def gog_release_dates(ids: List[str], cache_dir: Path) -> Dict[str, float]:
    """GOG's release dates, 50 games per request to its public API
    (no sign-in), kept a week."""
    from datetime import datetime
    out: Dict[str, float] = {}
    ids = sorted(i for i in ids if i.isdigit())
    for n in range(0, len(ids), 50):
        part = ids[n:n + 50]
        data = _get_json("https://api.gog.com/products?ids=" + ",".join(part), cache_dir)
        for item in data if isinstance(data, list) else []:
            when = item.get("release_date") or ""
            try:
                out[str(item.get("id"))] = datetime.fromisoformat(when).timestamp() if when else 0.0
            except ValueError:
                pass
    return out


def _non_empty(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return text not in ("", "{}", "[]", "null")


# --- right after a sign-in: the games before Heroic lists them -----------------------
# Heroic writes store_cache/<store>_library.json only after its refresh ends -
# for GOG after asking for every game's details one by one (minutes; seen on
# Bazzite). Meanwhile the same games come from what the runners keep
# themselves (Epic: legendary's metadata, Amazon: nile's library.json - read
# the way Heroic reads them) or, for GOG, from GOG's own list (fetched in the
# background with the token Heroic's gogdl hands out, cached per account).

OWN_CACHE: List[Optional[Path]] = [None]          # set by the Treasure Map (its cache folder)


def _signed_in_library(root: Path, store: str) -> List[Dict]:
    out: List[Dict] = []
    if store == "epic":
        for f in sorted((root / "legendaryConfig" / "legendary" / "metadata").glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            m = d.get("metadata") or {}
            cats = {c.get("path") for c in m.get("categories") or [] if isinstance(c, dict)}
            if m.get("mainGameItem") or "addons" in cats or not d.get("app_name"):
                continue
            art = {k.get("type"): k.get("url") for k in m.get("keyImages") or [] if isinstance(k, dict)}
            out.append({"app_name": d["app_name"], "title": d.get("app_title") or m.get("title") or d["app_name"],
                        "art_cover": art.get("DieselGameBoxTall") or art.get("DieselGameBox"),
                        "art_background": art.get("DieselGameBox"), "developer": m.get("developer") or "",
                        "extra": {"about": {"description": m.get("description") or ""}}})
    elif store == "amazon":
        for rel in ("nile_config/nile/library.json", "nile_store/library.json"):
            try:
                lib = json.loads((root / rel).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for g in lib if isinstance(lib, list) else []:
                product = (g or {}).get("product") or {}
                detail = product.get("productDetail") or {}
                details = detail.get("details") or {}
                if not product.get("id"):
                    continue
                out.append({"app_name": product["id"], "title": product.get("title") or "???",
                            "art_cover": details.get("backgroundUrl2") or detail.get("iconUrl"),
                            "art_background": details.get("backgroundUrl1") or details.get("backgroundUrl2"),
                            "developer": details.get("developer") or "",
                            "extra": {"about": {"description": details.get("shortDescription") or ""}}})
            break
    elif store == "gog":
        cache = gog_owned_cache()
        user = _gog_user(root)
        if cache is not None and user:
            try:
                d = json.loads(cache.read_text(encoding="utf-8"))
                if d.get("user") == user:
                    out = [g for g in d.get("games") or [] if isinstance(g, dict) and g.get("app_name")]
            except (OSError, ValueError):
                pass
    return out


def _gog_user(root: Path) -> str:
    try:
        auth = json.loads((root / "gog_store" / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    for v in (auth.values() if isinstance(auth, dict) else []):
        if isinstance(v, dict) and v.get("user_id"):
            return str(v["user_id"])
    return ""


def gog_owned_cache() -> Optional[Path]:
    return OWN_CACHE[0] / "gog_owned.json" if OWN_CACHE[0] else None


def gog_needs_owned() -> bool:
    """Signed in to GOG, Heroic's list still empty, ours not fetched for
    this account yet."""
    root = heroic_root()
    if root is None or gog_owned_cache() is None or not _non_empty(root / "gog_store" / "auth.json"):
        return False
    try:
        lib = json.loads((root / "store_cache" / "gog_library.json").read_text(encoding="utf-8"))
        if [g for g in lib.get("games") or [] if g.get("app_name") != "gog-redist"]:
            return False
    except (OSError, ValueError, AttributeError):
        pass
    try:
        return json.loads(gog_owned_cache().read_text(encoding="utf-8")).get("user") != _gog_user(root)
    except (OSError, ValueError, AttributeError):
        return True


def gog_fetch_owned() -> bool:
    """GOG's own list of the account's games (titles from its product API,
    games only), written to the cache. Blocks (seconds): a worker thread."""
    import subprocess
    import urllib.request
    root = heroic_root()
    cmd, env = _tool_cmd("gog")
    cache = gog_owned_cache()
    if root is None or cmd is None or cache is None:
        return False
    try:
        res = subprocess.run(cmd + ["auth"], env=env, capture_output=True, text=True, timeout=90)
        token = json.loads(res.stdout.strip().splitlines()[-1]).get("access_token")
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        return False
    if not token:
        return False
    ua = {"User-Agent": "PlunderHub"}
    try:
        req = urllib.request.Request("https://embed.gog.com/user/data/games",
                                     headers={**ua, "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=20) as r:
            owned = [str(i) for i in json.load(r).get("owned") or []]
    except (OSError, ValueError):
        return False
    games = []
    for i in range(0, len(owned), 50):
        try:
            req = urllib.request.Request("https://api.gog.com/products?ids=" + ",".join(owned[i:i + 50]), headers=ua)
            with urllib.request.urlopen(req, timeout=20) as r:
                batch = json.load(r)
        except (OSError, ValueError):
            continue
        for d in batch if isinstance(batch, list) else []:
            title = str(d.get("title") or "")
            # "Goodies" packs are listed as games but hold nothing to install
            if d.get("game_type") != "game" or not title or re.search(r"goodie", title, re.I):
                continue
            bg = str((d.get("images") or {}).get("background") or "")
            games.append({"app_name": str(d.get("id")), "title": title,
                          "art_background": ("https:" + bg) if bg.startswith("//") else bg or None})
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(json.dumps({"user": _gog_user(root), "games": games}), encoding="utf-8")
    os.replace(tmp, cache)
    return True


def _gog_cover_url(app_id: str, cache_dir: Path) -> Optional[str]:
    """A GOG game's portrait cover, where Heroic takes it from (GOG's gamesdb)."""
    d = _get_json(f"https://gamesdb.gog.com/platforms/gog/external_releases/{app_id}", cache_dir, 30 * 86400)
    fmt = (((d or {}).get("game") or {}).get("vertical_cover") or {}).get("url_format")
    return fmt.replace("{formatter}", "").replace("{ext}", "jpg") if fmt else None


def all_states() -> Dict[str, StoreState]:
    out = {"steam": steam_state()}
    for store in ("epic", "gog", "amazon"):
        out[store] = heroic_state(store)
    out["faugus"] = faugus_state()
    return out


def faugus_state() -> StoreState:
    """Faugus' games (all aboard: Faugus lists only what's on disk) -
    status no_launcher / ok."""
    from plunderhub.core import faugus_add as fa
    try:
        status, site = fa.faugus_status()
    except Exception as e:
        logger.info(f"Treasure Map: Faugus not read: {e}")
        return StoreState("faugus", "unreadable")
    if status != "ok" or site is None:
        return StoreState("faugus", status)
    games = []
    from plunderhub.core.faugus_store import known as store_games
    launcher_games = store_games()
    for g in fa.read_games(site):
        gid, title = str(g.get("gameid") or ""), str(g.get("title") or "")
        if not gid or not title or g.get("hidden") or g.get("runner") == "Steam":
            continue
        cover = str(g.get("cover") or "")
        if not cover or not Path(os.path.expanduser(cover)).is_file():
            own = site.data / "covers" / f"{gid}.png"
            cover = str(own) if own.is_file() else ""
        secs = g.get("playtime")
        last = g.get("last_played")
        games.append(OwnedGame(f"faugus:{gid}", "faugus", title, True, cover or None, gid,
                               playtime=int(secs) // 60 if isinstance(secs, (int, float)) else 0,
                               last_played=float(last) if isinstance(last, (int, float)) else 0.0,
                               extra={"store_icon": str(site.data / "icons" / f"{launcher_games[gid].get('launcher')}.png")
                                      if gid in launcher_games else None,
                                      "launcher": None if gid in launcher_games else fa.launcher_kind(str(g.get("path") or "")),
                                      "prefix": os.path.expanduser(str(g.get("prefix") or ""))}))
    return StoreState("faugus", "ok", games)


# --- covers ----------------------------------------------------------------------

def cover_file(game: OwnedGame, cache_dir: Path) -> Optional[Path]:
    """A local file with the game's cover: Steam's own cache, Heroic's
    own images-cache, or ours (downloaded once, small)."""
    if game.store == "faugus":                    # Faugus' own covers folder, or the launcher's drawn art
        return Path(game.cover) if game.cover and Path(game.cover).is_file() else None
    if game.store == "steam":
        path = game.cover or steam_cover(game)
        if path:
            return Path(path)
        # Steam hasn't drawn this one's library art yet: its CDN, once
        for name in ("library_600x900_2x.jpg", "library_600x900.jpg", "header.jpg"):
            got = _download(f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
                            f"{game.app_id}/{name}", cache_dir)
            if got is not None:
                return got
        return None
    url = game.cover
    if not url and game.store == "gog" and game.app_id.isdigit():
        url = _gog_cover_url(game.app_id, cache_dir)       # listed before Heroic did
    if not url:
        return None
    root = heroic_root()
    if root is not None:
        theirs = root / "images-cache" / hashlib.sha256(url.encode()).hexdigest()
        if theirs.is_file():
            return theirs
    return _download(url, cache_dir)


def _download(url: str, cache_dir: Path) -> Optional[Path]:
    ours = cache_dir / hashlib.sha256(url.encode()).hexdigest()
    if ours.is_file():
        return ours if ours.stat().st_size else None
    import urllib.request
    fetch = url
    from urllib.parse import urlsplit
    if (urlsplit(url).hostname or "") in ("cdn1.epicgames.com", "cdn2.unrealengine.com"):   # Epic's resizing CDN
        fetch = url + ("&" if "?" in url else "?") + "h=480&quality=medium&resize=1&w=360"
    req = urllib.request.Request(fetch, headers={"User-Agent": "PlunderHub"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read(8 << 20)
    except Exception:
        cache_dir.mkdir(parents=True, exist_ok=True)
        ours.write_bytes(b"")           # not there: don't ask again
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = ours.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, ours)
    return ours


# --- the Dig up card: what the game is, what it needs, what there is --------------

def _steam_size(info: Dict) -> int:
    """The download as Steam's install window counts it, about: the
    Windows (or no-OS) depots, English or no language, no DLC depots."""
    total = 0
    for key, depot in (info.get("depots") or {}).items():
        if not str(key).isdigit() or not isinstance(depot, dict) or depot.get("dlcappid") \
                or depot.get("sharedinstall"):
            continue
        cfg = depot.get("config") or {}
        oslist = str(cfg.get("oslist", ""))
        if oslist and "windows" not in oslist:
            continue
        if cfg.get("language") not in (None, "", "english") or cfg.get("lowviolence"):
            continue
        public = (depot.get("manifests") or {}).get("public")
        size = public.get("size") if isinstance(public, dict) else depot.get("maxsize")
        try:
            total += int(size or 0)
        except (TypeError, ValueError):
            pass
    return total


def _strip_html(text: str) -> str:
    import html
    text = re.sub(r"<br\s*/?>|</p>|</h\d>|</li>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def _get_json(url: str, cache_dir: Path, max_age: float = 7 * 86400):
    path = cache_dir / ("api-" + hashlib.sha256(url.encode()).hexdigest() + ".json")
    try:
        if time.time() - path.stat().st_mtime < max_age:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PlunderHub"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read(4 << 20).decode("utf-8", "replace"))
    except Exception:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _free_space(paths: List[Path]) -> Tuple[int, int, str]:
    """(free, total, where) of the roomiest of paths."""
    best = (0, 0, "")
    for p in paths:
        p = Path(p)
        while not p.exists() and p != p.parent:
            p = p.parent
        try:
            st = os.statvfs(p)
        except OSError:
            continue
        free, total = st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
        if free > best[0]:
            best = (free, total, str(p).replace(str(Path.home()), "~", 1))
    return best


def details(game: OwnedGame, cache_dir: Path, slow: bool = True) -> Dict:
    """Everything the Dig up card shows; slow parts (web) included - run
    it off the main thread. Missing pieces are simply left out. Without
    slow, Heroic's tools aren't asked for the sizes (seconds each): the
    card's art and words come first, its numbers after."""
    out: Dict = {}
    if game.store == "steam":
        info = _STEAM_INFO.get(int(game.app_id)) or {}
        common, ext = info.get("common") or {}, info.get("extended") or {}
        out["developer"] = ext.get("developer") or ""
        try:
            out["year"] = time.gmtime(int(common.get("steam_release_date") or common.get("original_release_date"))).tm_year
        except (TypeError, ValueError):
            pass
        if common.get("review_percentage"):
            out["review"] = int(common["review_percentage"])
        out["size"] = _steam_size(info)
        art = launcher_art._steam(game.app_id)
        out["hero"] = art.get("hero") or _download(
            f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{game.app_id}/library_hero.jpg", cache_dir)
        api = _get_json(f"https://store.steampowered.com/api/appdetails?appids={game.app_id}&l=english&filters=basic",
                        cache_dir)
        data = ((api or {}).get(game.app_id) or {}).get("data") or {}
        out["description"] = _strip_html(data.get("short_description") or "")
        lib = _steam_default_library()
        out["free"], out["disk"], out["where"] = _free_space([lib] if lib else [Path(p) for p in steam_libraries()])
    else:
        item = _HEROIC_ITEMS.get(game.uid) or {}
        about = ((item.get("extra") or {}).get("about") or {})
        desc = _strip_html(about.get("shortDescription") or about.get("description") or "")
        out["description"] = "" if desc.strip().lower() == game.title.strip().lower() else desc
        out["developer"] = item.get("developer") or ""
        hero = item.get("art_background") or item.get("art_cover")
        if isinstance(hero, str) and hero.startswith("http"):
            out["hero"] = _download(hero, cache_dir)
        if game.store == "gog" and game.app_id.isdigit():
            api = _get_json(f"https://api.gog.com/products/{game.app_id}?expand=downloads,description", cache_dir)
            if api:
                installers = ((api.get("downloads") or {}).get("installers") or [])
                win = [i for i in installers if i.get("os") == "windows" and i.get("language") in ("en", None)] \
                    or [i for i in installers if i.get("os") == "linux"]
                if win:
                    out["size"] = int(win[0].get("total_size") or 0)
                if not out["description"]:
                    out["description"] = _strip_html(((api.get("description") or {}).get("lead")) or "")
                rel = str(api.get("release_date") or "")[:4]
                if rel.isdigit():
                    out["year"] = int(rel)
        if slow and not game.installed and game.store in ("epic", "gog"):
            tool_info = heroic_install_info(game.store, game.app_id)
            if tool_info.get("disk_size"):
                out["size"] = tool_info["disk_size"]
                out["download"] = tool_info.get("download_size") or 0
        root = heroic_root()
        target = Path.home() / "Games" / "Heroic"
        try:
            cfg = json.loads((root / "config.json").read_text(encoding="utf-8"))
            target = Path((cfg.get("defaultSettings") or {}).get("defaultInstallPath") or target)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        out["free"], out["disk"], out["where"] = _free_space([target])
    if out.get("description") and len(out["description"]) > 330:
        cut = out["description"][:330]
        out["description"] = cut[:cut.rfind(" ")].rstrip(",.;:") + "…"
    return out


if __name__ == "__main__":
    t = time.monotonic()
    for store, st in all_states().items():
        print(f"{store:7} {st.status:11} {len(st.games):4} games, {sum(g.installed for g in st.games)} installed {st.note}")
    print(f"{time.monotonic() - t:.2f} s")


# --- following a real install / uninstall ------------------------------------------

def open_uri(uri: str) -> bool:
    """Hands a steam:// or heroic:// link to whatever handles it on this
    desktop (native or Flatpak launcher alike) - GIO first, xdg-open else."""
    try:
        from gi.repository import Gio
        Gio.AppInfo.launch_default_for_uri(uri, None)
        return True
    except Exception:
        import subprocess
        try:
            subprocess.Popen(["xdg-open", uri], start_new_session=True)
            return True
        except OSError:
            return False


def steam_manifest(appid: str) -> Optional[Dict[str, int]]:
    """The game's appmanifest numbers in whichever library it is going to,
    None while there is none (not started, or uninstalled)."""
    for lib in steam_libraries():
        acf = Path(lib) / "steamapps" / f"appmanifest_{appid}.acf"
        try:
            text = acf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out = {}
        for key in ("StateFlags", "BytesToDownload", "BytesDownloaded", "BytesToStage", "BytesStaged", "SizeOnDisk"):
            m = re.search(r'"%s"\s+"(\d+)"' % key, text)
            out[key] = int(m.group(1)) if m else 0
        out["lib"] = str(lib)
        m = re.search(r'"installdir"\s+"([^"]*)"', text)
        out["installdir"] = m.group(1) if m else ""
        return out
    return None


# --- Steam without its windows -----------------------------------------------------
# Verified 11-07 (LIMBO): with Steam closed, a minimal appmanifest in a library
# with StateFlags 1026 (update required + started) makes Steam download and
# install the game by itself when it starts (in the tray, -silent). Removing
# the game's folder and its manifest while Steam is closed uninstalls it -
# Steam forgets it at its next start (libraryfolders.vdf too).

def _mount_label(path: Path) -> str:
    """Where a library lives, as Steam's install window names it: its
    drive's mount point (/home, /mnt/games...)."""
    p = path
    try:
        p = p.resolve()
        while not os.path.ismount(p) and p != p.parent:
            p = p.parent
    except OSError:
        return str(path)
    return str(p)


def steam_library_choices() -> List[Dict]:
    """Every Steam library, in Steam's order: {path, label, free, default} -
    default is the one Steam's install window picks first (its star)."""
    from plunderhub.core.steam_heroic import steam_library_paths
    out, seen = [], set()
    for root in steam_roots():
        _steam_played(root)                 # reads LastInstallFolderIndex
        for i, lib in enumerate(steam_library_paths(root)):
            try:
                real = lib.resolve()
            except OSError:
                real = lib
            if real in seen or not lib.is_dir():
                continue
            seen.add(real)
            try:
                st = os.statvfs(lib)
                free = st.f_bavail * st.f_frsize
            except OSError:
                free = 0
            out.append({"path": str(lib), "label": _mount_label(lib), "free": free,
                        "default": str(i) == _STEAM_INSTALL_INDEX[0]})
    if out and not any(c["default"] for c in out):
        out[0]["default"] = True
    return out


def _steam_owner() -> str:
    """The signed-in account's SteamID64: loginusers.vdf's MostRecent, else
    the one signed in last (a single account has no MostRecent)."""
    best, best_t = "0", -1
    for root in steam_roots():
        try:
            text = (root / "config" / "loginusers.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'"(\d{17})"\s*\{([^{}]*)\}', text):
            if re.search(r'"mostrecent"\s*"1"', m.group(2), re.I):
                return m.group(1)
            t = re.search(r'"timestamp"\s*"(\d+)"', m.group(2))
            t = int(t.group(1)) if t else 0
            if t > best_t:
                best, best_t = m.group(1), t
    return best


def steam_queue_install(appid: str, library: str) -> Tuple[bool, str]:
    """Writes the manifest that has Steam install appid into library at
    its next start. Steam must be closed (it writes its own manifests over
    ours while it runs)."""
    if steam_manifest(appid) is not None:
        return False, "already has a manifest"
    info = _STEAM_INFO.get(int(appid)) or {}
    installdir = str((info.get("config") or {}).get("installdir") or "").strip()
    name = str((info.get("common") or {}).get("name") or appid)
    if not installdir or "/" in installdir or installdir in (".", ".."):
        return False, "no install folder in Steam's app info"
    steamapps = Path(library) / "steamapps"
    if not steamapps.is_dir():
        return False, f"{steamapps} not found"

    def q(v):
        return str(v).replace("\\", "").replace('"', "")
    fields = [("appid", appid), ("Universe", "1"), ("name", q(name)), ("StateFlags", "1026"),
              ("installdir", q(installdir)), ("LastUpdated", "0"), ("SizeOnDisk", "0"), ("buildid", "0"),
              ("LastOwner", _steam_owner()), ("BytesToDownload", "0"), ("BytesDownloaded", "0"),
              ("AutoUpdateBehavior", "0"), ("AllowOtherDownloadsWhileRunning", "0"), ("ScheduledAutoUpdate", "0")]
    text = '"AppState"\n{\n' + "".join(f'\t"{k}"\t\t"{v}"\n' for k, v in fields) + "}\n"
    path = steamapps / f"appmanifest_{appid}.acf"
    try:
        tmp = path.with_suffix(".acf.plunderhub-tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        return False, str(e)
    return True, ""


def steam_remove_files(appid: str) -> Tuple[bool, str]:
    """Uninstalls appid the way Steam leaves it: its folder, manifest,
    download leftovers, shader cache and workshop items. Its Proton prefix
    (compatdata - the saves of many games) stays: Bilge cleaning finds it
    later. Steam must be closed. Blocks: a worker thread."""
    if not str(appid).isdigit():
        return False, f"not a Steam app id: {appid!r}"
    for lib in steam_libraries():
        steamapps = Path(lib) / "steamapps"
        acf = steamapps / f"appmanifest_{appid}.acf"
        if not acf.is_file():
            continue
        m = re.search(r'"installdir"\s+"([^"]*)"', acf.read_text(encoding="utf-8", errors="replace"))
        installdir = m.group(1) if m else ""
        if installdir and "/" not in installdir and installdir not in (".", ".."):
            folder = steamapps / "common" / installdir
            if folder.is_dir() and not folder.is_symlink():
                shutil.rmtree(folder, ignore_errors=True)
                if folder.exists():
                    return False, f"couldn't remove {folder}"
        for extra in (steamapps / "downloading" / appid, steamapps / "temp" / appid,
                      steamapps / "shadercache" / appid, steamapps / "workshop" / "content" / appid,
                      steamapps / "workshop" / "downloads" / appid):
            if extra.is_dir() and not extra.is_symlink():
                shutil.rmtree(extra, ignore_errors=True)
        for f in (steamapps / "workshop" / f"appworkshop_{appid}.acf", acf):
            try:
                f.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                return False, str(e)
        return True, ""
    return True, ""                      # no manifest anywhere: not installed


def _parse_size(text: str) -> int:
    if isinstance(text, (int, float)):
        return int(text)                      # legendary's installed.json: bytes
    m = re.match(r"\s*([\d.]+)\s*([KMGT]?)i?B", str(text or ""))
    if not m:
        return 0
    return int(float(m.group(1)) * 1024 ** ("KMGT".index(m.group(2)) + 1 if m.group(2) else 0))


def heroic_download(app_name: str) -> Optional[Dict]:
    """Where Heroic's download manager has the game: {"where": "queue" |
    "finished", "status", "target", "size", "added"} - None if it has
    never seen it."""
    root = heroic_root()
    if root is None:
        return None
    try:
        data = json.loads((root / "store" / "download-manager.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for where in ("queue", "finished"):
        for item in reversed(data.get(where) or []):
            params = item.get("params") or {}
            if params.get("appName") != app_name:
                continue
            info = params.get("gameInfo") or {}
            folder = info.get("folder_name") or ""
            target = Path(params.get("path") or "") / folder if folder else None
            return {"where": where, "status": item.get("status") or "", "target": target,
                    "size": _parse_size(params.get("size")), "added": float(item.get("addToQueueTime") or 0) / 1000}
    return None


def folder_bytes(path: Optional[Path], limit_s: float = 1.5) -> int:
    """How much is in a folder so far (a download growing) - stops after
    limit_s on a huge one and returns what it counted."""
    if path is None:
        return 0
    total, start, stack = 0, time.monotonic(), [str(path)]
    while stack:
        if time.monotonic() - start > limit_s:
            break
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_blocks * 512
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def steam_phase(appid: str, since: float) -> Optional[Dict]:
    """What Steam is doing for the game right now, from its own
    logs/content_log.txt (the manifest's byte counts stay 0 during a
    download): {"phase": "shaders" | "game" | "commit", "download": total
    bytes, "stage": total bytes, "folder": where the bytes land} - None
    before Steam started anything for it after `since` (unix time)."""
    from datetime import datetime
    for root in steam_roots():
        log = root / "logs" / "content_log.txt"
        try:
            with open(log, "rb") as fh:
                fh.seek(0, 2)
                fh.seek(max(0, fh.tell() - 256 * 1024))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        out = None
        tag = f"AppID {appid} "
        lib = None
        for line in lines:
            if tag not in line:
                continue
            m = re.match(r"\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]", line)
            try:
                when = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp() if m else 0
            except ValueError:
                when = 0
            if when < since - 5:
                continue
            kind = re.search(r"(Shader|App|Workshop) update changed : (.*)", line)
            if kind:
                state = kind.group(2)
                if "Running Update" in state:
                    out = out or {}
                    out["phase"] = "commit" if "Committing" in state else \
                        ("shaders" if kind.group(1) == "Shader" else "game")
                elif out is not None and state.strip() == "None":
                    out["phase"] = "between"
                continue
            started = re.search(r"update started : download \d+/(\d+), .*stage \d+/(\d+)", line)
            if started:
                out = out or {}
                out["download"], out["stage"] = int(started.group(1)), int(started.group(2))
                continue
            commit = re.search(r'starting commit from "([^"]+)"', line)
            if commit:
                lib = commit.group(1)
        if out is not None:
            out["commit_from"] = lib
            return out
    return None


# --- uninstalling a Heroic game without Heroic -------------------------------------
# Heroic takes no uninstall from outside (its links only launch). These are its
# own steps (2.22.4, the gog / legendary / nile game managers' uninstall()),
# with the answers its Uninstall question has by default: the Wine prefix and
# the game's settings and log are KEPT. Heroic must be closed meanwhile - its
# in-memory stores would write the game back. If a Heroic update changes these
# steps: memory note heroic-uninstall-risk.

_ARCH = "arm64" if os.uname().machine in ("aarch64", "arm64") else "x64"
_HEROIC_RESOURCES = [
    Path("/usr/lib/heroic-games-launcher"),                          # Arch / AUR
    Path("/usr/lib/heroic"),
    Path("/opt/Heroic/resources"),                                   # .deb / .rpm
    Path.home() / ".local/share/flatpak/app/com.heroicgameslauncher.hgl/current/active/files/bin/heroic/resources",
    Path("/var/lib/flatpak/app/com.heroicgameslauncher.hgl/current/active/files/bin/heroic/resources"),
]


def heroic_tool(name: str) -> Optional[str]:
    """Heroic's own copy of legendary / nile (the one it runs), its
    settings' alternative one if set, else one on PATH."""
    root = heroic_root()
    try:
        settings = json.loads((root / "config.json").read_text(encoding="utf-8")).get("defaultSettings") or {}
        alt = settings.get({"legendary": "altLegendaryBin", "nile": "altNileBin"}.get(name, "")) or ""
        if alt and Path(alt).is_file():
            return alt
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    for res in _HEROIC_RESOURCES:
        path = res / "app.asar.unpacked" / "build" / "bin" / _ARCH / "linux" / name
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which(name)


def _safe_game_dir(path: str) -> Optional[Path]:
    """A game's folder that's fine to delete: absolute, existing, not a
    home, a root, or a drive's top."""
    if not path:
        return None
    p = Path(path)
    try:
        p = p.resolve()
    except OSError:
        return None
    home = Path.home()
    if not p.is_absolute() or not p.is_dir() or len(p.parts) < 3 or p in (home, Path("/")) \
            or p in home.parents:
        return None
    # under the home: a game's folder is at least two down (~/Games/<game>), never
    # one of the home's own folders (~/Documents, ~/Games itself)
    try:
        if len(p.relative_to(home).parts) < 2:
            return None
    except ValueError:
        pass
    return p


def _plain_id(text: str) -> bool:
    """A store's game id that's safe inside a path: no folders, no . / .."""
    return bool(text) and "/" not in text and "\0" not in text and text not in (".", "..")


def _remove_empty_dirs(top: Path):
    """Removes top and the folders under it only where nothing at all is
    left (no files, no links) - never deletes a file."""
    for dirpath, _dirs, _files in os.walk(top, topdown=False):
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def _remove_shortcuts(title: str, app_name: str):
    """Heroic's removeShortcuts: <title>.desktop on the desktop and in the
    app menu - only ones that start this game through Heroic."""
    safe = re.sub(r'[\\/:*?"<>|]', "", title).strip()
    desktop = Path(os.environ.get("XDG_DESKTOP_DIR") or Path.home() / "Desktop")
    for f in (desktop / f"{safe}.desktop", Path.home() / ".local/share/applications" / f"{safe}.desktop"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "heroic" in text and app_name in text:
            try:
                f.unlink()
            except OSError:
                pass


def heroic_uninstall(store: str, app_name: str, title: str) -> Tuple[bool, str]:
    """Uninstalls one Heroic game the way Heroic does, prefix and settings
    kept. Blocks (minutes for a big game): call it from a worker thread.
    (ok, what went wrong)."""
    import subprocess
    if not _plain_id(app_name):
        return False, f"not a game id: {app_name!r}"
    root = heroic_root()
    if root is None:
        return False, "no Heroic config"
    if store in ("epic", "amazon"):
        tool, env = _tool_cmd(store)
        if tool is None:
            return False, f"{'legendary' if store == 'epic' else 'nile'} not found"
        cmd = tool + (["uninstall", app_name, "-y"] if store == "epic" else ["uninstall", app_name])
        before = (_heroic_installed(root, store) or {}).get(app_name) or {}
        folder = _safe_game_dir(before.get("install_path") or before.get("path") or "")
        try:
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, str(e)
        still = _heroic_installed(root, store) or {}
        if app_name in still:
            lines = (res.stderr or res.stdout or "").strip().splitlines()
            return False, lines[-1] if lines else f"exit {res.returncode}"
        if folder is not None:                  # the tools leave the empty folders behind
            _remove_empty_dirs(folder)
        _remove_shortcuts(title, app_name)
        return True, ""
    if store == "gog":
        from plunderhub.core.file_lock import held
        with held(root / "gog_store" / "installed.json"):
            return _gog_uninstall(root, app_name, title)
    return False, f"unknown store {store}"


def _gog_uninstall(root: Path, app_name: str, title: str) -> Tuple[bool, str]:
    """GOG's part of heroic_uninstall, with installed.json's lock held."""
    path = root / "gog_store" / "installed.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, str(e)
    entries = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return False, "unknown installed.json"
    entry = next((e for e in entries if isinstance(e, dict) and str(e.get("appName")) == app_name), None)
    if entry is None:
        return True, ""                       # already gone
    # (Heroic also runs the game's own unins000.exe in its prefix first, for
    # the prefix's registry; the files go with the folder below either way)
    folder = _safe_game_dir(entry.get("install_path") or "")
    # never a folder that holds another installed game (Heroic's install folder itself)
    for other in entries:
        if folder is None or other is entry or not isinstance(other, dict):
            continue
        o = _safe_game_dir(str(other.get("install_path") or ""))
        if o is not None and (o == folder or folder in o.parents):
            return False, f"{folder} holds another game too - not removed"
    if folder is not None:
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            return False, f"couldn't remove {folder}"
    gogdl = root / "gogdlConfig" / "heroic_gogdl"
    try:
        (gogdl / "manifests" / app_name).unlink()
    except OSError:
        pass
    shutil.rmtree(gogdl / "gog-support" / app_name, ignore_errors=True)
    data["installed"] = [e for e in entries if e is not entry]
    tmp = path.with_suffix(".json.plunderhub-tmp")
    tmp.write_text(json.dumps(data, indent="\t", ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    _remove_shortcuts(title, app_name)
    return True, ""


# --- installing a Heroic game without Heroic ----------------------------------------
# Heroic 2.22.4's install(): Epic `legendary install <app> --platform Windows
# --base-path <dir> --skip-dlcs --skip-sdl -y`, Amazon `nile install --base-path
# <dir> <id>`, GOG `gogdl --auth-config-path <auth> download <id> --platform
# windows --path <dir> --support <gog-support/id> --skip-dlcs --lang <lang>`
# then the installed.json entry Heroic writes itself. The game's Wine settings
# go where Heroic's install window puts them (GamesConfig/<app>.json:
# winePrefix, wineVersion). Memory note heroic-uninstall-risk covers updates.

def heroic_game_prefix(app_name: str) -> Optional[Path]:
    """The Wine prefix Heroic's settings give a game (kept at uninstall)."""
    root = heroic_root()
    if root is None:
        return None
    try:
        data = json.loads((root / "GamesConfig" / f"{app_name}.json").read_text(encoding="utf-8"))
        prefix = (data.get(app_name) or {}).get("winePrefix")
    except (OSError, ValueError, AttributeError):
        return None
    return Path(os.path.expanduser(prefix)) if prefix else None


def _sanitize(title: str) -> str:
    """Heroic's removeSpecialcharacters (its folder and prefix names)."""
    return re.sub(r'[:|/*?<>\\&{}%$@`!\n+\'"]', "", title).strip()


def heroic_defaults(title: str) -> Dict:
    """What Heroic's install window fills in: its default install folder,
    <defaultWinePrefixDir>/<title> and its default Wine / Proton."""
    root = heroic_root()
    s = {}
    try:
        s = json.loads((root / "config.json").read_text(encoding="utf-8")).get("defaultSettings") or {}
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    base = s.get("defaultInstallPath") or str(Path.home() / "Games" / "Heroic")
    prefix_dir = s.get("defaultWinePrefixDir") or str(Path.home() / "Games" / "Heroic" / "Prefixes")
    return {"install_path": base, "prefix": f"{prefix_dir.rstrip('/')}//{_sanitize(title)}"
            if prefix_dir.endswith("/") else f"{prefix_dir}/{_sanitize(title)}",
            "wine": _usable_wine(s.get("wineVersion"))}


def _usable_wine(w) -> Dict:
    """Heroic's default Wine / Proton when it is really there - a fresh
    Flatpak Heroic stores {"bin": "", "name": "Default Wine - Not Found"}
    (no system wine in its sandbox; seen on Ubuntu 26.04)."""
    if isinstance(w, dict) and w.get("bin") and Path(str(w["bin"])).exists():
        return w
    return {}


def preferred_wine(versions: List[Dict]) -> Dict:
    """The build to offer when Heroic has no usable default: a Proton
    named ...Latest (Heroic's own Proton-GE Latest), else the first."""
    for v in versions:
        if v.get("type") == "proton" and "latest" in str(v.get("name", "")).lower():
            return v
    return versions[0] if versions else {}


def heroic_wine_versions() -> List[Dict]:
    """The Wine / Proton builds Heroic offers: its own tools folders, Steam's
    Protons and compatibility tools, the system's wine."""
    root = heroic_root()
    out, seen = [], set()

    def add(name, binary, kind):
        if not binary or not Path(binary).exists():
            return
        real = str(Path(binary).resolve())
        if real not in seen:
            seen.add(real)
            out.append({"bin": str(binary), "name": name, "type": kind})
    if root is not None:
        for d in sorted((root / "tools" / "proton").glob("*")):
            add(d.name, d / "proton", "proton")
        for d in sorted((root / "tools" / "wine").glob("*")):
            add(d.name, d / "bin" / "wine", "wine")
        try:                                    # the one picked in Heroic's settings
            w = json.loads((root / "config.json").read_text(encoding="utf-8"))["defaultSettings"]["wineVersion"]
            if _usable_wine(w) and str(Path(w["bin"]).resolve()) not in seen:
                add(str(w.get("name") or Path(w["bin"]).parent.name), w["bin"], str(w.get("type") or "wine"))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if out:
        # what Heroic downloaded for itself (its Wine Manager) - the same
        # builds again from Steam's folders only made the list confusing
        return out
    for r in steam_roots():
        for d in sorted((r / "compatibilitytools.d").glob("*")):
            add(f"{d.name} (Steam)", d / "proton", "proton")
        for d in sorted((r / "steamapps" / "common").glob("Proton*")):
            add(f"{d.name} (Steam)", d / "proton", "proton")
    sys_wine = shutil.which("wine") if not heroic_flatpak_id() else None   # not inside a Flatpak
    if sys_wine:
        add("Wine Default", sys_wine, "wine")
    return out


def heroic_flatpak_id() -> Optional[str]:
    """Heroic's Flatpak app id when the Heroic in use is the Flatpak one
    (its settings under ~/.var/app/<id>), else None."""
    root = heroic_root()
    if root is None:
        return None
    try:
        return root.relative_to(Path.home() / ".var" / "app").parts[0]
    except (ValueError, IndexError):
        return None


def _flatpak_tool(app_id: str, name: str) -> Optional[str]:
    """legendary / gogdl / nile as the Flatpak sees it (/app/...), when
    the Flatpak has it."""
    for base in (Path.home() / ".local/share/flatpak", Path("/var/lib/flatpak")):
        files = base / "app" / app_id / "current" / "active" / "files"
        for sub in (f"bin/heroic/resources/app.asar.unpacked/build/bin/{_ARCH}/linux",
                    "bin/heroic/resources/app.asar.unpacked/build/bin/linux"):
            if (files / sub / name).is_file():
                return f"/app/{sub}/{name}"
    return None


def _tool_cmd(store: str) -> Tuple[Optional[List[str]], Dict[str, str]]:
    """The command line of Heroic's own tool for a store, and its env. A
    Flatpak Heroic's tools run inside its Flatpak (flatpak run --command):
    its own libraries and the same folders Heroic itself can see."""
    root = heroic_root()
    if root is None:
        return None, dict(os.environ)
    name = {"epic": "legendary", "amazon": "nile"}.get(store, "gogdl")
    extra = {"epic": {"LEGENDARY_CONFIG_PATH": str(root / "legendaryConfig" / "legendary")},
             "amazon": {"NILE_CONFIG_PATH": str(root / "nile_config")}}.get(
        store, {"GOGDL_CONFIG_PATH": str(root / "gogdlConfig")})
    tail = ["--auth-config-path", str(root / "gog_store" / "auth.json")] if name == "gogdl" else []
    env = dict(os.environ)
    app_id = heroic_flatpak_id()
    if app_id and shutil.which("flatpak"):
        inside = _flatpak_tool(app_id, name)
        if inside is None:
            return None, env
        return (["flatpak", "run", "--die-with-parent", f"--command={inside}"]
                + [f"--env={k}={v}" for k, v in extra.items()] + [app_id] + tail), env
    tool = heroic_tool(name)
    env.update(extra)
    return ([tool] + tail if tool else None), env


def heroic_install_info(store: str, app_name: str) -> Dict:
    """Download / disk size before installing (Epic, GOG) - the tool asks
    its store; blocks a few seconds."""
    import subprocess
    cmd, env = _tool_cmd(store)
    if cmd is None or store == "amazon":
        return {}
    args = ["info", app_name, "--json"] if store == "epic" else ["info", app_name, "--os", "windows"]
    try:
        res = subprocess.run(cmd + args, env=env, capture_output=True, text=True, timeout=60)
        data = json.loads(res.stdout[res.stdout.find("{"):])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    if store == "gog":
        # gogdl info: {"size": {lang|"*": {download_size, disk_size}}, "buildId",
        # "versionEtag", "versionName", "folder_name", "languages", ...}
        langs = data.get("languages") or []
        lang = "en-US" if "en-US" in langs else (langs[0] if langs else "en-US")
        sizes = data.get("size") or {}
        sz = sizes.get(lang) or next((v for v in sizes.values() if (v or {}).get("disk_size")), {}) or {}
        return {"download_size": int(sz.get("download_size") or 0), "disk_size": int(sz.get("disk_size") or 0),
                "lang": lang, "folder_name": data.get("folder_name") or "",
                "game": {"version": data.get("versionName") or "", "buildId": data.get("buildId") or ""},
                "manifest": {"versionEtag": data.get("versionEtag") or ""}}
    m = data.get("manifest") or {}
    return {"download_size": int(m.get("download_size") or 0), "disk_size": int(m.get("disk_size") or 0),
            "game": data.get("game") or {}, "manifest": m}


_PROGRESS = re.compile(r"Progress: (\d+(?:\.\d+)?)")
_DOWNLOADED = re.compile(r"Downloaded: (\S+) MiB")
_SPEED = re.compile(r"Download\s+- (\S+) MiB")


def heroic_install(store: str, app_name: str, title: str, base_path: str, prefix: str, wine: Dict,
                   on_progress=None, on_proc=None, lang: str = "en-US") -> Tuple[bool, str]:
    """Installs like Heroic does; blocks (a worker thread). on_progress(
    percent, downloaded_bytes, bytes_per_s), on_proc(Popen) to stop it."""
    import subprocess
    root = heroic_root()
    cmd, env = _tool_cmd(store)
    if root is None or cmd is None:
        return False, "Heroic's tools weren't found"
    Path(base_path).mkdir(parents=True, exist_ok=True)
    if store == "epic":
        args = ["install", app_name, "--platform", "Windows", "--base-path", base_path,
                "--skip-dlcs", "--skip-sdl", "-y"]
    elif store == "amazon":
        args = ["install", "--base-path", base_path, app_name]
    else:
        args = ["download", app_name, "--platform", "windows", "--path", base_path,
                "--support", str(root / "gogdlConfig" / "heroic_gogdl" / "gog-support" / app_name),
                "--skip-dlcs", "--lang", lang]
    try:
        proc = subprocess.Popen(cmd + args, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, start_new_session=True)
    except OSError as e:
        return False, str(e)
    if on_proc:
        on_proc(proc)
    tail: List[str] = []
    pct, got, speed = 0.0, 0, 0.0
    for line in proc.stdout:
        tail = (tail + [line.rstrip()])[-20:]
        m = _PROGRESS.search(line)
        if m:
            pct = float(m.group(1))
        m = _DOWNLOADED.search(line)
        if m:
            try:
                got = int(float(m.group(1)) * 1024 * 1024)
            except ValueError:
                pass
        m = _SPEED.search(line)
        if m:
            try:
                speed = float(m.group(1)) * 1024 * 1024
            except ValueError:
                pass
        if on_progress and (_PROGRESS.search(line) or _DOWNLOADED.search(line)):
            on_progress(pct, got, speed)
    code = proc.wait()
    if code != 0:
        why = next((l for l in reversed(tail) if "error" in l.lower()), tail[-1] if tail else f"exit {code}")
        return False, why.strip()
    if store == "gog":
        info = heroic_install_info("gog", app_name)
        folder = info.get("folder_name")
        if not folder:
            try:
                lib = json.loads((root / "store_cache" / "gog_library.json").read_text(encoding="utf-8"))
                item = next((g for g in lib.get("games") or [] if str(g.get("app_name")) == app_name), {})
                folder = item.get("folder_name")
            except (OSError, ValueError):
                pass
        install_path = str(Path(base_path) / (folder or _sanitize(title)))
        entry = {"platform": "windows", "executable": "", "install_path": install_path,
                 "install_size": f"{folder_bytes(Path(install_path), 30) / 1024 ** 3:.2f} GiB",
                 "is_dlc": False, "version": (info.get("game") or {}).get("version") or "",
                 "appName": app_name, "installedDLCs": [], "language": lang,
                 "versionEtag": (info.get("manifest") or {}).get("versionEtag") or "",
                 "buildId": (info.get("game") or {}).get("buildId") or "", "pinnedVersion": False}
        path = root / "gog_store" / "installed.json"
        from plunderhub.core.file_lock import held
        with held(path):            # an uninstall's worker may rewrite it meanwhile
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data["installed"] = [e for e in data.get("installed") or []
                                 if str(e.get("appName")) != app_name] + [entry]
            tmp = path.with_suffix(".json.plunderhub-tmp")
            tmp.write_text(json.dumps(data, indent="\t", ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
    _write_game_config(root, app_name, prefix, wine)
    return True, ""


def _write_game_config(root: Path, app_name: str, prefix: str, wine: Dict):
    from plunderhub.core.file_lock import held
    with held(root / "GamesConfig" / f"{app_name}.json"):
        _write_game_config_locked(root, app_name, prefix, wine)


def _write_game_config_locked(root: Path, app_name: str, prefix: str, wine: Dict):
    """The game's own Heroic settings, as its install window leaves them: a
    copy of Heroic's defaults with this game's Wine prefix and version.
    An existing file keeps everything else."""
    path = root / "GamesConfig" / f"{app_name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    game = data.get(app_name) if isinstance(data.get(app_name), dict) else None
    if not game:
        try:
            game = dict(json.loads((root / "config.json").read_text(encoding="utf-8")).get("defaultSettings") or {})
        except (OSError, ValueError, AttributeError):
            game = {}
    if prefix:
        game["winePrefix"] = prefix
    if wine:
        game["wineVersion"] = wine
    data[app_name] = game
    data.setdefault("version", "v0")
    data.setdefault("explicit", True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.plunderhub-tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
