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
"""A release's notes (GitHub Markdown) turned into blocks of Pango
markup for the release notes window - no GTK here, so it can be tested
on its own.

Only what Proton release notes use: headings, nested bullet and
numbered lists, paragraphs, code blocks and `code`, **bold** / *italic*,
links (shown short: #865, MR 11824, a commit's hash) and rules. Images,
HTML comments and tags are dropped; a table's rows become plain lines.

Game names from the user's library are picked out (mentions()) and
highlighted where they appear.
"""

import html
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Pattern, Tuple
from urllib.parse import urlparse

MAX_NOTES = 60_000          # characters kept per release (cache size)


@dataclass
class Block:
    kind: str               # "h" | "li" | "p" | "code" | "hr"
    markup: str = ""        # Pango markup (code: plain text)
    level: int = 0          # heading level 1-3, list depth 0-3
    bullet: str = ""        # "•", "◦", "1." ...
    games: Tuple[str, ...] = ()   # library games mentioned in it


# --- game mentions ------------------------------------------------------------------

_TRADEMARKS = re.compile(r"[™®©]")
# titles that are words in every changelog
_TOO_COMMON = {"proton", "steam", "wine", "dxvk", "vkd3d", "game", "games", "the game", "launcher",
               "update", "control", "prey", "inside", "limbo", "portal", "observer", "ruiner"}


def _forms(title: str) -> List[str]:
    """How a title may be written in notes: whole, and the part before a
    subtitle ("Target Bravo: Operation G.H.O.S.T." -> "Target Bravo")
    when that part is two words or more."""
    clean = _TRADEMARKS.sub("", title).strip()
    forms = [clean]
    for sep in (":", " - ", " – "):
        if sep in clean:
            head = clean.split(sep, 1)[0].strip()
            if len(head.split()) >= 2:
                forms.append(head)
    # "The Witcher 3" is written "Witcher 3" just as often
    forms += [f[4:] for f in list(forms) if f.lower().startswith("the ") and len(f.split()) >= 3]
    return [f for f in forms if len(f) >= 4 and f.lower() not in _TOO_COMMON]


def game_pattern(titles: Iterable[str]) -> Optional[Tuple[Pattern, dict]]:
    """One regex for every form of every title (longest first, so a
    subtitle wins over its head), and form (lower case) -> title."""
    owner = {}
    for title in titles:
        for form in _forms(title):
            owner.setdefault(form.lower(), title)
    if not owner:
        return None
    alts = sorted(owner, key=len, reverse=True)
    body = "|".join(re.escape(html.escape(a, quote=False)) for a in alts)
    return re.compile(rf"(?<![\w]){'(?:' + body + ')'}(?![\w])", re.IGNORECASE), owner


def mentions(blocks: List[Block]) -> List[str]:
    """The library games the notes mention, in order of first mention."""
    seen: List[str] = []
    for block in blocks:
        for game in block.games:
            if game not in seen:
                seen.append(game)
    return seen


# --- inline -------------------------------------------------------------------------

