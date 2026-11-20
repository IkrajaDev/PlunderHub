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

"""A Steam game's achievements, from Steam's own files - no sign-in, no
API key, offline:

  appcache/stats/UserGameStatsSchema_<appid>.bin   every achievement: its
      name and description (in Steam's languages), icons, hidden or not
  appcache/stats/UserGameStats_<account>_<appid>.bin   the player's: which
      are unlocked (a stat's bits) and when (AchievementTimes)

Both are Steam's binary KeyValues. How rare each one is comes from Steam's
public GetGlobalAchievementPercentagesForApp (no key), kept a day; the
icons from Steam's image server, kept for good.

A GOG game in Heroic: GOG's gameplay service, as GOG Galaxy (and Heroic's
own Comet) asks it - with a token for the game's client, got from the
Galaxy sign-in Heroic keeps (gog_store/auth.json) the way Heroic's gogdl
gets one: refresh_token with the game's client id/secret (its build
manifest, gogdlConfig/heroic_gogdl/manifests/<id>) and without_new_session=1,
so Heroic's own sign-in is left as it is. Heroic's files are only read;
the game's token lives in memory. GOG gives the rarity and dates too."""

import hashlib
import json
import struct
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from plunderhub.constants import CACHE_DIR
from plunderhub.core.steam_heroic import steam_localconfig_path, steam_roots
from plunderhub.core.utils import HEADERS, http_get_json
from plunderhub.logging_setup import logger

CACHE = CACHE_DIR / "achievements"
RARITY_MAX_AGE = 24 * 3600
# Steam's achievement icons, tried in turn: the community assets store, from the
# host Steam's own pages use (fastly, 11-20: all 336 icons of 14 games), then its
# akamai mirror; last the old store, which lacks newer games' icons (Valheim: all
# 62 missing there - checked 11-20, PlunderHubNotes/game-launch.md "Achievements")
ICON_URLS = ("https://shared.fastly.steamstatic.com/community_assets/images/apps/{appid}/{name}",
             "https://shared.akamai.steamstatic.com/community_assets/images/apps/{appid}/{name}",
             "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{appid}/{name}")
RARITY_URL = ("https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"
              "?gameid={appid}&format=json")

# PlunderHub's languages -> Steam's
STEAM_LANG = {"en": "english", "de": "german", "es": "spanish", "fr": "french", "hu": "hungarian",
              "pl": "polish", "pt_br": "brazilian", "ru": "russian", "uk": "ukrainian"}


@dataclass
class Achievement:
    key: str                    # its API name
    name: str
    desc: str
    hidden: bool
    icon: str                   # the icon's file name on Steam's image server
    icon_gray: str
    unlocked_at: Optional[float] = None     # time.time(), None while locked
    percent: Optional[float] = None         # of all players, when known


@dataclass
class GameAchievements:
    appid: str
    items: List[Achievement] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def unlocked(self) -> int:
        return sum(1 for a in self.items if a.unlocked_at is not None)


def _kv(b: bytes, p: int):
    """Steam's binary KeyValues (inline key names): (dict, end)."""
    d: Dict = {}
    while True:
        t = b[p]
        p += 1
        if t == 8:
            return d, p
        e = b.index(b"\0", p)
        key = b[p:e].decode("utf-8", "replace")
        p = e + 1
        if t == 0:
            v, p = _kv(b, p)
        elif t == 1:
            e = b.index(b"\0", p)
            v = b[p:e].decode("utf-8", "replace")
            p = e + 1
        elif t in (2, 4, 6):
            v = struct.unpack_from("<i", b, p)[0]
            p += 4
        elif t == 3:
            v = struct.unpack_from("<f", b, p)[0]
            p += 4
        elif t == 7:
            v = struct.unpack_from("<Q", b, p)[0]
            p += 8
        else:
            raise ValueError(f"KeyValues type {t}")
        d[key] = v


def _read_kv(path: Path) -> Optional[Dict]:
    try:
        return _kv(path.read_bytes(), 0)[0]
    except (OSError, ValueError, IndexError, struct.error) as e:
        logger.debug(f"Achievements: couldn't read {path}: {e}")
        return None


def _stats_dirs() -> List[Path]:
    return [root / "appcache" / "stats" for root in steam_roots() if (root / "appcache" / "stats").is_dir()]


def _account() -> Optional[str]:
    """The signed-in Steam account's id (its userdata folder's name)."""
    path = steam_localconfig_path()
    return path.parent.parent.name if path is not None else None


def _text(value, lang: str) -> str:
    if isinstance(value, dict):
        return str(value.get(STEAM_LANG.get(lang, "english")) or value.get("english")
                   or next(iter(value.values()), "") or "")
    return str(value or "")


