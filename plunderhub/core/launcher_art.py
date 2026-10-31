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

"""The art a game's own launcher shows for it - so there are covers
without a SteamGridDB key.

art_for(game) -> {"cover" | "hero" | "icon": a local file or an http(s) URL}

  Steam    its client's cache: appcache/librarycache/<appid>/
  Heroic   the art_* URLs in its store_cache library files
  Lutris   coverart/<slug>.jpg and its lutris_<slug>.png icon
  Faugus   covers/ banners/ icons/ <gameid>.png (faugus/path_manager.py)
  Bottles  the program's thumbnail in library.yml: grid:<file> in the
           bottle's grids/ folder, umu-grid:<file> in umu/covers/

(Lutris: lutris/settings.py COVERART_PATH, ICON_PATH; Heroic: CacheStore
<store>_library under store_cache/, sideload_apps/library.json.)

Only reads files; downloads are left to the caller."""

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

from plunderhub.core.models import GameItem
from plunderhub.core.scan_paths import HEROIC_CONFIG_ROOTS, bottles_library_path, faugus_games_path, lutris_db_path
from plunderhub.core.steam_heroic import steam_roots
from plunderhub.logging_setup import logger

_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".ico", ".gif")


def art_for(game: GameItem) -> Dict[str, str]:
    kind, _, rest = game.uid.partition(":")
    try:
        if kind == "steam":
            return _steam(rest)
        if kind == "heroic":
            return _heroic(rest.split(":", 1)[-1])
        if kind == "lutris":
            return _lutris(rest)
        if kind == "faugus":
            return _faugus(rest)
        if kind == "bottles":
            return _bottles(rest)
    except Exception as e:      # a launcher's odd file never stops the rest
        logger.debug(f"No launcher art for {game.uid}: {e}")
    return {}


def _first(paths: List[Path]) -> Optional[str]:
    for path in paths:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return str(path)
        except OSError:
            continue
    return None


def _steam(appid: str) -> Dict[str, str]:
    if not appid.isdigit():
        return {}
    found: Dict[str, str] = {}
    for root in steam_roots():
        cache = root / "appcache" / "librarycache"
        folder = cache / appid
        # the client of 2024 on: a folder per game, a hash folder per asset;
        # before that: <appid>_library_600x900.jpg beside every other game's
        nested = sorted(folder.glob("*/*")) if folder.is_dir() else []
        by_name: Dict[str, List[Path]] = {}
        for path in nested:
            by_name.setdefault(path.name, []).append(path)
        cover = _first(by_name.get("library_capsule_2x.jpg", []) + by_name.get("library_capsule.jpg", [])
                       + by_name.get("library_600x900_2x.jpg", []) + by_name.get("library_600x900.jpg", [])
                       + [cache / f"{appid}_library_600x900_2x.jpg", cache / f"{appid}_library_600x900.jpg"])
        hero = _first(by_name.get("library_hero_2x.jpg", []) + by_name.get("library_hero.jpg", [])
                      + [cache / f"{appid}_library_hero.jpg"])
        # the game's icon: a <sha1>.jpg straight in its folder
        icons = sorted(p for p in folder.glob("*.jpg") if re.fullmatch(r"[0-9a-f]{40}\.jpg", p.name)) \
            if folder.is_dir() else []
        icon = _first(icons + [cache / f"{appid}_icon.jpg"])
        for key, value in (("cover", cover), ("hero", hero), ("icon", icon)):
            if value and key not in found:
                found[key] = value
    return found


_HEROIC_LIBRARIES = (("store_cache/gog_library.json", "games"), ("store_cache/legendary_library.json", "library"),
                     ("store_cache/nile_library.json", "library"), ("sideload_apps/library.json", "games"))


def _heroic(app_name: str) -> Dict[str, str]:
    for base in HEROIC_CONFIG_ROOTS:
        for rel, key in _HEROIC_LIBRARIES:
            path = base / "heroic" / rel
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items = data.get(key) if isinstance(data, dict) else None
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict) or str(item.get("app_name")) != app_name:
                    continue
                # Heroic's grid shows art_square; art_background is the wide one
                found = {}
                for kind, fields in (("cover", ("art_square",)), ("hero", ("art_background", "art_cover")),
                                     ("icon", ("art_icon",))):
                    for field in fields:
                        value = item.get(field)
                        if isinstance(value, str) and _usable(value):
                            found[kind] = value
                            break
                return found
    return {}


def _usable(value: str) -> bool:
    if value.startswith(("https://", "http://")):
        return True
    return value.startswith("/") and Path(value).is_file()      # a sideloaded game's own picture


def _lutris(game_id: str) -> Dict[str, str]:
    db = lutris_db_path()
    if db is None or not game_id.isdigit():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        row = con.execute("SELECT slug FROM games WHERE id = ?", (int(game_id),)).fetchone()
    finally:
        con.close()
    slug = row[0] if row else None
    if not slug or "/" in slug:
        return {}
    data = db.parent                    # .../lutris (native or Flatpak)
    found = {}
    cover = _first([data / "coverart" / f"{slug}.jpg", data / "coverart" / f"{slug}.png",
                    Path.home() / ".cache" / "lutris" / "coverart" / f"{slug}.jpg"])
    if cover:
        found["cover"] = cover
    icon = _first([data.parent / "icons" / "hicolor" / "128x128" / "apps" / f"lutris_{slug}.png",
                   Path.home() / ".local/share/icons/hicolor/128x128/apps" / f"lutris_{slug}.png"])
    if icon:
        found["icon"] = icon
    return found


def _faugus(game_id: str) -> Dict[str, str]:
    games = faugus_games_path()
    if games is None or not game_id or "/" in game_id:
        return {}
    found = {}
    legacy = Path.home() / ".config" / "faugus-launcher"      # banners before Faugus 1.10
    for kind, folders in (("cover", (games.parent / "covers",)),
                          ("hero", (games.parent / "banners", legacy / "banners")),
                          ("icon", (games.parent / "icons",))):
        files = []
        for directory in folders:
            try:
                files += sorted(p for p in directory.iterdir()
                                if p.stem == game_id and p.suffix.lower() in _IMAGE_SUFFIXES)
            except OSError:
                continue
        path = _first(files)
        if path:
            found[kind] = path
    return found


def _bottles(game_id: str) -> Dict[str, str]:
    import yaml
    library_path = bottles_library_path()
    if library_path is None:
        return {}
    library = yaml.safe_load(library_path.read_text(encoding="utf-8"))
    entry = next((e for e in (library.values() if isinstance(library, dict) else [])
                  if isinstance(e, dict) and str(e.get("id")) == game_id), None)
    thumbnail = str((entry or {}).get("thumbnail") or "")
    base = library_path.parent
    if thumbnail.startswith("grid:"):
        bottles_root = base / "bottles"
        try:
            custom = yaml.safe_load((base / "data.yml").read_text(encoding="utf-8"))
            if isinstance(custom, dict) and custom.get("custom_bottles_path"):
                bottles_root = Path(custom["custom_bottles_path"])
        except (OSError, yaml.YAMLError):
            pass
        bottle_path = str(((entry or {}).get("bottle") or {}).get("path") or "")
        folder, name = bottles_root / bottle_path / "grids", thumbnail[len("grid:"):]
    elif thumbnail.startswith("umu-grid:"):
        folder, name = base / "umu" / "covers", thumbnail[len("umu-grid:"):]
    else:
        return {}
    if not name or Path(name).name != name or ".." in str(folder):
        return {}
    cover = _first([folder / name])
    return {"cover": cover} if cover else {}
