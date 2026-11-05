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

"""A game's ProtonDB rating (Platinum, Gold, Silver, Bronze, Borked) -
for every launcher, not only Steam. No GTK; `rating()` blocks (network),
call it from a worker thread.

ProtonDB knows games by their Steam appid only:
  - a Steam game has it in its uid ("steam:1245620")
  - any other game (Heroic, Lutris, Faugus, Bottles...) is looked up by
    its title in Steam's store search, and only a result whose name is
    the SAME title (case, ™/®, punctuation and an edition suffix aside)
    is taken - the search's first hit for "Hades" is "Hades II". No sure
    match: no rating (better none than a wrong one).

What goes out: the title (to Steam's store search, only for non-Steam
games) and the appid (to ProtonDB). The README's Privacy part says so,
and Settings can turn it off (settings["protondb_enabled"]).

Both answers are kept in CACHE_DIR/protondb.json: a title's match for
30 days (a miss for 7), a rating for 3 days - a game page opened again
asks nobody.
"""

import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from plunderhub.constants import CACHE_DIR
from plunderhub.core.utils import HEADERS, http_get_json
from plunderhub.logging_setup import logger

TIERS = ("platinum", "gold", "silver", "bronze", "borked")
_CACHE_FILE = CACHE_DIR / "protondb.json"
_MATCH_TTL = 30 * 86400
_MISS_TTL = 7 * 86400
_TIER_TTL = 3 * 86400
_lock = threading.Lock()

# trailing words a store adds to the same game ("Fallout: New Vegas Ultimate Edition")
_EDITION = re.compile(
    r"\s+(?:(?:game of the year|goty|definitive|complete|enhanced|ultimate|deluxe|gold|"
    r"remastered|anniversary|standard|special|digital deluxe)\s+edition|goty|remastered|"
    r"director s cut|directors cut)$")


@dataclass
class Rating:
    tier: str           # one of TIERS, "native" (a Linux version on Steam - what
                        # ProtonDB's page shows then) or "pending" (too few reports yet)
    total: int          # how many reports
    appid: str
    by_title: bool      # matched by title (not a Steam game): the Steam version's rating
    proton: str = ""    # for a "native" one: the reports' tier, how the Windows version runs with Proton

    @property
    def url(self) -> str:
        return page_url(self.appid)


def page_url(appid: str) -> str:
    return f"https://www.protondb.com/app/{appid}"


def normalize(title: str, strip_edition: bool = False) -> str:
    """'DOOM Eternal™' and 'Doom Eternal' alike; 'é' as 'e', '&' as 'and'.
    strip_edition: an edition word at the end goes too (GOTY, Remastered...)."""
    text = re.sub(r"[™®©]", "", title or "")     # before NFKD, which spells ™ as "TM"
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ").replace("'", "")
    text = re.sub(r"[^0-9a-z]+", " ", text).strip()
    return _EDITION.sub("", text).strip() if strip_edition else text


def _load() -> dict:
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("match", {})
            data.setdefault("tier", {})
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug(f"ProtonDB cache unreadable, starting afresh: {e}")
    return {"match": {}, "tier": {}}


def _save(data: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_CACHE_FILE)
    except Exception as e:
        logger.debug(f"Couldn't save the ProtonDB cache: {e}")


def steam_appid_by_title(title: str) -> Optional[str]:
    """The appid of the Steam game named exactly `title`, '' when Steam
    has none, None when Steam didn't answer (ask again later). A title
    with an edition word that finds nothing is searched without it too
    ("The Witcher 3: Wild Hunt - Game of the Year Edition")."""
    found = _search(title)
    if found == "":
        shorter = _strip_edition_text(title)
        if shorter and shorter != title:
            found = _search(shorter)
    return found


def _strip_edition_text(title: str) -> str:
    """The title as written, an edition at its end cut off - what to search for."""
    return re.sub(r"\s*[-–—:]?\s*(?:the\s+)?(?:game of the year|goty|definitive|complete|enhanced|ultimate|"
                  r"deluxe|remastered|anniversary|digital deluxe)(?:\s+edition)?\s*$|\s*[-–—:]?\s*"
                  r"director'?s cut\s*$", "", title, flags=re.I).strip()