def has_achievements(appid: str) -> bool:
    """Whether Steam knows achievements for the game (its schema file is there)."""
    return any((d / f"UserGameStatsSchema_{appid}.bin").is_file() for d in _stats_dirs())


def steam_achievements(appid: str, lang: str = "en", rarity: bool = True) -> Optional[GameAchievements]:
    """The game's achievements and the player's progress; None when Steam
    has no achievements for it. rarity: the global percentages too (from
    the web, kept a day) - call off the GTK thread."""
    schema = None
    stats_dir = None
    for d in _stats_dirs():
        if (d / f"UserGameStatsSchema_{appid}.bin").is_file():
            schema = _read_kv(d / f"UserGameStatsSchema_{appid}.bin")
            stats_dir = d
            break
    if not schema:
        return None
    root = schema.get(appid) or next(iter(schema.values()), {})
    stats = (root or {}).get("stats") or {}
    account = _account()
    user = _read_kv(stats_dir / f"UserGameStats_{account}_{appid}.bin") if account else None
    cache = (user or {}).get("cache") or {}
    out = GameAchievements(appid)
    for stat_id, stat in stats.items():
        bits = stat.get("bits") if isinstance(stat, dict) else None
        if not isinstance(bits, dict):
            continue
        mine = cache.get(str(stat_id)) or {}
        data = mine.get("data", 0) if isinstance(mine, dict) else 0
        times = (mine.get("AchievementTimes") or {}) if isinstance(mine, dict) else {}
        for bit, ach in bits.items():
            if not isinstance(ach, dict):
                continue
            display = ach.get("display") or {}
            try:
                unlocked = bool((int(data) >> int(bit)) & 1)
            except (TypeError, ValueError):
                unlocked = False
            when = None
            if unlocked:
                try:
                    when = float(times.get(str(bit)) or 0)
                except (TypeError, ValueError):
                    when = 0.0
            out.items.append(Achievement(
                key=str(ach.get("name") or f"{stat_id}_{bit}"),
                name=_text(display.get("name"), lang) or str(ach.get("name") or ""),
                desc=_text(display.get("desc"), lang),
                hidden=str(display.get("hidden", "0")) == "1",
                icon=str(display.get("icon") or ""),
                icon_gray=str(display.get("icon_gray") or ""),
                unlocked_at=when))
    if not out.items:
        return None
    if rarity:
        percents = global_percentages(appid)
        for a in out.items:
            a.percent = percents.get(a.key)
    return out