_CODE = re.compile(r"`([^`]+)`")
_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BARE_URL = re.compile(r"(?<![\"'(=])\bhttps?://[^\s<>()\[\]\x00]+[^\s<>()\[\].,;:!?'\"\x00]")
_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC = re.compile(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_TAG = re.compile(r"<[^>\n]+>")
_HOLE = re.compile(r"\x00(\d+)\x00")


def short_link(url: str, repo: str = "") -> str:
    """A link's text when the notes show the bare URL: #865 for an issue
    or pull request of the release's own repo, owner/repo#36 for another
    one's, MR 11824 on a GitLab, a commit's first 7 characters, else the
    site's name."""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    path = [p for p in parts.path.split("/") if p]
    host = parts.netloc.lower()
    if host == "github.com" and len(path) >= 4 and path[2] in ("issues", "pull", "discussions") \
            and path[3].isdigit():
        own = "/".join(path[:2])
        return f"#{path[3]}" if own.lower() == repo.lower() else f"{own}#{path[3]}"
    if host == "github.com" and len(path) >= 4 and path[2] == "commit":
        return path[3][:7]
    if host == "github.com" and len(path) >= 5 and path[2] in ("releases",) and path[3] == "tag":
        return path[4]
    if "merge_requests" in path:
        idx = path.index("merge_requests")
        if idx + 1 < len(path) and path[idx + 1].isdigit():
            return f"MR {path[idx + 1]}"
    if host == "github.com" and len(path) >= 2:
        return "/".join(path[:2])
    return host.removeprefix("www.") or url


def _link_markup(text: str, url: str, accent: str) -> str:
    return (f'<a href="{html.escape(url, quote=True)}"><span foreground="{accent}" underline="none">'
            f"{text}</span></a>")


def inline(text: str, accent: str, repo: str = "",
           games: Optional[Tuple[Pattern, dict]] = None) -> Tuple[str, Tuple[str, ...]]:
    """One line or paragraph of Markdown as Pango markup, and the games
    it mentions."""
    holes: List[str] = []

    def hole(markup: str) -> str:
        holes.append(markup)
        return f"\x00{len(holes) - 1}\x00"

    text = _CODE.sub(lambda m: hole(
        f'<span font_family="monospace" background="#80808033">{html.escape(m.group(1), quote=False)}</span>'),
        text)

    def link(m):
        if m.group(0).startswith("!"):          # an image: its alt text, if any
            return m.group(1)
        label = m.group(1).strip() or short_link(m.group(2), repo)
        # a label that's itself `code` keeps its look
        label_markup = _HOLE.sub(lambda h: holes[int(h.group(1))], html.escape(label, quote=False)) \
            if "\x00" in label else html.escape(label, quote=False)
        return hole(_link_markup(label_markup, m.group(2), accent))

    text = _LINK.sub(link, text)
    text = _AUTOLINK.sub(lambda m: hole(_link_markup(html.escape(short_link(m.group(1), repo)),
                                                     m.group(1), accent)), text)
    text = _BARE_URL.sub(lambda m: hole(_link_markup(html.escape(short_link(m.group(0), repo)),
                                                     m.group(0), accent)), text)
    text = _TAG.sub("", text)                    # <br>, <details> and the like
    text = html.unescape(text)                   # &amp; written in the Markdown
    text = html.escape(text, quote=False)

    found: List[str] = []
    if games is not None:
        pattern, owner = games

        def mark(m):
            game = owner.get(html.unescape(m.group(0)).lower())
            if game and game not in found:
                found.append(game)
            return hole(f'<span foreground="{accent}" weight="bold">{m.group(0)}</span>')

        text = pattern.sub(mark, text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(2)}</b>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = _STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    # holes inside holes (a game's name in a link label) fill in turn
    for _ in range(3):
        if "\x00" not in text:
            break
        text = _HOLE.sub(lambda m: holes[int(m.group(1))], text)
    return text, tuple(found)


# --- blocks -------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_NUMBER = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BULLETS = ("•", "◦", "▪", "▫")


def _depth(indent: str) -> int:
    return min(len(indent.replace("\t", "    ")) // 2, 3)


def parse(markdown: str, accent: str, repo: str = "",
          games: Optional[Tuple[Pattern, dict]] = None) -> List[Block]:
    """The notes as blocks, ready for labels."""
    text = _COMMENT.sub("", (markdown or "")[:MAX_NOTES]).replace("\r\n", "\n")
    blocks: List[Block] = []
    para: List[str] = []
    item: Optional[Tuple[int, str, List[str]]] = None    # depth, bullet, lines
    code: Optional[List[str]] = None

    def make(kind, raw, level=0, bullet=""):
        markup, found = inline(raw, accent, repo, games)
        if markup.strip():
            blocks.append(Block(kind, markup, level, bullet, found))

    def flush():
        nonlocal item, para
        if item is not None:
            make("li", " ".join(item[2]), item[0], item[1])
            item = None
        if para:
            make("p", " ".join(para))
            para = []

    for line in text.split("\n"):
        if code is not None:
            if _FENCE.match(line):
                blocks.append(Block("code", "\n".join(code).rstrip("\n")))
                code = None
            else:
                code.append(line)
            continue
        if _FENCE.match(line):
            flush()
            code = []
            continue
        if not line.strip():
            flush()
            continue
        if _RULE.match(line):
            flush()
            blocks.append(Block("hr"))
            continue
        m = _HEADING.match(line)
        if m:
            flush()
            make("h", m.group(2), min(len(m.group(1)), 3))
            continue
        if _TABLE_SEP.match(line):
            continue
        if line.lstrip().startswith("|"):
            flush()
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            make("p", "  ·  ".join(c for c in cells if c))
            continue
        m = _BULLET.match(line) or _NUMBER.match(line)
        if m:
            flush()
            depth = _depth(m.group(1))
            bullet = f"{m.group(2)}." if m.group(2).isdigit() else _BULLETS[depth]
            item = (depth, bullet, [m.group(3).strip()])
            continue
        if item is not None and (line.startswith(" ") or line.startswith("\t") or not para):
            # a list item's wrapped line (GE's notes break long items)
            item[2].append(line.strip())
            continue
        para.append(line.strip())
    if code is not None:
        blocks.append(Block("code", "\n".join(code).rstrip("\n")))
    flush()
    # a first heading that only repeats the tag says nothing new
    return blocks


def drop_title(blocks: List[Block], tag: str) -> List[Block]:
    """Without a leading heading that is just the release's own name
    (GE's notes start with "# GE-Proton11-12") - the window shows it."""
    if blocks and blocks[0].kind == "h" and html.unescape(re.sub(r"<[^>]+>", "", blocks[0].markup)).strip() == tag:
        return blocks[1:]
    return blocks


def is_hotfix(markdown: str) -> bool:
    head = (markdown or "")[:400].lower()
    return "hotfix" in head or "bugfix release" in head
