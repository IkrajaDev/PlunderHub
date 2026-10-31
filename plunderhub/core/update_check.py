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
from typing import Optional, Tuple

from plunderhub.core.utils import http_get_json
from plunderhub.logging_setup import logger

# "IkrajaDev/PlunderHub", not a full URL - reused to build both the API
# endpoint and the browser-facing release page link.
GITHUB_REPO = "IkrajaDev/PlunderHub"

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_version(version_str: str) -> Optional[Tuple[int, int, int]]:
    """Pulls a (major, minor, patch) int triple out of a tag like
    "v0.6.39-beta" or "0.6.39" - ignores any "-beta"/"-rc1"/etc. suffix
    and a leading "v", since that's all every tag this project has
    ever used looks like. No external dependency (e.g. `packaging`)
    needed for something this simple.

    Returns None if nothing matching a X.Y.Z pattern can be found, so
    a malformed or unexpected tag never crashes the caller - it just
    means "can't tell, so don't claim there's an update"."""
    if not version_str:
        return None
    match = _VERSION_RE.search(version_str)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _version_key(version_str: str) -> Optional[Tuple[int, int, int, int]]:
    """parse_version plus whether it's a final release: a "-beta"/"-rc1"
    pre-release sorts before the final release of the same number, so
    someone on 0.8.0-beta is told when 0.8.0 itself comes out. Two
    pre-releases of the same number compare equal."""
    parsed = parse_version(version_str)
    if parsed is None:
        return None
    match = _VERSION_RE.search(version_str)
    is_prerelease = version_str[match.end():].startswith("-")
    return (*parsed, 0 if is_prerelease else 1)


def is_newer_version(remote: str, local: str) -> bool:
    """True only if remote parses AND is strictly greater than local.
    Any parse failure (unexpected tag format on either side) resolves
    to False - silently saying "no update" beats wrongly claiming one
    exists, or crashing the whole check over a malformed tag."""
    remote_key = _version_key(remote)
    local_key = _version_key(local)
    if remote_key is None or local_key is None:
        return False
    return remote_key > local_key


def fetch_latest_release_tag() -> Optional[str]:
    """Blocking - call this from a background thread, never the GTK
    main thread. Returns the tag_name of the repo's latest GitHub
    Release (e.g. "v0.6.40-beta"), or None if the request failed or
    the repo has no releases yet. No authentication - GitHub allows
    60 unauthenticated requests/hour per IP, comfortably more than a
    once-a-day automatic check plus the occasional manual one needs."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    data = http_get_json(url, {"Accept": "application/vnd.github+json"}, timeout=10)
    if not data:
        return None
    tag = data.get("tag_name")
    if not tag or not isinstance(tag, str):
        logger.warning(f"GitHub releases/latest response had no usable tag_name: {data!r}")
        return None
    return tag


def release_page_url(tag: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}"