def global_percentages(appid: str) -> Dict[str, float]:
    """API name -> % of players who have it (Steam's public numbers, kept a day)."""
    path = CACHE / f"rarity_{appid}.json"
    try:
        if time.time() - path.stat().st_mtime < RARITY_MAX_AGE:
            return {k: float(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, AttributeError):
        pass
    data = http_get_json(RARITY_URL.format(appid=appid), HEADERS, timeout=10, retries=2)
    found: Dict[str, float] = {}
    try:
        for item in (data or {}).get("achievementpercentages", {}).get("achievements", []):
            found[str(item["name"])] = float(item["percent"])
    except (KeyError, TypeError, ValueError, AttributeError):
        found = {}
    if found:
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(found), encoding="utf-8")
        except OSError as e:
            logger.debug(f"Achievements: couldn't keep the rarity of {appid}: {e}")
    elif path.exists():
        # offline: yesterday's numbers are better than none
        try:
            return {k: float(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
        except (OSError, ValueError, AttributeError):
            pass
    return found


def icon_file(appid: str, name: str) -> Optional[Path]:
    """The icon on disk - fetched the first time (call off the GTK thread):
    a Steam icon's file name, or a whole https:// address (GOG)."""
    if not name:
        return None
    if name.startswith("https://"):
        urls = (name,)
        ext = Path(urllib.parse.urlparse(name).path).suffix.lower()
        local = hashlib.sha1(name.encode()).hexdigest() + (ext if ext in (".jpg", ".jpeg", ".png", ".webp") else "")
    elif "/" in name or name.startswith("."):
        return None
    else:
        urls, local = tuple(u.format(appid=appid, name=name) for u in ICON_URLS), name
    path = CACHE / "icons" / appid.replace("/", "_") / local
    if path.is_file() and path.stat().st_size > 0:
        return path
    for url in urls:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read(2 * 1024 * 1024)
            if not body:
                continue
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(body)
            tmp.replace(path)
            return path
        except Exception as e:
            logger.debug(f"Achievements: no icon {name} for {appid} at {url}: {e}")
    return None


# --- GOG (Heroic) ------------------------------------------------------------------------

GALAXY_CLIENT = "46899977096215655"
_GOG_TOKENS: Dict[str, Dict] = {}          # client id -> token (in memory only)
_GOG_LOCK = threading.Lock()


def _heroic() -> Optional[Path]:
    from plunderhub.core.treasure import heroic_root
    return heroic_root()


def _gog_client(product_id: str) -> Optional[tuple]:
    """(client id, client secret) of an installed GOG game, from the build
    manifest Heroic's gogdl keeps."""
    root = _heroic()
    if root is None or not product_id.isdigit():
        return None
    path = root / "gogdlConfig" / "heroic_gogdl" / "manifests" / product_id
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cid, secret = data.get("clientId"), data.get("clientSecret")
    return (str(cid), str(secret)) if cid and secret else None


def _gog_galaxy() -> Optional[Dict]:
    root = _heroic()
    if root is None:
        return None
    try:
        auth = json.loads((root / "gog_store" / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    galaxy = auth.get(GALAXY_CLIENT) if isinstance(auth, dict) else None
    return galaxy if isinstance(galaxy, dict) and galaxy.get("refresh_token") else None


def _gog_token(cid: str, secret: str) -> Optional[Dict]:
    """A token for the game's client, as gogdl gets it (Heroic's sign-in untouched)."""
    with _GOG_LOCK:
        tok = _GOG_TOKENS.get(cid)
        if tok and time.time() < tok["at"] + float(tok.get("expires_in") or 0) - 60:
            return tok
        galaxy = _gog_galaxy()
        if galaxy is None:
            return None
        query = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "refresh_token",
                                        "refresh_token": galaxy["refresh_token"], "without_new_session": "1"})
        data = http_get_json(f"https://auth.gog.com/token?{query}", HEADERS, timeout=10, retries=2)
        if not isinstance(data, dict) or not data.get("access_token"):
            return None
        data["at"] = time.time()
        _GOG_TOKENS[cid] = data
        return data


def gog_has_achievements(product_id: str) -> bool:
    """Could be asked: installed through Heroic (its manifest) and signed in."""
    return _gog_client(product_id) is not None and _gog_galaxy() is not None


def gog_achievements(product_id: str, lang: str = "en") -> Optional[GameAchievements]:
    """The GOG game's achievements and the player's progress, rarity and
    dates included; None without any (or when GOG can't be asked)."""
    client = _gog_client(product_id)
    if client is None:
        return None
    tok = _gog_token(*client)
    if tok is None:
        return None
    user = str(tok.get("user_id") or (_gog_galaxy() or {}).get("user_id") or "")
    if not user:
        return None
    headers = {**HEADERS, "Authorization": f"Bearer {tok['access_token']}",
               "Accept-Language": {"pt_br": "pt-BR"}.get(lang, lang)}
    out = GameAchievements(f"gog_{product_id}")
    page = None
    for _ in range(20):
        url = f"https://gameplay.gog.com/clients/{client[0]}/users/{user}/achievements"
        if page:
            url += "?" + urllib.parse.urlencode({"page_token": page})
        data = http_get_json(url, headers, timeout=12, retries=2)
        if not isinstance(data, dict):
            break
        for item in data.get("items") or []:
            when = None
            if item.get("date_unlocked"):
                try:
                    from datetime import datetime
                    when = datetime.fromisoformat(str(item["date_unlocked"]).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    when = 0.0
            try:
                percent = float(item["rarity"]) if item.get("rarity") is not None else None
            except (TypeError, ValueError):
                percent = None
            out.items.append(Achievement(
                key=str(item.get("achievement_key") or item.get("achievement_id") or ""),
                name=str(item.get("name") or ""), desc=str(item.get("description") or ""),
                hidden=not item.get("visible", True),
                icon=str(item.get("image_url_unlocked") or ""), icon_gray=str(item.get("image_url_locked") or ""),
                unlocked_at=when, percent=percent))
        page = data.get("page_token") or data.get("next_page_token")
        if not page or len(out.items) >= int(data.get("total_count") or 0):
            break
    return out if out.items else None


# --- one way in, by the library's uid -------------------------------------------------

def source(uid: str) -> Optional[tuple]:
    """("steam", appid) / ("gog", product id) for a game whose achievements
    can be read; None for the rest (Epic, Amazon, Lutris, ...)."""
    if uid.startswith("steam:"):
        return "steam", uid.split(":", 1)[1]
    if uid.startswith("heroic:gog_store:"):
        return "gog", uid.rsplit(":", 1)[1]
    return None


def available(uid: str) -> bool:
    src = source(uid)
    if src is None:
        return False
    return has_achievements(src[1]) if src[0] == "steam" else gog_has_achievements(src[1])


def for_uid(uid: str, lang: str = "en", rarity: bool = True) -> Optional[GameAchievements]:
    """Call off the GTK thread."""
    src = source(uid)
    if src is None:
        return None
    if src[0] == "steam":
        return steam_achievements(src[1], lang, rarity=rarity)
    return gog_achievements(src[1], lang)
