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
"""Which of the library's games a NEWER Proton release talks about than
the one the game runs on - "GE-Proton11-12 mentions this game, you're on
11-10". No GTK, no network: the release lists as last fetched
(proton_store.cached_releases), the game's Proton as its launcher has it.

A mention isn't always a fix ("removed the X override", "X still
crashes"), so the sentence itself is kept to be shown; `fix` says it
reads as one (is_fix: "fixed", "works again", "no longer crashes"...).

    hard  the game is pinned to an older Proton-GE / Proton-CachyOS build:
          the card's badge, the game page's strip, the Proton page's pill
    soft  it runs on another Proton (Valve's, the launcher's default) and
          a recent Proton-GE release mentions it: a line in Launch options
"""

import html
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from plunderhub.core import release_notes as rn

SOFT_RECENT = 6          # the newest Proton-GE releases looked at for a soft hint
OLDER_MAX = 40           # releases looked at when the game's own isn't in the list
EXCERPT_MAX = 220


@dataclass
class Mention:
    family: str                 # "ge" | "cachyos"
    tag: str                    # the newest release that mentions the game
    date: str
    excerpt: str                # its sentence, plain text
    fix: bool                   # the sentence reads as a fix (is_fix)
    current: str                # the build the game runs on ("" unknown)
    soft: bool                  # not on a Proton-GE / CachyOS build of its own
    count: int = 1              # newer releases mentioning it
    on_latest: bool = False     # the game is on its family's Latest - which is behind
    latest_tag: str = ""        # the release the family's Latest holds ("" not installed)
    latest_behind: bool = False  # that Latest is older than tag (or missing): update it first

    @property
    def action(self) -> Optional[str]:
        """What gets the game the newer release: "switch" (to the Latest),
        "update_switch" (update the Latest, then switch), "download_switch"
        (no Latest yet), "update" (it's on the Latest, which is behind);
        None for a soft hint."""
        if self.soft:
            return None
        if self.on_latest:
            return "update"
        if not self.latest_tag:
            return "download_switch"
        return "update_switch" if self.latest_behind else "switch"


def _plain(markup: str) -> str:
    text = re.sub(r"<[^>]+>", "", markup)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _excerpt(text: str) -> str:
    return text if len(text) <= EXCERPT_MAX else text[:EXCERPT_MAX - 1].rstrip() + "…"


def _mention_in(release, title: str) -> Optional[str]:
    """The release's sentence naming the game, or None."""
    notes = getattr(release, "notes", "")
    forms = rn._forms(title)
    # most releases don't name the game at all: a plain look first
    if not notes or not any(f.lower() in notes.lower() for f in forms):
        return None
    pattern = rn.game_pattern([title])
    if pattern is None:
        return None
    for block in rn.parse(release.notes, "#000000", "", pattern):
        if block.games and block.kind in ("li", "p"):
            return _plain(block.markup)
    return None


# what a fix sounds like in release notes - anywhere in the sentence, as a
# whole word ("protonfixes", "hotfix" don't count)
_FIX = re.compile(
    r"\b(fix|fixes|fixed|fixing)\b"
    r"|\b(works?|running|launch(es)?|starts?|plays?)\b[^.;]{0,40}?\bagain\b"
    r"|\bno longer (crash|freez|hang|stutter|fail|break|black|flicker|desync|kick|get)"
    r"|\b(resolved?|repaired|corrected|regression)\b"
    r"|\bnow (works|launches|starts|runs|loads|plays)\b",
    re.IGNORECASE)


def is_fix(sentence: str) -> bool:
    """Whether the sentence reads as a fix (the card says "Proton fix"),
    not just news about the game ("Proton news")."""
    return bool(_FIX.search(sentence or ""))


def build_of(name: str, builds: Sequence) -> Optional[tuple]:
    """(family, tag, latest) of the build a launcher lists as name, if it's
    a Proton-GE / Proton-CachyOS one: PlunderHub's own (its marker says
    which release), else a GE-Proton* folder named after its tag."""
    for build in builds:
        if getattr(build, "name", None) == name:
            return build.family, build.tag, bool(getattr(build, "latest", False))
    if re.match(r"^GE-Proton\d", name or ""):
        return "ge", name, False
    return None


def latest_tag(family: str, builds: Sequence) -> str:
    return next((b.tag for b in builds if getattr(b, "family", "") == family
                 and getattr(b, "latest", False) and getattr(b, "tag", "")), "")


def find(title: str, current: str, releases: Dict[str, Sequence], builds: Sequence = ()) -> Optional[Mention]:
    """The newest release newer than the game's build that mentions it -
    for a game on its family's Latest: newer than the release the Latest
    holds (it moves on by itself, but may not have yet)."""
    if not title:
        return None
    own = build_of(current, builds)
    on_latest = False
    if own is not None:
        family, tag, latest = own
        on_latest = latest
        listed = [r for r in releases.get(family, ()) if getattr(r, "tag", "")]
        tags = [r.tag for r in listed]
        newer = listed[:tags.index(tag)] if tag in tags else listed[:OLDER_MAX]
        soft = False
    else:
        family = "ge"
        newer = [r for r in releases.get("ge", ()) if getattr(r, "tag", "")][:SOFT_RECENT]
        soft = True
    found = [(r, s) for r in newer for s in [_mention_in(r, title)] if s]
    if not found:
        return None
    release, sentence = found[0]
    holds = latest_tag(family, builds)
    order = [r.tag for r in releases.get(family, ()) if getattr(r, "tag", "")]
    # newer = nearer the top of the list; a Latest whose release isn't listed counts as behind
    behind = not holds or holds not in order or order.index(holds) > order.index(release.tag)
    return Mention(family, release.tag, getattr(release, "date", ""), _excerpt(sentence),
                   is_fix(sentence), current, soft, len(found),
                   on_latest, holds, behind)
