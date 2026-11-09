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


import os
from pathlib import Path
from typing import Dict, List


APP_NAME = "PlunderHub"
APP_VERSION = "1.0.6"
# "beta" puts a Beta badge on the home screen, About and Settings; "" for a
# stable release.
APP_CHANNEL = "beta"
APP_AUTHOR = "Ikraja"
BUG_REPORT_URL = "https://github.com/IkrajaDev/PlunderHub/issues/new"
# for a bug report without a GitHub account
BUG_REPORT_EMAIL = "PlunderHub@proton.me"
KOFI_URL = "https://ko-fi.com/ikraja"

REVEAL_STAGGER_MS = 18
REVEAL_STAGGER_MAX_MS = 500
REVEAL_FADE_DURATION_MS = 220
REVEAL_FADE_STEP_MS = 20

# ezeket ne piszkáld, sokáig kalibráltam kézzel
BUBBLE_COUNT = 34
BUBBLE_MIN_RADIUS = 11.0
BUBBLE_MAX_RADIUS = 31.0
BUBBLE_MIN_ALPHA = 0.16
BUBBLE_MAX_ALPHA = 0.34
BUBBLE_OPACITY = 0.4
BUBBLE_REFERENCE_FPS = 60.0
BUBBLE_BASE_RISE_SPEED = -0.22
BUBBLE_SCROLL_RISE_SPEED = -15.2
BUBBLE_SCROLL_SINK_SPEED = 12.4
BUBBLE_EASE = 0.16
BUBBLE_SCROLL_ACTIVE_WINDOW = 0.2

APP_ICON_NAME = "PlunderHub"
APP_ID = "io.github.ikraja.PlunderHub"
def _config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate
    return Path.home() / ".config"


CONFIG_DIR = _config_home() / "plunderhub"


def _cache_home() -> Path:
    raw = os.environ.get("XDG_CACHE_HOME", "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            return candidate
    return Path.home() / ".cache"


CACHE_DIR = _cache_home() / "plunderhub"
HIDDEN_FILE = CONFIG_DIR / "hidden.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
FAVORITES_FILE = CONFIG_DIR / "favorites.json"
RECENT_FILE = CONFIG_DIR / "recent.json"
PLAYTIME_FILE = CONFIG_DIR / "playtime.json"
PLAYTIME_HISTORY_FILE = CONFIG_DIR / "playtime_history.json"
FIRST_SEEN_FILE = CONFIG_DIR / "first_seen.json"
# Downloaded cover/icon art lives under CONFIG_DIR, not CACHE_DIR - so it
# rides along with settings.json/first_seen.json etc. in "Sink the config"
# and in an exported backup, and can't get silently wiped out from under
# them by an unrelated cache-cleaning tool (which would have left behind
# a first_seen.json remembering games as "already seen", so the missing
# art would never have been re-downloaded automatically either).
COVERS_DIR = CONFIG_DIR / "covers"
ICONS_DIR = CONFIG_DIR / "icons"
GAME_ICONS_DIR = CONFIG_DIR / "game_icons"
HEROES_DIR = CONFIG_DIR / "heroes"         # the wide banners from SteamGridDB
COVER_MAP_FILE = CONFIG_DIR / "cover_map.json"
ICON_MAP_FILE = CONFIG_DIR / "icon_map.json"
GAME_ICON_MAP_FILE = CONFIG_DIR / "game_icon_map.json"
HERO_MAP_FILE = CONFIG_DIR / "hero_map.json"
KNOWN_TITLES_FILE = CONFIG_DIR / "known_titles.json"
GAME_ICON_SIZE_LABEL_KEYS: Dict[int, str] = {
    16: "icon_size_tiny",
    24: "icon_size_xsmall",
    32: "icon_size_small",
    48: "icon_size_medium",
    64: "icon_size_large",
    96: "icon_size_xlarge",
    128: "icon_size_huge",
}
LOG_FILE = CONFIG_DIR / "plunderhub.log"

COVER_SIZE_LABEL_KEYS: Dict[int, str] = {
    200: "cover_size_tiny",
    250: "cover_size_xsmall",
    300: "cover_size_small",
    350: "cover_size_medium",
    400: "cover_size_large",
    450: "cover_size_xlarge",
    500: "cover_size_huge",
}

CONSOLE_UI_SIZE_LABEL_KEYS: Dict[int, str] = {
    100: "ui_size_tiny",
    150: "ui_size_xsmall",
    200: "ui_size_small",
    250: "ui_size_medium",
    300: "ui_size_large",
    350: "ui_size_xlarge",
    400: "ui_size_huge",
}
CONSOLE_UI_SCALE_CSS_PERCENT: Dict[int, int] = {
    100: 70,
    150: 85,
    200: 100,
    250: 115,
    300: 130,
    350: 145,
    400: 160,
}

FONT_SCALE_ORDER: List[str] = ["tiny", "xsmall", "small", "medium", "large", "xlarge", "huge"]
FONT_SCALE_CSS_PERCENT: Dict[str, int] = {
    "tiny": 77,
    "xsmall": 94,
    "small": 111,
    "medium": 128,
    "large": 145,
    "xlarge": 162,
    "huge": 179,
}
FONT_SCALE_LABEL_KEYS: Dict[str, str] = {
    "tiny": "font_size_tiny",
    "xsmall": "font_size_xsmall",
    "small": "font_size_small",
    "medium": "font_size_medium",
    "large": "font_size_large",
    "xlarge": "font_size_xlarge",
    "huge": "font_size_huge",
}

RESOLUTION_TIER_ORDER: List[str] = ["tiny", "xsmall", "small", "medium", "large"]
RESOLUTION_TIER_COVER_SIZE: Dict[str, int] = {
    "tiny": 200, "xsmall": 250, "small": 300, "medium": 350, "large": 400,
}
RESOLUTION_TIER_CONSOLE_UI_SCALE: Dict[str, int] = {
    "tiny": 100, "xsmall": 150, "small": 200, "medium": 250, "large": 300,
}
RESOLUTION_TIER_FONT_SCALE: Dict[str, str] = {
    "tiny": "tiny", "xsmall": "xsmall", "small": "small", "medium": "medium", "large": "large",
}
RESOLUTION_TIER_GAME_ICON_SIZE: Dict[str, int] = {
    "tiny": 16, "xsmall": 24, "small": 32, "medium": 48, "large": 64,
}


CONTROLLER_GLYPH_TEXT = {
    "xbox": {"activate": "A", "back": "B", "sort": "Y", "favorite": "X", "prev_tab": "LB", "next_tab": "RB",
             "settings": "View", "toggle": "Start"},
    "sony": {"activate": "✕", "back": "○", "sort": "△", "favorite": "□", "prev_tab": "L1", "next_tab": "R1",
             "settings": "Share", "toggle": "Options"},
    # no controller in use: Console Mode's own keys (App._on_window_key_press)
    "keyboard": {"activate": "Enter", "back": "Esc", "sort": "S", "favorite": "F", "prev_tab": "Q", "next_tab": "E"},
}
