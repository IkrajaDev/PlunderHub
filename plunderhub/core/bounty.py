# PlunderHub - a GTK3 game launcher for Linux
# Copyright (C) 2026 IkrajaDev
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""The Bounty Board: PC games free to keep right now, on Steam, Epic, GOG
and Humble - from LootScraper's feeds (https://eikowagenknecht.com/lootscraper/,
MIT; one Atom feed per store, read by many). Each offer: the game, its
store page, its picture, until when, the price it usually has.

Only offers running now are kept (one that ended, or not started yet,
isn't shown). Read at most every REFRESH_S, kept in the cache for when
there is no network; the pictures too."""

import hashlib
import json
import os
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from calendar import timegm
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from plunderhub.constants import CACHE_DIR
from plunderhub.logging_setup import logger

FEED = "https://feed.eikowagenknecht.com/lootscraper_{store}_game.xml"
STORES = ("epic", "steam", "gog", "humble")
STORE_NAMES = {"epic": "Epic Games", "steam": "Steam", "gog": "GOG", "humble": "Humble"}
REFRESH_S = 6 * 3600
CACHE_FILE = CACHE_DIR / "bounty.json"
ART_DIR = CACHE_DIR / "bounty_art"
USER_AGENT = "PlunderHub (https://github.com/IkrajaDev/PlunderHub)"
_ATOM = "{http://www.w3.org/2005/Atom}"
_XHTML = "{http://www.w3.org/1999/xhtml}"
_LOCK = threading.Lock()


@dataclass
class Offer:
    id: str
    store: str
    title: str
    url: str
    image: str = ""
    starts: Optional[float] = None
    ends: Optional[float] = None
    price: str = ""
    description: str = ""
    genres: List[str] = field(default_factory=list)
    seen: float = 0.0

    def running(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return (self.starts is None or self.starts <= now) and (self.ends is None or self.ends > now)


def _when(text: str) -> Optional[float]:
    """LootScraper's times ("2026-11-19 16:00"), UTC."""
    m = re.search(r"(\d{4})-(\d\d)-(\d\d)[ T](\d\d):(\d\d)", text or "")
    if not m:
        return None
    y, mo, d, h, mi = (int(x) for x in m.groups())
    return float(timegm((y, mo, d, h, mi, 0)))


def _text(node) -> str:
    return "".join(node.itertext()) if node is not None else ""


def parse(xml: bytes, store: str) -> List[Offer]:
    offers = []
    root = ET.fromstring(xml)
    for entry in root.findall(f"{_ATOM}entry"):
        cats = {c.get("term", "") for c in entry.findall(f"{_ATOM}category")}
        if any(t.startswith("platform:") for t in cats) and "platform:PC" not in cats:
            continue
        if any(t.startswith("type:") for t in cats) and "type:GAME" not in cats:
            continue
        title = _text(entry.find(f"{_ATOM}title"))
        # "Epic Games (Game, PC) - Knightica"
        title = re.sub(r"^.*?\)\s*-\s*", "", title, count=1).strip() or title
        link = entry.find(f"{_ATOM}link")
        url = link.get("href", "") if link is not None else ""
        content = entry.find(f"{_ATOM}content")
        image, starts, ends, price, description = "", None, None, "", ""
        if content is not None:
            img = content.find(f".//{_XHTML}img")
            image = img.get("src", "") if img is not None else ""
            for li in content.iter(f"{_XHTML}li"):
                line = _text(li).strip()
                if line.startswith("Offer valid from"):
                    starts = _when(line)
                elif line.startswith("Offer valid to"):
                    ends = _when(line)
                elif line.startswith("Recommended price"):
                    price = line.split(":", 1)[1].strip()
                elif line.startswith("Description"):
                    description = line.split(":", 1)[1].strip()
        genres = [c.get("label", "") for c in entry.findall(f"{_ATOM}category")
                  if c.get("term", "").startswith("Genre:")]
        offers.append(Offer(id=_text(entry.find(f"{_ATOM}id")) or url, store=store, title=title, url=url,
                            image=image, starts=starts, ends=ends, price=price, description=description,
                            genres=genres, seen=time.time()))
    return offers


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read(4 * 1024 * 1024)


def _load_cache() -> Dict:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def cached() -> List[Offer]:
    """What the last fetch found, still running now."""
    offers = []
    for item in _load_cache().get("offers") or []:
        try:
            offer = Offer(**item)
        except TypeError:
            continue
        if offer.running():
            offers.append(offer)
    return _sorted(offers)


def fetched_at() -> float:
    return float(_load_cache().get("at") or 0)


def _sorted(offers: List[Offer]) -> List[Offer]:
    # the one ending soonest first; no end date last
    return sorted(offers, key=lambda o: (o.ends is None, o.ends or 0, o.title.lower()))


def refresh(force: bool = False) -> Optional[List[Offer]]:
    """The running offers - fetched again when the cache is old (worker
    thread). None when nothing could be read and there is no cache."""
    with _LOCK:
        cache = _load_cache()
        if not force and time.time() - float(cache.get("at") or 0) < REFRESH_S and "offers" in cache:
            return cached()
        found: List[Offer] = []
        failed = []
        for store in STORES:
            try:
                found += parse(_fetch(FEED.format(store=store)), store)
            except Exception as e:
                failed.append(store)
                logger.info(f"Bounty Board: {store} feed not read: {e}")
        if len(failed) == len(STORES):
            return cached() if "offers" in cache else None
        if failed:
            # a store that didn't answer keeps what it had
            found += [Offer(**o) for o in cache.get("offers") or [] if o.get("store") in failed]
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
            tmp.write_text(json.dumps({"at": time.time(), "offers": [asdict(o) for o in found]}), encoding="utf-8")
            os.replace(tmp, CACHE_FILE)
        except OSError as e:
            logger.warning(f"Bounty Board cache not written: {e}")
        return _sorted([o for o in found if o.running()])


def art_path(offer: Offer) -> Optional[Path]:
    """The offer's picture on disk - downloaded the first time (worker
    thread); None when it has none or it can't be had."""
    if not offer.image.startswith("https://"):
        return None
    suffix = re.search(r"\.(png|jpe?g|webp|gif)(?:\?|$)", offer.image.lower())
    path = ART_DIR / (hashlib.sha1(offer.image.encode()).hexdigest()[:20] + "." + (suffix.group(1) if suffix else "img"))
    if path.is_file() and path.stat().st_size > 0:
        return path
    try:
        data = _fetch(offer.image)
        ART_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return path
    except Exception as e:
        logger.info(f"Bounty Board: picture of {offer.title} not loaded: {e}")
        return None


def tidy_art(keep: List[Offer]):
    """Pictures of offers long gone go (a week after they were last seen)."""
    wanted = {hashlib.sha1(o.image.encode()).hexdigest()[:20] for o in keep if o.image}
    try:
        for path in ART_DIR.iterdir():
            if path.name.split(".")[0] not in wanted and time.time() - path.stat().st_mtime > 7 * 86400:
                path.unlink(missing_ok=True)
    except OSError:
        pass


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower().replace("&", "and"))


def owned(offers: List[Offer], titles: List[str]) -> Dict[str, bool]:
    """Which offers' games are already in the library (by title)."""
    have = {_norm(t) for t in titles if t}
    return {o.id: _norm(o.title) in have for o in offers}