def _search(title: str) -> Optional[str]:
    wanted = normalize(title)
    if not wanted:
        return ""
    url = ("https://store.steampowered.com/api/storesearch/?term="
           + urllib.parse.quote(title, safe="") + "&l=english&cc=US")
    data = http_get_json(url, HEADERS, timeout=10, retries=2)
    if not isinstance(data, dict):
        return None
    apps = [(str(i.get("name", "")), str(i.get("id", ""))) for i in data.get("items") or []
            if isinstance(i, dict) and i.get("type", "app") == "app" and str(i.get("id", "")).isdigit()]
    # the same name first; only then the same name but for an edition word
    for name, appid in apps:
        if normalize(name) == wanted:
            return appid
    bare = normalize(title, strip_edition=True)
    for name, appid in apps:
        if normalize(name, strip_edition=True) == bare:
            return appid
    return ""


def _fetch_tier(appid: str) -> Optional[dict]:
    """{'tier', 'total'}; {'tier': ''} when ProtonDB has no page for it
    (404), None when it didn't answer (asked again next time)."""
    url = f"https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
    try:
        req = urllib.request.Request(url, headers={**HEADERS, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"tier": ""}
        logger.debug(f"ProtonDB didn't answer for {appid}: {e}")
        return None
    except Exception as e:
        logger.debug(f"ProtonDB didn't answer for {appid}: {e}")
        return None
    if not isinstance(data, dict):
        return None
    tier = str(data.get("tier") or "").lower()
    if tier not in TIERS and tier != "pending":
        tier = ""
    try:
        total = int(data.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    return {"tier": tier, "total": total}


def _steam_native(appid: str) -> Optional[bool]:
    """Whether Steam lists a Linux version of the game; None when Steam
    didn't answer."""
    data = http_get_json(f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=platforms",
                         HEADERS, timeout=10, retries=2)
    if not isinstance(data, dict):
        return None
    entry = data.get(appid) or {}
    platforms = (entry.get("data") or {}).get("platforms") if isinstance(entry.get("data"), dict) else None
    return bool(isinstance(platforms, dict) and platforms.get("linux"))


def rating(uid: str, title: str) -> Optional[Rating]:
    """The game's ProtonDB rating, None when there's no sure Steam match
    or ProtonDB has no rating for it. Blocks: a worker thread."""
    now = time.time()
    with _lock:
        cache = _load()
    if uid.startswith("steam:") and uid.split(":", 1)[1].isdigit():
        appid, by_title = uid.split(":", 1)[1], False
    else:
        by_title = True
        key = normalize(title)
        hit = cache["match"].get(key)
        if isinstance(hit, dict) and now - hit.get("t", 0) < (_MATCH_TTL if hit.get("appid") else _MISS_TTL):
            appid = hit.get("appid") or ""
        else:
            appid = steam_appid_by_title(title)
            if appid is None:
                return None             # Steam didn't answer: not kept
            cache["match"][key] = {"appid": appid, "t": now}
        if not appid:
            with _lock:
                _save(cache)
            return None
    got = cache["tier"].get(appid)
    if not (isinstance(got, dict) and now - got.get("t", 0) < _TIER_TTL):
        got = _fetch_tier(appid)
        native = _steam_native(appid) if got is not None else None
        if native is None:
            got = None
        elif native:
            # ProtonDB's own page says NATIVE then, whatever the reports say
            got["proton"] = got.get("tier") if got.get("tier") in TIERS else ""
            got["tier"] = "native"
        if got is None:
            got = {"tier": ""}          # not kept: asked again next time
        else:
            got["t"] = now
            cache["tier"][appid] = got
    with _lock:
        fresh = _load()
        fresh["match"].update(cache["match"])
        fresh["tier"].update(cache["tier"])
        _save(fresh)
    if not got.get("tier"):
        return None
    return Rating(got["tier"], int(got.get("total") or 0), appid, by_title, str(got.get("proton") or ""))
