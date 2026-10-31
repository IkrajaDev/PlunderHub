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

"""Takes personal details out of text before it is written to the log or
copied for a bug report: the user name and home folder, the computer's
name, account ids (Steam), e-mail addresses, keys and tokens (the
SteamGridDB key, a key= in a URL, a Bearer header, a password in a URL).
Every log line goes through scrub(), so the log never holds them and can
be shared as it is."""

import getpass
import os
import re
import socket
import threading
from pathlib import Path
from typing import List, Optional, Pattern, Set, Tuple

HIDDEN = "<hidden>"

# Names too common to tell anyone apart - replacing them would only spoil
# the log ("Steam Deck", "games", ...).
_GENERIC_NAMES = {
    "root", "user", "users", "admin", "steam", "deck", "steamdeck", "games", "game", "gamer",
    "player", "linux", "localhost", "home", "media", "test", "guest", "live", "liveuser", "pi",
    "default", "computer", "desktop", "laptop", "pc", "flatpak", "wine", "proton",
}

_lock = threading.Lock()
_secrets: Set[str] = set()
_rules: Optional[List[Tuple[Pattern, str]]] = None


def add_secret(value: Optional[str]) -> None:
    """A value (an API key) that must never show in the log."""
    if value and len(value.strip()) >= 6:
        with _lock:
            _secrets.add(value.strip())


def _os_release_words() -> Set[str]:
    words: Set[str] = set()
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip() in ("ID", "ID_LIKE", "VARIANT_ID"):
                words.update(value.strip().strip('"').lower().split())
        break
    return words


def _personal_names() -> List[Tuple[str, str]]:
    """(name, placeholder) for the user and the computer, when they could
    tell someone apart."""
    skip = _GENERIC_NAMES | _os_release_words()
    names = []
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER", "")
    try:
        host = socket.gethostname().split(".")[0]
    except Exception:
        host = ""
    for name, placeholder in ((user, "<user>"), (host, "<host>")):
        if name and len(name) >= 3 and name.lower() not in skip:
            names.append((name, placeholder))
    return names


def _build_rules() -> List[Tuple[Pattern, str]]:
    rules: List[Tuple[Pattern, str]] = []
    homes = {str(Path.home())}
    try:
        homes.add(os.path.realpath(str(Path.home())))
    except OSError:
        pass
    end = r"(?=[/\\\s'\",:;)\]}>]|$)"
    for home in sorted((h.rstrip("/") for h in homes if h and h != "/"), key=len, reverse=True):
        rules.append((re.compile(re.escape(home) + end), "~"))
    # someone else's home, a removable drive's mount point
    rules.append((re.compile(r"(/(?:var/)?home/)(?!<user>)[^/\s'\"]+"), r"\1<user>"))
    rules.append((re.compile(r"(/run/media/|/media/)(?!<user>)[^/\s'\"]+"), r"\1<user>"))
    # Steam: userdata/<account id>, 64-bit Steam ids
    rules.append((re.compile(r"(userdata[/\\])\d+"), r"\1<id>"))
    rules.append((re.compile(r"\b7656119\d{10}\b"), "<steam-id>"))
    # a password in a URL, keys and tokens in a query or a header
    rules.append((re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1" + HIDDEN + "@"))
    rules.append((re.compile(
        r"([?&](?:api[_-]?key|key|token|access_token|refresh_token|auth|code|password|secret|sig|signature)=)"
        r"[^&\s#'\"]+", re.IGNORECASE), r"\1" + HIDDEN))
    rules.append((re.compile(r"(\b(?:Bearer|Basic|token)\s+)[A-Za-z0-9._~+/=-]{6,}"), r"\1" + HIDDEN))
    rules.append((re.compile(
        r"""(["']?(?:api[_-]?key|access_token|refresh_token|password|secret|steamgriddb_api_key)["']?\s*[:=]\s*["']?)"""
        r"""[^"'\s,}]+""", re.IGNORECASE), r"\1" + HIDDEN))
    # e-mail addresses (not git@host: of a clone URL)
    rules.append((re.compile(r"\b(?!git@)[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
                  "<email>"))
    for name, placeholder in _personal_names():
        rules.append((re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])"), placeholder))
    return rules


def scrub(text: str) -> str:
    """text with every personal detail replaced by a placeholder."""
    global _rules
    if not text:
        return text
    if _rules is None:
        with _lock:
            if _rules is None:
                _rules = _build_rules()
    with _lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, HIDDEN)
    for pattern, replacement in _rules:
        text = pattern.sub(replacement, text)
    return text
