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
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class GameItem:
    launcher: str
    title: str
    uid: str
    launch_cmd: List[str]
    launch_label: str
    env: Optional[Dict[str, str]] = None
    install_dir: Optional[str] = None
    store_url: Optional[str] = None
    store_platform: Optional[str] = None


# Heroic's sources, as GameItem.store_platform spells them - also the
# keys of the Heroic store filter. "sideload" is games added to Heroic by
# hand ("Add game"), which belong to no store.
HEROIC_STORES = ("epic", "gog", "amazon", "zoom", "sideload")


def resolve_store_url(game: "GameItem") -> str:
    if game.store_url:
        return game.store_url
    quoted_title = urllib.parse.quote(game.title, safe="")
    if game.store_platform == "gog":
        return f"https://www.gog.com/en/games?query={quoted_title}"
    if game.store_platform == "epic":
        return f"https://store.epicgames.com/en-US/browse?q={quoted_title}&sortBy=relevancy&sortDir=DESC"
    if game.store_platform == "amazon":
        return f"https://gaming.amazon.com/search?q={quoted_title}"
    return f"https://store.steampowered.com/search/?term={quoted_title}"


_STEAM_APPID_RE = re.compile(r'/app/(\d+)')


def resolve_protondb_url(game: "GameItem") -> str:
    appid = None
    if game.uid.startswith("steam:"):
        appid = game.uid.split(":", 1)[1]
    elif game.store_url:
        m = _STEAM_APPID_RE.search(game.store_url)
        if m:
            appid = m.group(1)
    if appid and appid.isdigit():
        return f"https://www.protondb.com/app/{appid}"
    quoted_title = urllib.parse.quote(game.title, safe="")
    return f"https://www.protondb.com/search?q={quoted_title}"


@dataclass
class LauncherDef:
    name: str
    scan_method: Optional[str]
    binary: Optional[str]
