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
"""The Harbors in numbers: per store launcher in Faugus (Battle.net, EA app,
Ubisoft Connect...) how many games it holds, what they take, and what the
launcher itself takes - its Wine prefix less the games inside it (a game
under the launcher's own folder, Ubisoft's games/, would count twice).

    report(games, size_of) -> {launcher uid: Harbor}
"""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from plunderhub.core.models import GameItem


@dataclass
class Harbor:
    games: int = 0              # games its launcher installed
    games_bytes: int = 0        # what they take
    own_bytes: int = 0          # the launcher's prefix less its games
    prefix: str = ""


def prefix_of(game: GameItem) -> Optional[str]:
    """A Faugus store launcher's Wine prefix (its env says it)."""
    pfx = (game.env or {}).get("WINEPREFIX") or ""
    return os.path.realpath(os.path.expanduser(pfx)) if pfx else None


def _inside(path: str, top: str) -> bool:
    return path == top or path.startswith(top.rstrip("/") + "/")


def report(games: Iterable[GameItem], size_of: Callable[[Path], int]) -> Dict[str, Harbor]:
    """Blocks (walks the prefixes): a worker thread."""
    from plunderhub.core import faugus_store
    games = list(games)
    known = faugus_store.known()
    out: Dict[str, Harbor] = {}
    for g in games:
        if g.store_platform != "store-launcher" or not g.uid.startswith("faugus:"):
            continue
        lid = g.uid.split(":", 1)[1]
        harbor = Harbor(prefix=prefix_of(g) or "")
        inside_prefix = 0
        for info in known.values():
            if str(info.get("launcher")) != lid:
                continue
            harbor.games += 1
            folder = str(info.get("install_dir") or "")
            if folder and os.path.isdir(folder):
                size = size_of(Path(folder))
                harbor.games_bytes += size
                if harbor.prefix and _inside(os.path.realpath(folder), harbor.prefix):
                    inside_prefix += size
        if harbor.prefix and os.path.isdir(harbor.prefix):
            harbor.own_bytes = max(0, size_of(Path(harbor.prefix)) - inside_prefix)
        out[g.uid] = harbor
    return out
