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


import atexit
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import yaml

from plunderhub.constants import (
    APP_AUTHOR, APP_ICON_NAME, APP_ID, APP_NAME, APP_VERSION, KOFI_URL,
    CONSOLE_UI_SCALE_CSS_PERCENT, COVERS_DIR,
    FONT_SCALE_CSS_PERCENT, FONT_SCALE_ORDER,
    GAME_ICONS_DIR, GAME_ICON_SIZE_LABEL_KEYS, HEROES_DIR, ICONS_DIR,
    RESOLUTION_TIER_CONSOLE_UI_SCALE, RESOLUTION_TIER_COVER_SIZE, RESOLUTION_TIER_GAME_ICON_SIZE,
    RESOLUTION_TIER_FONT_SCALE, REVEAL_FADE_DURATION_MS, REVEAL_FADE_STEP_MS,
)
from plunderhub.core.models import GameItem, resolve_protondb_url, resolve_store_url
from plunderhub.core.steam_launch_status import STALL_SECONDS
from plunderhub.core.heroic_cloud import CloudSaves, cloud_saves_all, run_sync as run_cloud_sync
from plunderhub.core.save_backup import back_up as back_up_saves, list_backups as list_save_backups
from plunderhub.core.save_backup import backup_dir as save_backup_dir, restore as restore_saves
from plunderhub.core.launch_options import SteamBackend, launcher_pids
from plunderhub.core.heroic_launch import HeroicLaunch, HeroicLaunchStage, heroic_running, updates_before_launch
from plunderhub.core.process_probe import ProcessProbe
from plunderhub.core.workspaces import WorkspaceMover, detect_compositor
from plunderhub.core.running_detector import Detection, RunningDetector, build_targets
from plunderhub.core import secret_store
from plunderhub.core.persistence import (
    migrate_cache_dirs,
    load_cover_map, load_favorites, load_first_seen, load_game_icon_map, load_hero_map, load_icon_map,
    load_hidden, load_json, load_known_titles, load_playtime, load_playtime_history, load_recent, load_settings,
    save_favorites, save_first_seen, save_icon_map,
    save_known_titles, save_playtime, save_playtime_history, save_recent, save_settings,
)
from plunderhub.core.scan_paths import (
    HEROIC_CONFIG_ROOTS, LAUNCHER_FLATPAK_IDS, bottles_library_path, faugus_games_path,
    lutris_db_path, lutris_game_config_path, resolve_launcher_invocation,
)
from plunderhub.core.scanner import (
    Scanner, _APPMANIFEST_RE, bottles_program_executables, read_faugus_playtimes, read_lutris_config_id,
    read_lutris_playtimes,
)
from plunderhub.core.steam_heroic import (
    heroic_roots, is_steam_appid_running, read_heroic_playtimes,
    read_steam_playtimes, steam_libraries, steam_library_paths, steam_localconfig_path, steam_roots,
)
from plunderhub.core.utils import (
    HEADERS, GameProcessMatch, find_game_pids, format_bytes, http_get_json, is_game_process_running,
    process_start_time, run, split_minutes_by_day,
)
from plunderhub.core.update_check import (
    fetch_latest_release_tag, is_newer_version, release_page_url,
)
from plunderhub.gi_setup import AppIndicator3, Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, _INDICATOR_BACKEND, logical_size, pixbuf_at_size, theme_icon_pixbuf
from plunderhub.i18n import AI_TRANSLATED_LANGUAGES, LANGUAGE_NATIVE_NAMES, LANGUAGE_ORDER, STRINGS
from plunderhub.icons import get_icon_pixbuf
from plunderhub.input.joystick import JoystickManager
from plunderhub.launchers.registry import (
    LAUNCHER_ICON_NAMES, LAUNCHER_REGISTRY, bundled_launcher_icon,
)
from plunderhub import diagnostics, privacy
from plunderhub.logging_setup import install_exception_logging, logger
from plunderhub.theme import (
    DEFAULT_ACCENT, DEFAULT_ACCENT_CHECK, DEFAULT_ACCENT_RGB, DEFAULT_ACCENT_SOFT,
    DEFAULT_ACCENT_SOFT_HOVER, FONT_FAMILY, THEME_ORDER, THEME_PALETTES, THEME_RENAMES, _accent_css,
    LOGO_MARKER, _app_icon_file, _app_icon_name, _app_icon_spec, _build_theme_css, mark_logo, set_logo_accent,
    DANGER_FG, _capture_original_theme_preference, accent_text_color, settings_css_for_system,
    register_bundled_fonts,
)
from plunderhub.ui.options_mixin import OptionsMixin
from plunderhub.ui.launch_options_mixin import LaunchOptionsMixin
from plunderhub.ui.console_mode_mixin import ConsoleModeMixin
from plunderhub.ui.covers_mixin import CoversMixin
from plunderhub.ui.animated_covers import AnimatedCoverPlayer
from plunderhub.ui.backup_mixin import BackupMixin
from plunderhub.ui.bubbles import BubbleField
from plunderhub.ui import memory
from plunderhub.ui.dialogs_mixin import DialogsMixin
from plunderhub.ui.playtime_mixin import PlaytimeMixin
from plunderhub.ui.home_mixin import HomeMixin, _treasure_map
from plunderhub.ui.progress_card import card_dialog
from plunderhub.ui.rendering_mixin import RenderingMixin
from plunderhub.ui.self_update_mixin import SelfUpdateMixin


def _natural_key(text: str) -> List:
    """Sorts "Game 2" before "Game 10"."""
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", text)]


@dataclass
class SaveChoice:
    """A game's save folders in one install of its launcher."""
    variant: str                            # native or flatpak
    locations: List[Tuple[str, str]]        # (name, folder)
    cloud: Optional[CloudSaves] = None      # the Heroic syncing them


class App(OptionsMixin, LaunchOptionsMixin, ConsoleModeMixin, PlaytimeMixin, HomeMixin, RenderingMixin,
          CoversMixin, DialogsMixin, BackupMixin, SelfUpdateMixin, Gtk.Window):
    # Edit order's keys besides the arrows (see _reorder_input)
    REORDER_KEY_ACTIONS = {
        Gdk.KEY_Return: "activate", Gdk.KEY_KP_Enter: "activate", Gdk.KEY_ISO_Enter: "activate",
        Gdk.KEY_space: "activate", Gdk.KEY_Escape: "back", Gdk.KEY_Home: "home", Gdk.KEY_End: "end",
    }
    ARROW_KEY_DIRECTIONS = {
        Gdk.KEY_Left: "left", Gdk.KEY_KP_Left: "left",
        Gdk.KEY_Right: "right", Gdk.KEY_KP_Right: "right",
        Gdk.KEY_Up: "up", Gdk.KEY_KP_Up: "up",
        Gdk.KEY_Down: "down", Gdk.KEY_KP_Down: "down",
    }

    def _fit_default_size(self, width: int, height: int):
        """The main window at width x height - smaller on a screen that has
        no room for it (a 1080p laptop at 125 %: 1536 x 864 logical pixels),
        and then maximized, so it never opens past the screen's edges."""
        try:
            display = Gdk.Display.get_default()
            monitor = display.get_primary_monitor() or display.get_monitor(0) if display else None
            if monitor is not None:
                work = monitor.get_workarea()
                if work.width > 0 and work.height > 0 and (width > work.width - 40 or height > work.height - 40):
                    width = max(640, min(width, work.width - 40))
                    height = max(480, min(height, work.height - 40))
                    self.maximize()
        except Exception as e:
            logger.debug(f"Main window: couldn't read the monitor size: {e}")
        self.set_default_size(width, height)

    def _fit_bubbles_to_screen(self):
        """The bubbles' size follows the monitor's height (see
        BubbleField.set_screen_height): the one the window is on once it's
        shown, the primary one before."""
        try:
            display = Gdk.Display.get_default()
            if display is None:
                return
            gdk_window = self.get_window()
            monitor = (display.get_monitor_at_window(gdk_window) if gdk_window is not None else None) \
                or display.get_primary_monitor() or display.get_monitor(0)
            if monitor is not None:
                self._bubble_field.set_screen_height(monitor.get_geometry().height)
                self._redraw_all_bubbles()
        except Exception as e:
            logger.debug(f"Bubbles: couldn't read the monitor size: {e}")

    def __init__(self):
        super().__init__(title=APP_NAME)
        register_bundled_fonts()
        self._fit_default_size(1620, 900)
        self.set_border_width(0)
        self._apply_window_icon()

        self.settings = load_settings()
        self._options_dialog: Optional[Gtk.Dialog] = None
        self._options_dialog_goto_tab: Optional[Callable] = None
        # The Settings tab last open, for this run only: a fresh start opens General.
        self._settings_last_tab: Optional[str] = None
        self._active_picker_dialog: Optional[Gtk.Dialog] = None
        self._theme_css_provider: Optional[Gtk.CssProvider] = None
        self._load_settings_into_attrs()
        self._maybe_prompt_first_run_language()
        _capture_original_theme_preference()
        self._bubble_field = BubbleField()
        self._fit_bubbles_to_screen()
        self.connect("map", lambda *_: self._fit_bubbles_to_screen())
        # the screen it's shown on decides the compact top bar (built before)
        self.connect("map", lambda *_: hasattr(self, "_topbar_narrow") and self._apply_compact_topbar())
        self._bubble_frame_t = 0.0
        self._bubble_last_frame_us: Optional[int] = None
        self._bubble_full_redraw = True
        self._last_scroll_pos: Optional[float] = None
        self._scroll_top_animating: bool = False
        self._scroll_top_animation_id: int = 0
        self._font_css_provider: Optional[Gtk.CssProvider] = None
        self._console_ui_css_provider: Optional[Gtk.CssProvider] = None
        self._console_topbar_glyphs: List[Tuple[Gtk.Label, Gtk.CssProvider, int, bool]] = []
        # what the on-screen buttons show: the kind of controller pressed
        # last ("xbox" / "sony"), or "keyboard" after a key - a controller
        # plugged in switches to it (JoystickManager)
        self._controller_family: str = "keyboard"
        self._console_family_glyphs: List[Tuple[Gtk.Label, str]] = []
        self._manual_radio_groups: List[Dict[str, object]] = []
        self._manual_combo_stops: Dict[Gtk.ComboBox, Dict[str, object]] = {}
        self._combo_picker_titles: Dict[Gtk.ComboBox, str] = {}
        self._radio_paired_spin: Dict[Gtk.RadioButton, Gtk.SpinButton] = {}
        self._spin_hold: Optional[Dict[str, object]] = None
        self._ringed_combo: Optional[Gtk.ComboBox] = None
        self._keyboard_nav_active: bool = False
        self._legendary_bin_checked = False
        self._legendary_bin: Optional[Path] = None

        self.favorites: set = load_favorites()
        self.recent: Dict[str, float] = load_recent()
        self.playtime: Dict[str, int] = load_playtime()
        self.playtime_history: Dict[str, Dict[str, int]] = load_playtime_history()
        self.first_seen: Dict[str, float] = load_first_seen()

        self.covers_dir = COVERS_DIR
        self.covers_dir.mkdir(parents=True, exist_ok=True)
        self.icons_dir = ICONS_DIR
        self.icons_dir.mkdir(parents=True, exist_ok=True)
        self.game_icons_dir = GAME_ICONS_DIR
        self.game_icons_dir.mkdir(parents=True, exist_ok=True)
        self.heroes_dir = HEROES_DIR
        self.heroes_dir.mkdir(parents=True, exist_ok=True)
        self.cover_map: Dict[str, Dict] = load_cover_map()
        self.icon_map: Dict[str, Dict] = load_icon_map()
        self.game_icon_map: Dict[str, Dict] = load_game_icon_map()
        self.hero_map: Dict[str, Dict] = load_hero_map()
        self._heroes_restoring: set = set()
        self._heroes_restore_failed: set = set()
        self._pending_hero_map_save = False
        # the game page that's open, told when that game's art changes
        self._game_page = None
        self.known_titles: Dict[str, Dict] = load_known_titles()
        self._covers_restoring: set = set()
        self._icons_restoring: set = set()
        self._game_icons_restoring: set = set()
        self._covers_restore_failed: set = set()
        self._cover_map_lock = threading.Lock()
        self._defer_map_saves = False
        self._pending_cover_map_save = False
        self._pending_game_icon_map_save = False
        self._icons_restore_failed: set = set()
        self._game_icons_restore_failed: set = set()

        self.scanner = Scanner(self.enabled_launchers)
        self.game_index: Dict[str, GameItem] = {}
        self.cover_widgets: Dict[str, List[Gtk.Image]] = {}
        # uid -> whether its cover has no see-through pixels (_art_is_opaque)
        self._cover_opaque: Dict[str, bool] = {}
        self.game_icon_widgets: Dict[str, List[Tuple[Gtk.Image, Optional[int]]]] = {}
        self.selection_mode: bool = False
        self.selected_uids: set = set()
        self.selection_widgets: Dict[str, List[Tuple[Gtk.Button, Optional[Gtk.Image]]]] = {}
        self.reorder_mode: bool = False
        self.console_mode_active: bool = False
        self._nav_groups: List[Dict] = []
        self.running_procs: Dict[str, subprocess.Popen] = {}
        self.running_launcher_procs: Dict[str, subprocess.Popen] = {}
        # launchers seen running, whoever started them (see _poll_running_launchers)
        self._launchers_running: set = set()
        self._launchers_checked_at = 0.0
        self._directory_confirmed_running: set = set()
        self._running_process_match: Dict[str, GameProcessMatch] = {}
        # Games running without PlunderHub having started them (see
        # core/running_detector.py), and uids to ignore briefly after a
        # game ended - the detector's last pass may still show it.
        self.detected_running: Dict[str, Detection] = {}
        self._detection_ignored_until: Dict[str, float] = {}
        self._console_hidden_for_game = False
        # a game's fullscreen took Console Mode's: given back on return
        self._console_lost_fullscreen = False
        self._window_iconified = False
        self._decorative_animations_paused = False
        self._console_cursor_hide_id: Optional[int] = None
        self._console_cursor_hidden = False
        self._console_blank_cursor: Optional[Gdk.Cursor] = None
        self.running_since: Dict[str, float] = {}
        self.running_time_widgets: Dict[str, List[Gtk.Label]] = {}
        self.launching_uids: Dict[str, float] = {}
        # Heroic launches being followed in its log (see heroic_launch)
        self._heroic_launches: Dict[str, HeroicLaunch] = {}
        self._heroic_stages: Dict[str, HeroicLaunchStage] = {}
        # Quit chose to wait for the work still running (request_quit)
        self._quit_after_work = False
        self._quit_blockers: List[object] = []
        # each launch's command and environment, to ask Steam again once
        # when it stalls after a download; the Steam launches asked again
        self._launch_cmds: Dict[str, Tuple[List[str], Optional[Dict[str, str]]]] = {}
        self._steam_asked_again: Set[str] = set()
        # when each launch settled, so Steam's window is kept in the
        # tray a little after the game starts too
        self._launch_settled_at: Dict[str, float] = {}
        # Heroic cloud-save syncs and save backups under way: uid -> mode
        # (download, ..., backup, restore), then "done:<mode>" a moment
        self._cloud_syncs: Dict[str, str] = {}
        # games waiting for the Proton build they run on to download:
        # uid -> the build (ui/proton_upkeep.py)
        self._proton_waits: Dict[str, str] = {}
        self._anim_player = AnimatedCoverPlayer(self, self._COVER_CORNER_RADIUS)
        self._reveal_epoch: int = 0
        self._reveal_index: int = 0
        self._download_thread: Optional[threading.Thread] = None
        self._browse_in_progress = False
        self._pending_reload = False
        self._temp_files_to_cleanup: List[Path] = []
        atexit.register(self._cleanup_temp_files)
        self._loading_dialog: Optional[Gtk.Dialog] = None
        self._scan_in_progress = False
        # A reload() requested while a scan was already running - run
        # once more when it finishes instead of dropping the request.
        self._reload_pending = False
        self._storage_scan_in_progress = False
        self._cached_launchers: List[Dict] = []
        self.tray_icon = None
        self._tray_backend: Optional[str] = None
        self._tray_menu: Optional[Gtk.Menu] = None
        self._tray_show_label: Optional[Gtk.Label] = None
        self._tray_games_item: Optional[Gtk.MenuItem] = None
        self._tray_quit_item: Optional[Gtk.MenuItem] = None
        self._file_monitors: List[Gio.FileMonitor] = []
        self._watch_debounce_id: Optional[int] = None
        self._watched_paths: Set[str] = set()
        self._watch_refresh_at = 0.0

        self._load_css()
        self._apply_theme(self.theme)
        self._apply_font_scale(self.font_scale)
        self._apply_console_ui_scale(self.console_ui_scale)

        self.connect("delete-event", self.on_delete_event)
        # hidden to the tray a while, the library's cards go (_drop_hidden_library)
        self._hidden_trim_id: Optional[int] = None
        self._library_dropped = False
        self.connect("hide", lambda *_: self._on_window_hidden())
        self.connect("show", lambda *_: self._on_window_shown())
        self.connect("key-press-event", self._on_window_key_press)
        self.connect("window-state-event", self._on_window_state_changed)
        self.connect("notify::is-active", self._on_window_active_changed)
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("motion-notify-event", self._on_console_motion_notify)
        self.connect("focus-in-event", self._on_window_focus_in)

        # No title bar of its own: the top bar inside the window moves it,
        # so the sidebar can run the window's full height.
        titlebar_stub = Gtk.Box()
        titlebar_stub.get_style_context().add_class("home-titlebar-stub")
        self.set_titlebar(titlebar_stub)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # over the whole window, sidebar and top bar too: the loading screen
        self._window_overlay = Gtk.Overlay()
        self._window_overlay.add(root)
        self.add(self._window_overlay)
        self.home_root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        root.pack_start(self.home_root, True, True, 0)
        self.home_root.pack_start(self._build_home_sidebar(), False, False, 0)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.home_root.pack_start(right, True, True, 0)
        right.pack_start(self._build_home_topbar(), False, False, 0)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.set_margin_start(8)
        right.pack_start(outer, True, True, 0)

        self.info = Gtk.Label(label="")
        self.info.set_xalign(0)
        self.info.get_style_context().add_class("dim-label")
        self.info.set_halign(Gtk.Align.START)
        self.info.set_valign(Gtk.Align.START)
        self.info.set_margin_start(4)
        self.info.set_margin_top(10)



        self._cover_hint_dismissed = False
        self.cover_hint_bar = self._build_cover_hint_bar()
        self.cover_hint_bar.set_no_show_all(True)
        self.cover_hint_bar.hide()
        outer.pack_start(self.cover_hint_bar, False, False, 0)

        self._available_update_tag: Optional[str] = None
        self._update_check_in_progress = False
        self._last_update_check_failed = False
        self.update_banner_bar = self._build_update_banner()
        self.update_banner_bar.set_no_show_all(True)
        self.update_banner_bar.hide()
        outer.pack_start(self.update_banner_bar, False, False, 0)

        self._missing_soft_deps: List[str] = []
        self._soft_deps_install_in_progress = False
        self.soft_deps_banner_bar = self._build_soft_deps_banner()
        self.soft_deps_banner_bar.set_no_show_all(True)
        self.soft_deps_banner_bar.hide()
        outer.pack_start(self.soft_deps_banner_bar, False, False, 0)

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_hexpand(True)
        self.scroller.set_vexpand(True)

        self.bubble_canvas = Gtk.DrawingArea()
        self.bubble_canvas.get_style_context().add_class("ocean-bg")
        self.bubble_canvas.set_hexpand(True)
        self.bubble_canvas.set_vexpand(True)
        self.bubble_canvas.connect("draw", self._on_bubble_canvas_draw)
        self.bubble_canvas.connect("size-allocate", lambda *_: self._redraw_all_bubbles())
        for signal_name in ("map", "unmap"):
            self.bubble_canvas.connect(signal_name, lambda *_: hasattr(self, "_anim_player")
                                       and self._update_decorative_animation_pause_state())
        self._bubble_tick_id = None
        self._start_bubble_animation()
        self._process_probe = ProcessProbe(interval=1.0)
        self._process_probe.start()
        self._workspace_mover: Optional[WorkspaceMover] = None
        self._workspace_detected_at = 0.0
        self.dialog_corner_radius = self.DIALOG_CORNER_RADIUS
        self.workspace_mover()
        self._match_compositor_corners()
        self._running_detector = RunningDetector(interval=3.0, steam_running=is_steam_appid_running)
        self._running_detector.start()
        if self.settings.get("bilge_waiting"):
            GLib.timeout_add_seconds(20, self._bilge_check_waiting)
        GLib.timeout_add(1000, self._poll_running_games)
        GLib.timeout_add(1000, self._poll_running_launchers)

        self.main_overlay = Gtk.Overlay()
        self.main_overlay.add(self.bubble_canvas)
        self.main_overlay.add_overlay(self.scroller)

        self.startup_overlay = Gtk.EventBox()
        self.startup_overlay.get_style_context().add_class("startup-scrim")
        self.startup_overlay.get_style_context().add_class("startup-scrim-solid")
        scrim_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scrim_box.set_halign(Gtk.Align.CENTER)
        scrim_box.set_valign(Gtk.Align.CENTER)
        from plunderhub.ui.startup_intro import LoadingMark
        self.startup_spinner = LoadingMark(self)
        scrim_box.pack_start(self.startup_spinner, False, False, 0)
        startup_label = Gtk.Label(label=self._("startup_loading"))
        startup_label.get_style_context().add_class("startup-scrim-label")
        scrim_box.pack_start(startup_label, False, False, 0)
        self.startup_overlay.add(scrim_box)

        self.main_overlay.add_overlay(self.info)
        self.main_overlay.set_overlay_pass_through(self.info, True)

        self.animations_paused_bar, self.animations_paused_label = self._build_animations_paused_bar()
        self.animations_paused_bar.set_halign(Gtk.Align.CENTER)
        self.animations_paused_bar.set_valign(Gtk.Align.START)
        self.animations_paused_bar.set_margin_top(0)    # the pill's own margin holds its shadow
        self.main_overlay.add_overlay(self.animations_paused_bar)
        self.main_overlay.set_overlay_pass_through(self.animations_paused_bar, True)

        self._undo_toast_timeout_id: Optional[int] = None
        self._undo_toast_callback: Optional[Callable[[], None]] = None
        self.undo_toast = self._build_undo_toast()
        self.undo_toast.set_halign(Gtk.Align.CENTER)
        self.undo_toast.set_valign(Gtk.Align.END)
        self.undo_toast.set_margin_bottom(20)
        self.main_overlay.add_overlay(self.undo_toast)
        self.main_overlay.set_overlay_pass_through(self.undo_toast, False)

        # In a window of its own: the scroller's viewport has one, and a
        # windowless overlay would be painted underneath the cards.
        self.bulk_action_bar = Gtk.EventBox()
        self.bulk_action_bar.set_visible_window(True)
        self.bulk_action_bar.get_style_context().add_class("home-floating")
        self.bulk_action_bar.add(self._build_bulk_action_bar())
        self.bulk_action_bar.set_halign(Gtk.Align.CENTER)
        self.bulk_action_bar.set_valign(Gtk.Align.END)
        self.bulk_action_bar.set_margin_bottom(4)
        self.bulk_action_bar.set_no_show_all(True)
        self.bulk_action_bar.hide()
        self.main_overlay.add_overlay(self.bulk_action_bar)
        self.main_overlay.add_overlay(self._build_resupply_pill())

        self.scroll_top_revealer = Gtk.Revealer()
        self.scroll_top_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self.scroll_top_revealer.set_transition_duration(1000)
        self.scroll_top_revealer.set_halign(Gtk.Align.END)
        self.scroll_top_revealer.set_valign(Gtk.Align.END)
        self.scroll_top_revealer.set_margin_end(20)
        self.scroll_top_revealer.set_margin_bottom(20)
        self.scroll_top_revealer.set_reveal_child(False)

        # A floating pill: an accent arrow and "Back to top".
        self.scroll_top_btn = Gtk.Button()
        self.scroll_top_btn.get_style_context().add_class("scroll-top-btn")
        self.scroll_top_btn.get_style_context().add_class("home-top-btn")
        self.scroll_top_btn.set_focus_on_click(False)
        top_row = Gtk.Box(spacing=8)
        self.scroll_top_arrow = Gtk.Image()
        self.scroll_top_arrow.set_valign(Gtk.Align.CENTER)
        self.scroll_top_arrow.connect("style-updated", self._tint_scroll_top_arrow)
        top_row.pack_start(self.scroll_top_arrow, False, False, 0)
        self.scroll_top_label = Gtk.Label(label=self._("scroll_top_btn_tooltip"))
        top_row.pack_start(self.scroll_top_label, False, False, 0)
        self.scroll_top_btn.add(top_row)
        self.scroll_top_btn.connect("clicked", self._on_scroll_top_clicked)
        self.scroll_top_revealer.add(self.scroll_top_btn)

        self.main_overlay.add_overlay(self.scroll_top_revealer)
        self.main_overlay.set_overlay_pass_through(self.scroll_top_revealer, True)
        self.scroller.get_vadjustment().connect("value-changed", self._on_main_scroll_value_changed)

        outer.pack_start(self.main_overlay, True, True, 0)

        self.console_box = self._build_console_mode_view()
        self.console_box.get_style_context().add_class("console-ui-scale-root")
        root.pack_start(self.console_box, True, True, 0)

        self._joystick_manager = JoystickManager(
            self._on_joystick_event, on_family_change=self._on_joystick_family_changed
        )

        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.content.set_margin_top(4)
        self.content.set_margin_start(16)
        self.content.set_margin_end(24)
        self.content.set_margin_bottom(24)
        self.scroller.add(self.content)
        self.content.set_focus_vadjustment(self.scroller.get_vadjustment())


        main_vadjustment = self.scroller.get_vadjustment()
        self._last_scroll_pos = main_vadjustment.get_value()
        main_vadjustment.connect("value-changed", self._on_scroll_value_changed)

        if self.use_tray:
            self._setup_tray_icon()

        try:
            self._setup_file_watchers()
        except Exception as e:
            logger.warning(f"Filesystem watching disabled: {e}")

        self._window_overlay.add_overlay(self.startup_overlay)
        self.startup_spinner.start()
        self._startup_mapped_at: Optional[float] = None
        self.connect("map-event", lambda *_: (self._startup_mapped_at is None and setattr(
            self, "_startup_mapped_at", time.monotonic()), False)[1])

        self.dialog_dim_scrim = Gtk.Box()
        self.dialog_dim_scrim.get_style_context().add_class("startup-scrim")
        self._dimming_dialog_open = False
        self._dim_depth = 0

        # must be set before reload() below - reload() calls
        # _maybe_check_for_updates(), which reads this immediately
        self.update_check_enabled = self.settings.get("update_check_enabled", True)

        self._maybe_show_soft_deps_banner()

        self.reload(is_startup=True)
        # Treasure Map: a sidebar page under Recently sailed (ui/treasure_map.py)
        GLib.idle_add(self._attach_treasure_map)

    def _attach_treasure_map(self) -> bool:
        from plunderhub.constants import CACHE_DIR
        try:
            _treasure_map().attach(self, {"cache": str(CACHE_DIR / "treasure-map"), "tab": "all"})
        except Exception as e:
            logger.warning(f"Treasure Map couldn't be set up: {e}")
        return False

    _STARTUP_OVERLAY_MAX_WAIT = 30.0
    _STARTUP_INTRO_DELAY = 0.5

    def _poll_startup_ready(self) -> bool:
        if getattr(self, "startup_overlay", None) is None:
            return False
        if time.monotonic() < getattr(self, "_startup_overlay_deadline", 0):
            if self._animation_work_pending():
                return True
            # as the welcome card's logo: not before the compositor's own
            # window-opening animation is over, which would hide it
            mapped = self._startup_mapped_at
            if self.get_visible() and (mapped is None
                                       or time.monotonic() < mapped + self._STARTUP_INTRO_DELAY):
                return True
        self._hide_startup_overlay()
        return False

    def _warm_launcher_states(self) -> bool:
        from plunderhub.launchers.registry import LAUNCHER_REGISTRY
        from plunderhub.ui import launcher_tasks

        def work():
            try:
                states = {l.name: launcher_tasks.launcher_state(l.name) for l in LAUNCHER_REGISTRY}
            except Exception as e:
                logger.debug(f"Couldn't read the launchers' state ahead: {e}")
                return
            GLib.idle_add(lambda: (getattr(self, "_launcher_state_cache", None) is None
                                   and setattr(self, "_launcher_state_cache", states)) and False)
        threading.Thread(target=work, daemon=True, name="plunderhub-launcher-state").start()
        return False

    def _hide_startup_overlay(self) -> bool:
        overlay = getattr(self, "startup_overlay", None)
        if overlay is None:
            return False
        self.startup_overlay = None
        # Proton builds checked and kept in step, once the window is up
        from plunderhub.ui.proton_upkeep import upkeep
        upkeep(self).start()
        # the launchers' install state, asked of the package manager ahead
        # of Settings (which would wait on it - seconds on dpkg / rpm)
        GLib.timeout_add_seconds(10, self._warm_launcher_states)
        try:
            self.startup_spinner.stop()
        except Exception as e:
            logger.debug(f"Couldn't stop the startup spinner: {e}")
        if self._startup_intro_wanted():
            # the window builds itself up as the scrim fades away
            from plunderhub.ui.startup_intro import StartupIntro
            intro = StartupIntro(self, overlay)
            try:
                intro.collect()
                self._startup_intro = intro
                intro.run()
            except Exception as e:
                logger.warning(f"Startup animation skipped: {e}")
                intro.finish()
            return False
        try:
            overlay.destroy()
        except Exception as e:
            logger.debug(f"Couldn't remove the startup overlay: {e}")
        return False

    def _startup_intro_wanted(self) -> bool:
        """Only where someone sees it: not in the tray, not in Console Mode,
        not with the system's animations off."""
        settings = Gtk.Settings.get_default()
        return (self.get_visible() and not self.console_mode_active
                and (settings is None or settings.props.gtk_enable_animations))

    def _theme_fg_hex(self, theme: Optional[str] = None) -> str:
        palette = THEME_PALETTES.get(theme if theme is not None else self.theme)
        if palette is not None:
            return palette["fg"]
        orig_prefer_dark, _ = _capture_original_theme_preference()
        return "#eeeeee" if orig_prefer_dark else "#241f31"

    def _theme_on_accent_hex(self, theme: Optional[str] = None) -> str:
        palette = THEME_PALETTES.get(theme if theme is not None else self.theme)
        return accent_text_color(palette) if palette is not None else "#ffffff"

    def _icon_tint_for(self, widget: Gtk.Widget, theme: Optional[str] = None) -> str:
        """An icon on an accent-filled (suggested-action) button is drawn
        in the colour of that button's text, not the window's."""
        parent = widget.get_parent()
        while parent is not None:
            if isinstance(parent, Gtk.Button):
                if parent.get_style_context().has_class("suggested-action"):
                    return self._theme_on_accent_hex(theme)
                if parent.get_style_context().has_class("destructive-action"):
                    return DANGER_FG
                break
            parent = parent.get_parent()
        return self._theme_fg_hex(theme)

    def _mark_suggested_action(self, button: Gtk.Button):
        button.get_style_context().add_class("suggested-action")
        image = button.get_image()
        marker = image.get_name() if image is not None else ""
        if not marker or not marker.startswith(f"{self._ICON_MARKER}:"):
            return
        _, px_text, icon_name = marker.split(":", 2)
        pixbuf = get_icon_pixbuf(icon_name, int(px_text), self._theme_on_accent_hex())
        if pixbuf is not None:
            image.set_from_pixbuf(pixbuf)

    def _icon_pixbuf(self, icon_name: str, px_size: int) -> Optional[GdkPixbuf.Pixbuf]:
        return get_icon_pixbuf(icon_name, px_size, self._theme_fg_hex())

    _ICON_SIZE_PX = {
        Gtk.IconSize.MENU: 18,
        Gtk.IconSize.BUTTON: 20,
        Gtk.IconSize.DND: 36,
        Gtk.IconSize.DIALOG: 48,
    }

    def _icon_image(self, icon_name: str, gtk_icon_size) -> Gtk.Image:
        px_size = self._ICON_SIZE_PX.get(gtk_icon_size)
        if px_size is None:
            ok, width, height = Gtk.icon_size_lookup(gtk_icon_size)
            px_size = width if ok and width > 0 else 16
        pixbuf = self._icon_pixbuf(icon_name, px_size)
        if pixbuf is not None:
            image = Gtk.Image.new_from_pixbuf(pixbuf)
            self._mark_tinted_icon(image, icon_name, px_size)
            return image
        return Gtk.Image.new_from_icon_name(icon_name, gtk_icon_size)

    def _icon_image_at_size(self, icon_name: str, px_size: int) -> Gtk.Image:
        """Same as _icon_image, but for a raw pixel size rather than one
        of Gtk's own predefined Gtk.IconSize buckets - for spots (like
        _make_no_cover_placeholder) that need an icon scaled to fit
        whatever arbitrary dimensions the caller has, not the nearest
        Gtk.IconSize."""
        pixbuf = self._icon_pixbuf(icon_name, px_size)
        if pixbuf is not None:
            image = Gtk.Image.new_from_pixbuf(pixbuf)
            self._mark_tinted_icon(image, icon_name, px_size)
            return image
        return Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)

    def _mark_tinted_icon(self, image: Gtk.Image, icon_name: str, px_size: int):
        try:
            image.set_name(f"{self._ICON_MARKER}:{px_size}:{icon_name}")
        except Exception as e:
            logger.debug(f"Couldn't mark icon {icon_name!r} for re-tinting: {e}")

    def _iter_widgets(self, root: Gtk.Widget):
        stack = [root]
        visited = []
        seen = set()
        while stack:
            widget = stack.pop()
            key = id(widget)
            if key in seen:
                continue
            seen.add(key)
            visited.append(widget)
            yield widget
            try:
                if isinstance(widget, Gtk.Container):
                    stack.extend(widget.get_children())
                if isinstance(widget, Gtk.MenuItem):
                    submenu = widget.get_submenu()
                    if submenu is not None:
                        stack.append(submenu)
                if isinstance(widget, Gtk.Button):
                    image = widget.get_image()
                    if image is not None:
                        stack.append(image)
            except Exception as e:
                logger.debug(f"Skipping a widget while walking the tree: {e}")

    def _refresh_icon_tints(self, theme: Optional[str] = None):
        prefix = f"{self._ICON_MARKER}:"
        repainted = 0
        for toplevel in Gtk.Window.list_toplevels():
            for widget in self._iter_widgets(toplevel):
                if not isinstance(widget, Gtk.Image):
                    continue
                marker = widget.get_name() or ""
                if marker.startswith(LOGO_MARKER + ":"):
                    # the logo, in the new theme's colour
                    try:
                        mark_logo(widget, int(marker.split(":", 1)[1]))
                    except Exception as e:
                        logger.debug(f"Couldn't repaint the logo: {e}")
                    continue
                if not marker.startswith(prefix):
                    continue
                try:
                    _, px_text, icon_name = marker.split(":", 2)
                    pixbuf = get_icon_pixbuf(icon_name, int(px_text), self._icon_tint_for(widget, theme))
                    if pixbuf is not None:
                        widget.set_from_pixbuf(pixbuf)
                        repainted += 1
                except Exception as e:
                    logger.debug(f"Couldn't re-tint icon {marker!r}: {e}")
        logger.debug(f"Re-tinted {repainted} icon(s) for theme {theme or self.theme!r}.")
        self._refresh_entry_icon_tints()

    def _refresh_entry_icon_tints(self):
        entry = getattr(self, "api_entry", None)
        if entry is None:
            return
        try:
            name = "view-reveal-symbolic" if entry.get_visibility() else "view-conceal-symbolic"
            pixbuf = self._icon_pixbuf(name, 16)
            if pixbuf is not None:
                entry.set_icon_from_pixbuf(Gtk.EntryIconPosition.SECONDARY, pixbuf)
        except Exception as e:
            logger.debug(f"Couldn't re-tint entry icon: {e}")

    def _make_icon_button(self, icon_name: str, tooltip: str) -> Gtk.Button:
        btn = Gtk.Button()
        btn.set_image(self._icon_image(icon_name, Gtk.IconSize.BUTTON))
        btn.set_tooltip_text(tooltip)
        btn.set_can_focus(False)
        btn.get_accessible().set_name(tooltip)
        return btn


    def _tint_scroll_top_arrow(self, image: Gtk.Image):
        found, colour = image.get_style_context().lookup_color("accent_color")
        hex_colour = ("#%02x%02x%02x" % (round(colour.red * 255), round(colour.green * 255), round(colour.blue * 255))
                      if found else "#22d3ac")
        pb = get_icon_pixbuf("arrow-up-to-line-symbolic", 18, hex_colour)
        if pb is not None:
            image.set_from_pixbuf(pb)

    def _set_scroll_top_visible(self, visible: bool):
        self.scroll_top_revealer.set_reveal_child(visible)
        self.main_overlay.set_overlay_pass_through(self.scroll_top_revealer, not visible)

    def _on_main_scroll_value_changed(self, adjustment):
        if not self.scroll_top_button_enabled:
            self._set_scroll_top_visible(False)
            return
        scrollable_range = adjustment.get_upper() - adjustment.get_page_size()
        is_scrollable = scrollable_range > 24
        scrolled_past_threshold = adjustment.get_value() > 80
        self._set_scroll_top_visible(is_scrollable and scrolled_past_threshold)

    def _on_scroll_top_clicked(self, _btn):
        try:
            adjustment = self.scroller.get_vadjustment()
            # A fast scroll gesture (touchpad/mouse-wheel) can leave
            # GTK's own kinetic-scrolling momentum still coasting when
            # the button is clicked right away. That leftover momentum
            # keeps writing to the SAME adjustment every frame, fighting
            # our own animation's adjustment.set_value() calls below -
            # that's the "choppy" scroll-up reported when clicking
            # immediately after scrolling (waiting a couple of seconds
            # let the momentum coast to a stop on its own first, which
            # is why it looked smooth then). Toggling kinetic scrolling
            # off and back on cancels any in-flight momentum outright.
            self.scroller.set_kinetic_scrolling(False)
            self.scroller.set_kinetic_scrolling(True)
            self._animate_scroll_top(adjustment, adjustment.get_value(), adjustment.get_lower())
        except Exception as e:
            logger.warning(f"Scroll-to-top animation failed: {e}")

    def _animate_scroll_top(self, adjustment: Gtk.Adjustment, start_value: float, target_value: float):
        """Glides back up: longer for a longer way, easing in and out, one
        step per frame of the monitor - the bubbles follow it down."""
        self._scroll_top_animation_id += 1
        my_animation_id = self._scroll_top_animation_id
        delta = target_value - start_value
        duration_us = int(1_000_000 * min(1.1, 0.45 + abs(delta) / 6000))
        start = {"us": None}
        self._scroll_top_animating = True

        def step(_widget, frame_clock) -> bool:
            if my_animation_id != self._scroll_top_animation_id:
                return False
            now = frame_clock.get_frame_time()
            if start["us"] is None:
                start["us"] = now
            t = min(1.0, (now - start["us"]) / duration_us)
            eased = 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2
            adjustment.set_value(start_value + delta * eased)
            if t >= 1.0:
                self._scroll_top_animating = False
                return False
            return True

        self.scroller.add_tick_callback(step)

    def _build_undo_toast(self) -> Gtk.Revealer:
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        revealer.set_transition_duration(150)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bar.get_style_context().add_class("undo-toast")
        bar.get_style_context().add_class("home-toast")

        # The toast's icon inside a ring that runs down with the time
        # left to undo.
        self._undo_toast_icon_name = "rotate-ccw-symbolic"
        self._undo_toast_shown_at = 0.0
        self._undo_toast_duration = 1.0
        self._undo_toast_tick_id: Optional[int] = None
        ring = Gtk.DrawingArea()
        ring.set_size_request(30, 30)
        ring.set_valign(Gtk.Align.CENTER)
        ring.connect("draw", self._draw_undo_toast_ring)
        self.undo_toast_ring = ring
        bar.pack_start(ring, False, False, 0)

        self.undo_toast_label = Gtk.Label(label="")
        self.undo_toast_label.set_xalign(0)
        self.undo_toast_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.undo_toast_label.set_max_width_chars(56)
        bar.pack_start(self.undo_toast_label, False, False, 0)

        self.undo_toast_btn = Gtk.Button(label=self._("undo_btn"))
        self.undo_toast_btn.get_style_context().add_class("undo-toast-btn")
        self.undo_toast_btn.get_style_context().add_class("home-toast-btn")
        self.undo_toast_btn.set_focus_on_click(False)
        self.undo_toast_btn.connect("clicked", lambda *_: self._run_undo_toast_action())
        bar.pack_start(self.undo_toast_btn, False, False, 0)

        revealer.add(bar)
        return revealer

    def _report_scan_errors(self, scanner: "Scanner"):
        errors = getattr(scanner, "scan_errors", None) or {}
        signature = tuple(sorted(errors))
        if signature == getattr(self, "_last_scan_error_signature", ()):
            return
        self._last_scan_error_signature = signature
        if not errors:
            return
        for launcher, detail in errors.items():
            logger.warning(f"{launcher} data couldn't be read this scan: {detail}")
        names = ", ".join(sorted(errors))
        try:
            self._show_undo_toast(
                self._("scan_error_toast").format(launchers=names),
                lambda: self._show_info_dialog(
                    "\n\n".join(f"{launcher}:\n{detail}"
                                for launcher, detail in sorted(errors.items())),
                    Gtk.MessageType.WARNING),
                timeout_ms=12000,
                action_label=self._("details_btn"),
                icon_name="triangle-alert-symbolic",
            )
        except Exception as e:
            logger.debug(f"Couldn't show the scan-error toast: {e}")

    def _steam_start(self, mode: str) -> bool:
        """Opened from Steam: Console Mode, or the person picks (the
        desktop) - Desktop to start with, the main window shown only after."""
        if mode == "ask":
            answer = self._card_dialog(self._("steam_start_head"), self._("steam_start_text"),
                                       [(self._("steam_start_console"), Gtk.ResponseType.YES, "ghost"),
                                        (self._("steam_start_desktop"), Gtk.ResponseType.NO, "accent")],
                                       icon_name="input-gaming-symbolic", default=Gtk.ResponseType.NO)
            mode = "console" if answer == Gtk.ResponseType.YES else ""
        if not self.get_visible():
            self.show_all()
            self.present()
            self.deiconify()
        if mode == "console":
            self._enter_console_mode()
        return False

    def _bilge_check_waiting(self) -> bool:
        """Bilge restores that waited for a game's first start (bilge_page)."""
        if self.settings.get("bilge_waiting"):
            from plunderhub.ui.bilge_page import check_waiting
            check_waiting(self)
        return False

    def _show_undo_toast(self, message: str, undo_callback: Callable[[], None], timeout_ms: int = 6000,
                         action_label: Optional[str] = None, icon_name: str = "rotate-ccw-symbolic"):
        if self._undo_toast_timeout_id is not None:
            GLib.source_remove(self._undo_toast_timeout_id)
            self._undo_toast_timeout_id = None
        self.undo_toast_label.set_text(message)
        self.undo_toast_btn.set_label(action_label or self._("undo_btn"))
        self._undo_toast_callback = undo_callback
        self._undo_toast_icon_name = icon_name
        self._undo_toast_shown_at = time.monotonic()
        self._undo_toast_duration = timeout_ms / 1000
        if self._undo_toast_tick_id is None:
            self._undo_toast_tick_id = GLib.timeout_add(50, self._tick_undo_toast_ring)
        self.undo_toast.set_reveal_child(True)
        self._undo_toast_timeout_id = GLib.timeout_add(timeout_ms, self._on_undo_toast_timeout)

    def _tick_undo_toast_ring(self) -> bool:
        if not self.undo_toast.get_reveal_child():
            self._undo_toast_tick_id = None
            return False
        self.undo_toast_ring.queue_draw()
        return True

    def _draw_undo_toast_ring(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        found, colour = widget.get_style_context().lookup_color("accent_color")
        rgb = (colour.red, colour.green, colour.blue) if found else (0x22 / 255, 0xd3 / 255, 0xac / 255)
        left = max(0.0, 1 - (time.monotonic() - self._undo_toast_shown_at) / max(0.1, self._undo_toast_duration))
        cx, cy, r = w / 2, h / 2, min(w, h) / 2 - 2
        cr.set_line_width(2.5)
        cr.set_source_rgba(*rgb, 0.18)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        if left > 0:
            cr.set_source_rgba(*rgb, 1)
            cr.arc(cx, cy, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * left)
            cr.stroke()
        pb = get_icon_pixbuf(self._undo_toast_icon_name, 14,
                             "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb))
        if pb is not None:
            Gdk.cairo_set_source_pixbuf(cr, pb, cx - logical_size(pb)[0] / 2, cy - logical_size(pb)[1] / 2)
            cr.paint()
        return False

    def _on_undo_toast_timeout(self) -> bool:
        self._undo_toast_timeout_id = None
        self._undo_toast_callback = None
        self.undo_toast.set_reveal_child(False)
        return False

    def _run_undo_toast_action(self):
        if self._undo_toast_timeout_id is not None:
            GLib.source_remove(self._undo_toast_timeout_id)
            self._undo_toast_timeout_id = None
        callback = self._undo_toast_callback
        self._undo_toast_callback = None
        self.undo_toast.set_reveal_child(False)
        if callback is not None:
            callback()

    _BULK_ACTIONS = (
        # attribute, icon, string key
        ("bulk_select_all_btn", "object-select-symbolic", "select_all_btn"),
        ("bulk_clear_sel_btn", "edit-clear-all-symbolic", "clear_selection_btn"),
        ("bulk_fav_btn", "starred-symbolic", "bulk_favorite_btn"),
        ("bulk_unfav_btn", "non-starred-symbolic", "bulk_unfavorite_btn"),
        ("bulk_hide_btn", "view-conceal-symbolic", "bulk_hide_btn"),
    )

    def _build_bulk_action_bar(self) -> Gtk.Box:
        """Selection mode's floating pill at the bottom of the library:
        the count, round icon actions (named in their tooltips) and Done."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.get_style_context().add_class("home-selection-bar")

        self.bulk_count_label = Gtk.Label(label="")
        self.bulk_count_label.get_style_context().add_class("home-selection-count")
        self._apply_tabular_figures(self.bulk_count_label)
        bar.pack_start(self.bulk_count_label, False, False, 0)
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.get_style_context().add_class("home-toolbar-sep")
        bar.pack_start(sep, False, False, 6)

        handlers = {
            "bulk_select_all_btn": self._select_all_visible,
            "bulk_clear_sel_btn": self._clear_selection,
            "bulk_fav_btn": lambda: self._bulk_set_favorite(True),
            "bulk_unfav_btn": lambda: self._bulk_set_favorite(False),
            "bulk_hide_btn": self._bulk_hide,
        }
        for attr, icon, key in self._BULK_ACTIONS:
            btn = Gtk.Button()
            btn.set_image(self._icon_image_at_size(icon, 20))
            btn.set_tooltip_text(self._(key))
            btn.get_accessible().set_name(self._(key))
            btn.set_valign(Gtk.Align.CENTER)
            btn.connect("clicked", lambda _b, h=handlers[attr]: h())
            setattr(self, attr, btn)
            bar.pack_start(btn, False, False, 0)

        self.bulk_done_btn = Gtk.Button(label=self._("bulk_done_btn"))
        self.bulk_done_btn.get_style_context().add_class("suggested-action")
        self.bulk_done_btn.get_style_context().add_class("home-play")
        self.bulk_done_btn.set_valign(Gtk.Align.CENTER)
        self.bulk_done_btn.set_margin_start(8)
        self.bulk_done_btn.connect("clicked", lambda *_: self._exit_selection_mode())
        bar.pack_start(self.bulk_done_btn, False, False, 0)
        return bar

    def _build_animations_paused_bar(self) -> Tuple[Gtk.Revealer, Gtk.Label]:
        """A floating status pill: a pause badge, "Animations paused" and,
        dimmer, why. Returns the revealer and the label for the why."""
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(220)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.get_style_context().add_class("animations-paused-bar")
        bar.get_style_context().add_class("home-status-pill")

        badge = Gtk.DrawingArea()
        badge.set_size_request(22, 22)
        badge.set_valign(Gtk.Align.CENTER)

        def draw_badge(widget, cr):
            found, colour = widget.get_style_context().lookup_color("accent_color")
            rgb = (colour.red, colour.green, colour.blue) if found else (0x22 / 255, 0xd3 / 255, 0xac / 255)
            cr.arc(11, 11, 11, 0, 2 * math.pi)
            cr.set_source_rgba(*rgb, 0.18)
            cr.fill()
            cr.set_source_rgba(*rgb, 1)
            for x in (7.5, 12.5):
                cr.rectangle(x, 6.5, 2.2, 9)
            cr.fill()
            return False
        badge.connect("draw", draw_badge)
        bar.pack_start(badge, False, False, 0)

        title = Gtk.Label(label=self._("animations_paused_title"))
        title.get_style_context().add_class("home-status-title")
        bar.pack_start(title, False, False, 0)
        dot = Gtk.Label(label="·")
        dot.get_style_context().add_class("dim-label")
        bar.pack_start(dot, False, False, 0)
        label = Gtk.Label(label="")
        label.get_style_context().add_class("dim-label")
        bar.pack_start(label, False, False, 0)

        revealer.add(bar)
        revealer.plunderhub_title = title
        return revealer, label

    def _on_search_entry_focus_out(self, widget, event):
        widget.set_placeholder_text("")
        widget.set_placeholder_text(self._("filter_placeholder"))
        widget.queue_draw()
        return False

    _SEARCH_DEBOUNCE_MS = 150

    def _on_search_changed(self):
        view = _treasure_map().shown(self)
        if view is not None:
            view.set_query(self.search_entry.get_text())
            return
        # Collapses rapid typing into a single re-render instead of one
        # full _render_launchers() (which tears down and rebuilds every
        # card in the whole grid) per keystroke - noticeable as typing
        # lag on a large library. Each new keystroke cancels whatever
        # was still pending and restarts the wait, so the actual
        # re-render only fires once input has paused briefly.
        if self._search_debounce_id is not None:
            GLib.source_remove(self._search_debounce_id)
        self._search_debounce_id = GLib.timeout_add(self._SEARCH_DEBOUNCE_MS, self._on_search_debounce_timeout)

    def _on_search_debounce_timeout(self) -> bool:
        self._search_debounce_id = None
        self._render_launchers()
        return False

    def _toggle_maximize(self):
        if self._is_maximized:
            self.unmaximize()
            self._is_maximized = False
            self.maximize_btn.set_image(self._icon_image("window-maximize-symbolic", Gtk.IconSize.BUTTON))
        else:
            self.maximize()
            self._is_maximized = True
            self.maximize_btn.set_image(self._icon_image("window-restore-symbolic", Gtk.IconSize.BUTTON))

    def _refresh_static_ui_language(self):
        if not hasattr(self, "shortcuts_btn"):
            return
        self.sort_btn.set_tooltip_text(self._("sort_by"))
        self.search_entry.set_placeholder_text(self._("filter_placeholder"))
        self._refresh_home_language()
        self.view_grid_btn.set_tooltip_text(self._("view_grid_tooltip"))
        self.view_grid_btn.get_accessible().set_name(self._("view_grid_tooltip"))
        self.view_list_btn.set_tooltip_text(self._("view_list_tooltip"))
        self.view_list_btn.get_accessible().set_name(self._("view_list_tooltip"))
        for btn, key in ((self.select_btn, "selection_mode_btn"), (self.reorder_btn, "reorder_menu"),
                         (self.shortcuts_btn, "help_btn_tooltip")):
            btn.set_tooltip_text(self._(key))
            btn.get_accessible().set_name(self._(key))
        self.report_bug_label.set_text(self._("about_report_bug"))
        self.kofi_btn.set_tooltip_text(self._("about_support_kofi"))
        self.minimize_btn.set_tooltip_text(self._("window_minimize_tooltip"))
        self.maximize_btn.set_tooltip_text(self._("window_maximize_tooltip"))
        self.close_btn_header.set_tooltip_text(self._("window_close_tooltip"))
        for attr, _icon, key in self._BULK_ACTIONS:
            getattr(self, attr).set_tooltip_text(self._(key))
            getattr(self, attr).get_accessible().set_name(self._(key))
        self.bulk_done_btn.set_label(self._("bulk_done_btn"))
        self._update_bulk_bar()
        self.scroll_top_label.set_text(self._("scroll_top_btn_tooltip"))
        self.soft_deps_banner_close_btn.set_tooltip_text(self._("dismiss_tooltip"))
        self.soft_deps_banner_close_btn.get_accessible().set_name(self._("dismiss_tooltip"))
        self.update_banner_close_btn.set_tooltip_text(self._("dismiss_tooltip"))
        self.update_banner_close_btn.get_accessible().set_name(self._("dismiss_tooltip"))

        self._retranslate_tray_menu()

    def _on_card_view_mode_toggled(self, btn, mode):
        if btn.get_active() and self.card_view_mode != mode:
            self.card_view_mode = mode
            self.settings["card_view_mode"] = mode
            save_settings(self.settings)
            self._render_launchers(animate=True)

    def _on_window_key_press(self, widget, event):
        if not event.is_modifier:
            self._on_joystick_family_changed("keyboard")
        if self.console_mode_active:
            if event.keyval == Gdk.KEY_Escape:
                self._exit_console_mode()
                return True
            if event.keyval in (Gdk.KEY_bracketleft, Gdk.KEY_Page_Up, Gdk.KEY_q, Gdk.KEY_Q):
                self._console_shift_launcher_filter(-1)
                return True
            if event.keyval in (Gdk.KEY_bracketright, Gdk.KEY_Page_Down, Gdk.KEY_e, Gdk.KEY_E):
                self._console_shift_launcher_filter(1)
                return True
            if event.keyval in self.ARROW_KEY_DIRECTIONS:
                self._console_move_focus(self.ARROW_KEY_DIRECTIONS[event.keyval])
                return True
            if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter, Gdk.KEY_space):
                self._console_activate()
                return True
            if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
                return False
            # F / S as the hints show them (X / Y: a pad's buttons, kept)
            if event.keyval in (Gdk.KEY_f, Gdk.KEY_F, Gdk.KEY_x, Gdk.KEY_X):
                self._console_toggle_favorite()
                return True
            if event.keyval in (Gdk.KEY_s, Gdk.KEY_S, Gdk.KEY_y, Gdk.KEY_Y):
                self._console_cycle_sort()
                return True
            if event.keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
                self._console_shift_launcher_filter(-1 if event.state & Gdk.ModifierType.SHIFT_MASK else 1)
                return True
            if event.keyval in (Gdk.KEY_c, Gdk.KEY_C, Gdk.KEY_BackSpace):
                self._exit_console_mode()
                return True
            if event.keyval in (Gdk.KEY_o, Gdk.KEY_O):
                # the pad's Menu / Options button
                self.show_options()
                return True
            if event.keyval == Gdk.KEY_F1:
                self._show_shortcuts_dialog()
                return True
            if event.keyval in (Gdk.KEY_Home, Gdk.KEY_End):
                self.console_view.move_to_end(event.keyval == Gdk.KEY_End)
                return True
            return False
        menu = getattr(self, "_open_menu", None)
        if menu is not None and menu.get_visible():
            # a game's or launcher's menu takes the arrows, Esc and Enter
            if event.keyval in self.ARROW_KEY_DIRECTIONS:
                return menu.nav(self.ARROW_KEY_DIRECTIONS[event.keyval])
            if event.keyval == Gdk.KEY_Escape:
                return menu.nav("back")
            if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter, Gdk.KEY_space):
                return menu.nav("activate")
            return False
        if event.state & Gdk.ModifierType.CONTROL_MASK and event.keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search_entry.grab_focus()
            return True
        if event.keyval == Gdk.KEY_F5:
            self.reload(manual=True)
            return True
        view = _treasure_map().shown(self)
        if view is not None and view.on_key(event):
            return True
        if self.reorder_mode and not isinstance(self.get_focus(), Gtk.Entry):
            action = self.ARROW_KEY_DIRECTIONS.get(event.keyval) or self.REORDER_KEY_ACTIONS.get(event.keyval)
            if action is not None and not event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK) \
                    and self._reorder_input(action):
                return True
            if type(self.get_focus()).__name__ in ("ReorderGrid", "LauncherOrderList"):
                return False    # no single-key shortcuts while moving covers
        if event.keyval == Gdk.KEY_Escape and self.selection_mode:
            self._exit_selection_mode()
            return True

        focus_widget = self.get_focus()
        if isinstance(focus_widget, (Gtk.Entry, Gtk.TextView)):
            return False
        if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
            return False
        if event.keyval in self.ARROW_KEY_DIRECTIONS:
            self._main_move_focus(self.ARROW_KEY_DIRECTIONS[event.keyval])
            return True
        if event.keyval in (Gdk.KEY_c, Gdk.KEY_C):
            self._toggle_console_mode()
            return True
        if event.keyval in (Gdk.KEY_d, Gdk.KEY_D):
            self.show_dashboard()
            return True
        if event.keyval in (Gdk.KEY_o, Gdk.KEY_O):
            self.show_options()
            return True
        if event.keyval in (Gdk.KEY_s, Gdk.KEY_S):
            self.show_shipwright()
            return True
        if event.keyval in (Gdk.KEY_t, Gdk.KEY_T):
            self._toggle_treasure_map()
            return True
        if event.keyval in (Gdk.KEY_a, Gdk.KEY_A) and not self.reorder_mode:
            # All Plunder (from the map too)
            self._set_home_nav("all")
            self._home_select_nav_row()
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter) and self._launch_focused_launcher():
            return True
        if event.keyval == Gdk.KEY_F1:
            self._show_shortcuts_dialog()
            return True
        if event.keyval == Gdk.KEY_Menu:
            return self._menu_for_focus()
        if event.keyval in (Gdk.KEY_f, Gdk.KEY_F):
            game = self._focused_game()
            if game is not None:
                self._toggle_favorite(game)
                # the library was drawn again: back onto the same game
                GLib.idle_add(self._refocus_game, game.uid)
            return game is not None
        if Gdk.KEY_1 <= event.keyval <= Gdk.KEY_9 and not self.reorder_mode:
            # the sidebar's places: All, Favorites, Recently sailed, then the launchers
            rows = [r for listbox in (self.home_nav_main, self.home_nav_launchers) if listbox.get_visible()
                    for r in listbox.get_children() if r.get_visible()]
            i = event.keyval - Gdk.KEY_1
            if i < len(rows):
                self._set_home_nav(rows[i].home_key)
                self._home_select_nav_row()
            return True
        if event.keyval in (Gdk.KEY_e, Gdk.KEY_E):
            if self.reorder_mode:
                self._exit_reorder_mode()
            elif self._cached_launchers:
                self._enter_reorder_mode()
            return True
        if event.keyval in (Gdk.KEY_v, Gdk.KEY_V) and not self.reorder_mode:
            if self.selection_mode:
                self._exit_selection_mode()
            else:
                self._enter_selection_mode()
            return True
        if event.keyval in (Gdk.KEY_g, Gdk.KEY_G, Gdk.KEY_l, Gdk.KEY_L) and not self.reorder_mode:
            # the grid and list views - as their buttons in the top bar do
            grid = event.keyval in (Gdk.KEY_g, Gdk.KEY_G)
            (self.view_grid_btn if grid else self.view_list_btn).set_active(True)
            return True
        if event.keyval in (Gdk.KEY_b, Gdk.KEY_B):
            self._on_scroll_top_clicked(None)
            return True
        return False

    def _focused_game(self) -> Optional[GameItem]:
        """The game the keyboard stands on: a cover, a list row or the
        hero's Set sail."""
        widget = self.get_focus()
        while widget is not None:
            uid = getattr(widget, "plunderhub_uid", None)
            if uid:
                return self.game_index.get(uid)
            widget = widget.get_parent()
        return None

    def _refocus_game(self, uid: str) -> bool:
        def find(widget):
            if getattr(widget, "plunderhub_uid", None) == uid:
                for w in [widget] + self._descendants_of(widget):
                    if isinstance(w, Gtk.Button) and w.get_can_focus() and w.get_mapped():
                        return w
            if isinstance(widget, Gtk.Container):
                for child in widget.get_children():
                    found = find(child)
                    if found is not None:
                        return found
            return None
        target = find(self.content)
        if target is not None:
            self._grab_keyboard_focus(target)
        return False

    @staticmethod
    def _descendants_of(widget) -> list:
        out = []
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                out.append(child)
                out.extend(App._descendants_of(child))
        return out

    def _launch_focused_launcher(self) -> bool:
        """Enter on a sidebar launcher's row opens the launcher, or closes it while it runs (a click only picks it -
        see _home_launcher_row)."""
        launch = getattr(self.get_focus(), "plunderhub_launch", None)
        return bool(launch and launch())

    def _menu_for_focus(self) -> bool:
        """The Menu key: the focused game's menu - or a launcher's, on its
        sidebar row - as a right-click would open it."""
        focus = self.get_focus()
        game = self._focused_game()
        if game is not None:
            from plunderhub.ui.game_page import GameMenu
            menu = GameMenu(self, game, focus)
            menu.popup()
            GLib.idle_add(lambda: menu.focus_first() and False)
            return True
        widget = focus
        while widget is not None:
            key = getattr(widget, "home_key", "")
            if key.startswith("l:"):
                menu = self._launcher_menu_at(widget, key[2:])
                GLib.idle_add(lambda: menu.focus_first() and False)
                return True
            widget = widget.get_parent()
        return False

    def _on_search_entry_key_press(self, widget, event):
        if event.keyval in self.ARROW_KEY_DIRECTIONS:
            self._main_move_focus(self.ARROW_KEY_DIRECTIONS[event.keyval])
            return True
        if event.keyval == Gdk.KEY_Escape:
            self.search_entry.set_text("")
            self.set_focus(None)
            return True
        return False

    def _focus_first_card(self):
        def find_first_focusable(widget):
            if isinstance(widget, Gtk.Button) and widget.get_can_focus() and widget.get_visible():
                return widget
            if hasattr(widget, "get_children"):
                for child in widget.get_children():
                    found = find_first_focusable(child)
                    if found:
                        return found
            return None

        target = find_first_focusable(self.content)
        if target:
            target.grab_focus()

    def _find_nav_position(self, widget: Optional[Gtk.Widget]) -> Tuple[Optional[int], Optional[str]]:
        if widget is None:
            return None, None
        for i, group in enumerate(self._nav_groups):
            if widget is group["launcher_widget"]:
                return i, "launcher"
            if self._direct_child_of(widget, group["container"]) is not None:
                return i, "container"
        return None, None

    def _nav_target_widget(self, group: Dict, child: Gtk.Widget) -> Gtk.Widget:
        if group["is_grid"]:
            inner = child.get_child()
            return getattr(inner, "plunderhub_banner_btn", None) or child
        return getattr(child, "plunderhub_row_btn", None) or child

    def _grid_neighbor(self, container: Gtk.FlowBox, direction: str) -> Optional[Gtk.FlowBoxChild]:
        children = container.get_children()
        if not children:
            return None
        current = self._direct_child_of(self.get_focus(), container)
        if current is None or current not in children:
            return children[0]
        cur_alloc = current.get_allocation()
        cx = cur_alloc.x + cur_alloc.width / 2
        cy = cur_alloc.y + cur_alloc.height / 2

        best, best_score = None, None
        for child in children:
            if child is current:
                continue
            alloc = child.get_allocation()
            x = alloc.x + alloc.width / 2
            y = alloc.y + alloc.height / 2
            dx, dy = x - cx, y - cy
            if direction == "left" and dx >= -1:
                continue
            if direction == "right" and dx <= 1:
                continue
            if direction == "up" and dy >= -1:
                continue
            if direction == "down" and dy <= 1:
                continue
            score = (abs(dy), abs(dx)) if direction in ("left", "right") else (abs(dx), abs(dy))
            if best_score is None or score < best_score:
                best_score, best = score, child
        return best

    def _focus_first_child_in_group(self, group: Dict) -> bool:
        if not group["container"].get_mapped():
            return False
        children = group["container"].get_children()
        if not children:
            return False
        self._grab_keyboard_focus(self._nav_target_widget(group, children[0]))
        return True

    def _focus_group_start(self, group_idx: int):
        if not (0 <= group_idx < len(self._nav_groups)):
            return
        group = self._nav_groups[group_idx]
        if group["launcher_widget"] is not None:
            self._grab_keyboard_focus(group["launcher_widget"])
        else:
            self._focus_first_child_in_group(group)

    def _focus_group_first_game(self, group_idx: int):
        if not (0 <= group_idx < len(self._nav_groups)):
            return
        group = self._nav_groups[group_idx]
        if not self._focus_first_child_in_group(group) and group["launcher_widget"] is not None:
            self._grab_keyboard_focus(group["launcher_widget"])

    def _child_focus_in_group(self, group: Dict, direction: str) -> bool:
        container = group["container"]
        if group["is_grid"]:
            target = self._grid_neighbor(container, direction)
            if target is None:
                return False
            self._grab_keyboard_focus(self._nav_target_widget(group, target))
            return True
        if direction not in ("up", "down"):
            return False
        rows = container.get_children()
        current_row = self._direct_child_of(self.get_focus(), container)
        if current_row is None or current_row not in rows:
            return False
        idx = rows.index(current_row)
        new_idx = idx - 1 if direction == "up" else idx + 1
        if not (0 <= new_idx < len(rows)):
            return False
        target = getattr(rows[new_idx], "plunderhub_row_btn", None) or rows[new_idx]
        self._grab_keyboard_focus(target)
        return True

    def _focus_first_in_nav(self):
        for group in self._nav_groups:
            if self._focus_first_child_in_group(group):
                return

    def _main_move_focus(self, direction: str):
        from plunderhub.ui import keynav
        if keynav.restore(self):
            return
        focused = self.get_focus()
        if self._home_move_focus(focused, direction):
            return
        view = _treasure_map().shown(self)
        if view is not None:
            # the map's tiles; off their left edge back to the sidebar
            if focused is not view.grid:
                view.grid.kb_enter()
            elif not view.grid.kb_move(direction) and direction == "left":
                self._home_focus_sidebar()
            return
        if not self._nav_groups:
            if direction == "left":
                self._home_focus_sidebar()
            return
        group_idx, kind = self._find_nav_position(focused)
        if group_idx is None:
            if not self._home_focus_content():
                self._focus_first_in_nav()
            return

        group = self._nav_groups[group_idx]
        if kind == "launcher":
            if group_idx == 0:
                self._focus_first_child_in_group(group)
                return
            if direction == "down":
                if not self._focus_first_child_in_group(group):
                    self._focus_group_start(group_idx + 1)
            elif direction == "up":
                self._focus_group_first_game(group_idx - 1)
            return

        if self._child_focus_in_group(group, direction):
            return
        if direction == "left":
            self._home_focus_sidebar()
        elif direction == "up":
            if group["launcher_widget"] is not None:
                self._grab_keyboard_focus(group["launcher_widget"])
            else:
                # The previous row that's showing (the Recently played row
                # can be folded away), else the hero's button.
                for prev in range(group_idx - 1, -1, -1):
                    if self._focus_first_child_in_group(self._nav_groups[prev]):
                        break
                else:
                    hero = getattr(self, "_home_hero_play", None)
                    if hero is not None and hero.get_mapped():
                        self._grab_keyboard_focus(hero)
        elif direction == "down":
            self._focus_group_start(group_idx + 1)

    def _populate_sort_popover(self):
        box = self._home_menu_box(self.sort_popover, heading=self._("sort_by"))
        view = _treasure_map().shown(self)
        if view is not None:
            view.fill_sort_menu(box, self.sort_popover)
            box.show_all()
            return
        for value, label in (("custom", self._("sort_custom")), ("alpha", self._("sort_alpha")),
                             ("newest", self._("sort_newest")), ("recent", self._("sort_recent")),
                             ("playtime", self._("sort_playtime"))):
            row = self._home_menu_row(None, label, checked=self.sort_mode == value)

            def on_clicked(_b, sort_value=value):
                self.sort_popover.popdown()
                if self.sort_mode != sort_value:
                    self.sort_mode = sort_value
                    self.settings["sort_mode"] = sort_value
                    save_settings(self.settings)
                    self._render_launchers(animate=True)

            row.connect("clicked", on_clicked)
            box.pack_start(row, False, False, 0)
        box.show_all()

    def _detect_resolution_size_tier(self) -> str:
        try:
            display = Gdk.Display.get_default()
            if display is None or display.get_n_monitors() == 0:
                return "medium"
            monitor = None
            get_primary = getattr(display, "get_primary_monitor", None)
            if callable(get_primary):
                monitor = get_primary()
            if monitor is None:
                monitor = display.get_monitor(0)
            if monitor is None:
                return "medium"
            geometry = monitor.get_geometry()
            width = geometry.width
            height = geometry.height
        except Exception as e:
            logger.debug(f"Couldn't detect monitor resolution for default size tier: {e}")
            return "medium"

        logger.debug(f"Detected monitor resolution {width}x{height} for default size tier")
        area = width * height
        hd_area = 1280 * 720
        fhd_area = 1920 * 1080
        qhd_area = 2560 * 1440
        if area < hd_area:
            return "tiny"
        if area < fhd_area:
            return "xsmall"
        if area == fhd_area:
            return "small"
        if area <= qhd_area:
            return "medium"
        return "large"

    def _detect_monitor_refresh_rate_hz(self) -> int:
        # az én gépemen 144 jön vissza, de nálad lehet más
        try:
            display = Gdk.Display.get_default()
            if display is None or display.get_n_monitors() == 0:
                return 60
            monitor = None
            get_primary = getattr(display, "get_primary_monitor", None)
            if callable(get_primary):
                monitor = get_primary()
            if monitor is None:
                monitor = display.get_monitor(0)
            if monitor is None:
                return 60
            millihertz = monitor.get_refresh_rate()
            if not millihertz:
                return 60
            return max(1, round(millihertz / 1000))
        except Exception as e:
            logger.debug(f"Couldn't detect monitor refresh rate: {e}")
            return 60

    def _load_api_key_with_migration(self) -> str:
        """The SteamGridDB API key lives in the OS keyring (via
        libsecret) now, not plain text in settings.json. One-time,
        one-directional migration: an existing plain-text key gets
        moved into the keyring and wiped from settings.json the first
        time this runs successfully - never the other way around, and
        never both places at once once migration succeeds (that would
        defeat the point of moving it off plain-text storage).

        If libsecret isn't available (missing dependency on an
        install that hasn't updated, or no keyring daemon running),
        this falls back to plain settings.json exactly like before -
        a key that's readable in a config file beats one that
        silently vanished."""
        if "steamgriddb_api_key" in self.settings:
            # A plain-text entry only ever exists when writing it to the
            # keyring failed (pre-keyring installs, or _save_api_key
            # while the keyring was unreachable), so it's always newer
            # than whatever the keyring holds and must win - even when
            # it's empty, i.e. the user cleared the key but the keyring
            # couldn't forget the old one at the time.
            pending_key = self.settings["steamgriddb_api_key"] or ""
            if secret_store.store_api_key(pending_key):
                self.settings.pop("steamgriddb_api_key", None)
                save_settings(self.settings)
                if pending_key:
                    logger.info("Moved the SteamGridDB API key into the OS keyring.")
            return pending_key
        return secret_store.load_api_key() or ""

    def _save_api_key(self, new_key: str) -> None:
        """Mirrors _load_api_key_with_migration's own policy: try the
        keyring first, and only keep a plain-text copy in settings.json
        if that isn't available - never both at once."""
        if secret_store.store_api_key(new_key):
            self.settings.pop("steamgriddb_api_key", None)
        else:
            self.settings["steamgriddb_api_key"] = new_key
        self.api_key = new_key
        privacy.add_secret(new_key)

    def _load_settings_into_attrs(self):
        self.api_key = self._load_api_key_with_migration()
        privacy.add_secret(self.api_key)
        self.lang = self.settings.get("lang", "en")
        if self.lang not in LANGUAGE_ORDER:
            self.lang = "en"
        self._init_strings()
        self.show_covers = self.settings.get("show_covers", True)
        size_tier = self._detect_resolution_size_tier()
        self.cover_size = max(200, self.settings.get(
            "cover_size", RESOLUTION_TIER_COVER_SIZE[size_tier]))
        self.console_cover_size = max(200, self.settings.get(
            "console_cover_size", RESOLUTION_TIER_COVER_SIZE[size_tier]))
        self.console_ui_scale = self.settings.get(
            "console_ui_scale", RESOLUTION_TIER_CONSOLE_UI_SCALE[size_tier])
        if self.console_ui_scale not in CONSOLE_UI_SCALE_CSS_PERCENT:
            self.console_ui_scale = RESOLUTION_TIER_CONSOLE_UI_SCALE[size_tier]
        self.font_scale = self.settings.get(
            "font_scale", RESOLUTION_TIER_FONT_SCALE[size_tier])
        if self.font_scale == "normal":
            self.font_scale = "medium"
        if self.font_scale not in FONT_SCALE_ORDER:
            self.font_scale = RESOLUTION_TIER_FONT_SCALE[size_tier]
        self.game_icon_size = self.settings.get(
            "game_icon_size", RESOLUTION_TIER_GAME_ICON_SIZE[size_tier])
        if self.game_icon_size not in GAME_ICON_SIZE_LABEL_KEYS:
            self.game_icon_size = RESOLUTION_TIER_GAME_ICON_SIZE[size_tier]
        # On unless switched off - every old frame-rate mode played them.
        self.animated_covers = True     # animated art always plays (Settings has no switch for it any more)
        self.show_bubbles = self.settings.get("show_bubbles", True)
        self.pause_animations_when_idle = self.settings.get("pause_animations_when_idle", True)
        self.dim_background_for_dialogs = self.settings.get("dim_background_for_dialogs", True)
        self.show_card_menu_button = self.settings.get("show_card_menu_button", True)
        self.show_game_titles = self.settings.get("show_game_titles", True)
        self.auto_close = self.settings.get("auto_close", False)
        self.enabled_launchers = set(self.settings.get("enabled_launchers", []))
        self.launcher_order = self.settings.get("launcher_order", [])
        self.game_order = self.settings.get("game_order", {})
        # All Plunder's own custom order, apart from the launchers'
        self.all_game_order = self.settings.get("all_game_order", [])
        self.use_tray = self.settings.get("use_tray", True)
        self.run_on_startup = self.settings.get("run_on_startup", False)
        self.show_shortcuts_help_button = self.settings.get("show_shortcuts_help_button", True)
        self.scroll_top_button_enabled = self.settings.get("scroll_top_button_enabled", True)
        self.show_playtime = self.settings.get("show_playtime", True)
        self.sort_mode = self.settings.get("sort_mode", "alpha")
        if self.sort_mode not in ("custom", "alpha", "playtime", "recent", "newest"):
            self.sort_mode = "alpha"
        self.minimize_on_launch = self.settings.get("minimize_on_launch", False)
        self.launch_on_next_workspace = self.settings.get("launch_on_next_workspace", True)
        # the compositor-made window modes are gone (a game's own launch
        # arguments now) - their old setting with them
        self.settings.pop("window_modes", None)
        self.launch_mode = self.settings.get("launch_mode", "confirm")
        if self.launch_mode not in ("instant", "confirm"):
            self.launch_mode = "confirm"
        self.theme = self.settings.get("theme", "plunderhub")
        # A retired theme becomes the closest one still there.
        self.theme = THEME_RENAMES.get(self.theme, self.theme)
        if self.theme not in THEME_ORDER:
            self.theme = "plunderhub"
        self.card_view_mode = self.settings.get("card_view_mode", "grid")
        if self.card_view_mode not in ("grid", "list"):
            self.card_view_mode = "grid"
        if self.minimize_on_launch:
            self.use_tray = True

    def _init_strings(self):
        self.strings = STRINGS.get(self.lang, STRINGS["en"])

    def _(self, key: str) -> str:
        return self.strings.get(key, key)

    def _maybe_prompt_first_run_language(self):
        """The very first start: a card with the languages as tiles. The
        system's language is picked to begin with, and the card's own
        words follow whichever one is picked."""
        # (a self-test on a fresh profile would wait on it for good)
        if "lang" in self.settings or "--self-test" in sys.argv:
            return
        self._load_css()
        self._apply_theme(self.theme)
        dlg, column = card_dialog(None, "Language")
        column.set_spacing(8)
        icon = Gtk.Image()
        mark_logo(icon, 72)
        column.pack_start(icon, False, False, 0)
        title = Gtk.Label()
        title.get_style_context().add_class("home-dialog-title")
        column.pack_start(title, False, False, 0)
        sub = Gtk.Label()
        sub.get_style_context().add_class("dim-label")
        column.pack_start(sub, False, False, 0)

        system = (os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or "")
        system = system.split(".")[0].lower()
        picked = {"code": next((c for c in (system, system.split("_")[0]) if c in LANGUAGE_NATIVE_NAMES), "en")}
        grid = Gtk.Grid(column_spacing=8, row_spacing=8, column_homogeneous=True)
        grid.set_margin_top(14)
        tiles: Dict[str, Gtk.ToggleButton] = {}
        note = Gtk.Label()
        note.set_line_wrap(True)
        note.get_style_context().add_class("dim-label")
        note.get_style_context().add_class("welcome-small")
        go = self._dialog_button("", "accent")

        def show(code: str):
            picked["code"] = code
            strings = STRINGS.get(code, STRINGS["en"])
            title.set_text(strings.get("welcome_lang_title", STRINGS["en"]["welcome_lang_title"]))
            sub.set_text(strings.get("welcome_lang_sub", STRINGS["en"]["welcome_lang_sub"]))
            note.set_text(strings.get("ai_translation_note", "") if code in AI_TRANSLATED_LANGUAGES else "")
            go.get_child().get_children()[0].set_text(strings.get("welcome_continue", "Continue"))
            for other, tile in tiles.items():
                if tile.get_active() != (other == code):
                    tile.set_active(other == code)

        for i, code in enumerate(LANGUAGE_ORDER):
            tile = Gtk.ToggleButton(label=LANGUAGE_NATIVE_NAMES[code])
            tile.get_style_context().add_class("install-choice")
            tile.get_style_context().add_class("lang-tile")
            tile.connect("toggled", lambda b, c=code: b.get_active() and c != picked["code"] and show(c)
                         or (not b.get_active() and c == picked["code"] and b.set_active(True)))
            tiles[code] = tile
            grid.attach(tile, i % 3, i // 3, 1, 1)
        column.pack_start(grid, False, False, 0)
        foot = Gtk.Box(spacing=12)
        foot.set_margin_top(14)
        note.set_xalign(0)
        foot.pack_start(note, True, True, 0)
        go.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.OK))
        foot.pack_end(go, False, False, 0)
        column.pack_start(foot, False, False, 0)
        tiles[picked["code"]].set_active(True)
        show(picked["code"])

        def on_key(_w, event) -> bool:
            if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Escape):
                dlg.response(Gtk.ResponseType.OK)
                return True
            return False
        dlg.connect("key-press-event", on_key)
        dlg.get_content_area().show_all()
        dlg.run()
        chosen = picked["code"]
        dlg.destroy()

        self.lang = chosen
        self.settings["lang"] = chosen
        save_settings(self.settings)
        self._init_strings()

    def _maybe_show_first_run_tip(self):
        """The welcome card, by itself once: on the very first start (or
        never, for anyone who ticked "don't show again" back when it came
        up every time). Clicking the PlunderHub wordmark brings it back (_show_welcome)."""
        from plunderhub.ui import captains_log
        if self.settings.get("welcome_shown") or self.settings.get("tip_hide"):
            # started before: after an update, the Captain's Log (once)
            captains_log.after_start(self)
            return
        # a first install: the welcome tour greets - no log of changes
        # for it, now or at the next start
        self.settings[captains_log.SEEN_KEY] = APP_VERSION
        save_settings(self.settings)
        # seen only once watched through (Set sail!): closed by force, it
        # comes again at the next start
        if self._show_welcome(forced=True):
            self.settings["welcome_shown"] = True
            save_settings(self.settings)

    def _show_welcome(self, forced: bool = False) -> bool:
        """The welcome tour (ui/welcome_tour.py); forced: the first
        start's, watched through. True once Set sail! closed it."""
        from plunderhub.ui import welcome_tour
        return welcome_tour.Tour(self, forced).run()

    def _apply_theme(self, theme: str):
        gtk_settings = Gtk.Settings.get_default()
        orig_prefer_dark, orig_theme_name = _capture_original_theme_preference()

        screen = Gdk.Screen.get_default()
        if self._theme_css_provider is not None and screen is not None:
            try:
                Gtk.StyleContext.remove_provider_for_screen(screen, self._theme_css_provider)
            except Exception as e:
                logger.debug(f"Couldn't remove old theme CSS provider: {e}")
            self._theme_css_provider = None

        palette = THEME_PALETTES.get(theme)
        if palette is not None:
            if gtk_settings is not None:
                try:
                    gtk_settings.set_property("gtk-theme-name", "Adwaita")
                    gtk_settings.set_property("gtk-application-prefer-dark-theme", bool(palette.get("dark", True)))
                except Exception as e:
                    logger.warning(f"Failed to apply theme: {e}")
            css_text = _build_theme_css(palette)
        else:
            if gtk_settings is not None:
                try:
                    if orig_theme_name:
                        gtk_settings.set_property("gtk-theme-name", orig_theme_name)
                    gtk_settings.set_property("gtk-application-prefer-dark-theme", orig_prefer_dark)
                except Exception as e:
                    logger.warning(f"Failed to apply theme: {e}")
            css_text = _accent_css(DEFAULT_ACCENT, DEFAULT_ACCENT_CHECK, DEFAULT_ACCENT_SOFT,
                                   DEFAULT_ACCENT_SOFT_HOVER, DEFAULT_ACCENT_RGB,
                                   card_shadow="0 2px 8px rgba(0, 0, 0, 0.40)",
                                   card_shadow_active="0 6px 20px rgba(0, 0, 0, 0.50)")
            css_text += settings_css_for_system()

        if screen is not None:
            provider = Gtk.CssProvider()
            try:
                self._watch_css_errors(provider)
                provider.load_from_data(css_text.encode())
                Gtk.StyleContext.add_provider_for_screen(
                    screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
                )
                self._theme_css_provider = provider
            except Exception as e:
                logger.warning(f"Failed to load theme CSS: {e}")

        set_logo_accent(palette["accent"] if palette is not None else DEFAULT_ACCENT)
        self._refresh_icon_tints(theme)
        self._set_bubble_colours(palette)

    def _set_bubble_colours(self, palette: Optional[Dict]):
        """The bubbles take the theme's accent - tinted glass on a dark
        theme, the accent itself on a light one. The system theme lends
        its own selection and text colours."""
        def rgb(hex_colour: str) -> Tuple[float, float, float]:
            h = hex_colour.lstrip("#")
            return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        if palette is not None:
            accent, fg, dark = rgb(palette["accent"]), rgb(palette["fg"]), bool(palette.get("dark", True))
        else:
            ctx = self.get_style_context()
            found, colour = ctx.lookup_color("theme_selected_bg_color")
            accent = (colour.red, colour.green, colour.blue) if found else rgb(DEFAULT_ACCENT)
            text = ctx.get_color(ctx.get_state())
            fg = (text.red, text.green, text.blue)
            # Light text means a dark theme.
            dark = 0.2126 * fg[0] + 0.7152 * fg[1] + 0.0722 * fg[2] > 0.5
        field = getattr(self, "_bubble_field", None)
        if field is None:
            # Too early: the very first start themes the language prompt
            # before the window's parts exist. The field takes its
            # colours when it's made.
            return
        field.set_theme(accent, fg, dark)
        self._redraw_all_bubbles()

    @staticmethod
    def _parse_font_point_size(font_name: Optional[str]) -> Optional[float]:
        if not font_name:
            return None
        try:
            return float(font_name.strip().rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            return None

    def _base_font_point_size(self) -> float:
        font_name = None
        try:
            settings = Gtk.Settings.get_default()
            font_name = settings.get_property("gtk-font-name") if settings is not None else None
        except Exception as e:
            logger.debug(f"Couldn't read the system default font size: {e}")
        size = self._parse_font_point_size(font_name)
        return size if size is not None else 10.0

    def _text_factor(self) -> float:
        """The picked font size against "Large", the size the hand-drawn
        text (cards, the Dashboard) was laid out at."""
        shown = getattr(self, "_shown_font_scale", None) or self.font_scale
        return max(0.7, FONT_SCALE_CSS_PERCENT.get(shown, 100) / FONT_SCALE_CSS_PERCENT["large"])

    def _apply_font_scale(self, font_scale: str):
        screen = Gdk.Screen.get_default()
        if screen is None:
            return
        if self._font_css_provider is not None:
            try:
                Gtk.StyleContext.remove_provider_for_screen(screen, self._font_css_provider)
            except Exception as e:
                logger.debug(f"Couldn't remove old font-size CSS provider: {e}")
            self._font_css_provider = None
        # as shown - the Settings slider previews before it's saved
        self._shown_font_scale = font_scale
        percent = FONT_SCALE_CSS_PERCENT.get(font_scale, 100)
        size_px = self._base_font_point_size() * (96.0 / 72.0) * (percent / 100.0)
        css_text = (
            "window, .background, menu, menuitem "
            f"{{ font-size: {size_px:.2f}px; }}"
        )
        provider = Gtk.CssProvider()
        try:
            self._watch_css_errors(provider)
            provider.load_from_data(css_text.encode())
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
            )
            self._font_css_provider = provider
        except Exception as e:
            logger.warning(f"Failed to load font-size CSS: {e}")
        # the hand-drawn text follows along
        self.queue_draw()
        # bigger text may no longer fit the screen with the full top bar
        if self.get_mapped() and hasattr(self, "_topbar_narrow"):
            GLib.idle_add(lambda: (self._apply_compact_topbar(), False)[1])

    # the card dialogs' own corners (.home-launch-dialog in theme.py)
    DIALOG_CORNER_RADIUS = 20

    def _match_compositor_corners(self):
        """Where the compositor rounds every window itself (Hyprland), the
        card dialogs take its radius: their own wider corners would leave
        a wedge of whatever is behind between the two curves."""
        mover = self.workspace_mover()
        radius = mover.compositor.corner_radius() if mover is not None else None
        screen = Gdk.Screen.get_default()
        if radius is None or screen is None:
            return
        self.dialog_corner_radius = radius
        provider = Gtk.CssProvider()
        try:
            self._watch_css_errors(provider)
            provider.load_from_data(f".home-launch-dialog {{ border-radius: {radius}px; }}".encode())
            Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        except Exception as e:
            logger.warning(f"Failed to match the compositor's window corners: {e}")

    def _apply_console_ui_scale(self, ui_size: int):
        screen = Gdk.Screen.get_default()
        if screen is None:
            return
        if self._console_ui_css_provider is not None:
            try:
                Gtk.StyleContext.remove_provider_for_screen(screen, self._console_ui_css_provider)
            except Exception as e:
                logger.debug(f"Couldn't remove old Console Mode UI-scale CSS provider: {e}")
            self._console_ui_css_provider = None
        percent = CONSOLE_UI_SCALE_CSS_PERCENT.get(ui_size, 100)
        css_text = f".console-ui-scale-root {{ font-size: {percent}%; }}"
        provider = Gtk.CssProvider()
        try:
            self._watch_css_errors(provider)
            provider.load_from_data(css_text.encode())
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
            )
            self._console_ui_css_provider = provider
        except Exception as e:
            logger.warning(f"Failed to load Console Mode UI-size CSS: {e}")

        for glyph, size_provider, base_diameter, shoulder in self._console_topbar_glyphs:
            self._console_size_glyph(glyph, size_provider, base_diameter, shoulder)
        if hasattr(self, "console_view"):
            self.console_view.queue_draw()

    def _start_bubble_animation(self):
        # switched off, they don't keep the frame clock running for nothing
        if self._bubble_tick_id is None and getattr(self, "show_bubbles", True):
            self._bubble_last_frame_us = None
            self._bubble_tick_id = self.bubble_canvas.add_tick_callback(self._tick_bubbles)

    def _pause_bubble_animation(self):
        if self._bubble_tick_id is not None:
            self.bubble_canvas.remove_tick_callback(self._bubble_tick_id)
            self._bubble_tick_id = None

    def _resume_bubble_animation(self):
        self._start_bubble_animation()

    @staticmethod
    def _maybe_enable_anim_pause_debug_logging() -> None:
        if os.environ.get("PLUNDERHUB_ANIM_PAUSE_DEBUG") == "1":
            logger.setLevel(logging.DEBUG)

    def _is_game_running(self, uid: str) -> bool:
        """Started from PlunderHub, or noticed running from its own launcher."""
        return uid in self.running_procs or uid in self.detected_running

    def _any_game_running(self) -> bool:
        return bool(self.running_procs or self.detected_running)

    def _should_pause_decorative_animations(self) -> bool:
        if not self.pause_animations_when_idle:
            return False
        if self.running_procs or getattr(self, "detected_running", None):
            return True
        if self._window_iconified:
            return True
        return not self.is_active()

    def _should_pause_decorative_animations_for(self, extra_window: Optional[Gtk.Window] = None) -> bool:
        # a picker dialog stealing focus shouldn't count as "unfocused"
        if not self.pause_animations_when_idle:
            return False
        if self.running_procs or getattr(self, "detected_running", None):
            return True
        if self._window_iconified:
            return True
        if self.is_active():
            return False
        if extra_window is not None and extra_window.is_active():
            return False
        return True

    def _should_show_animations_paused_banner(self) -> bool:
        return self._should_pause_decorative_animations() and not self._window_iconified

    def _enter_dimmed_dialog(self):
        # counts overlapping sources (dialog + popover could both fire)
        # so dimming only actually lifts once everything has closed
        self._dim_depth = getattr(self, "_dim_depth", 0) + 1
        self._dimming_dialog_open = True
        if self._dim_depth > 1:
            return
        if self.dim_background_for_dialogs and hasattr(self, "dialog_dim_scrim"):
            if self.dialog_dim_scrim.get_parent() is None:
                self.main_overlay.add_overlay(self.dialog_dim_scrim)
                self.main_overlay.set_overlay_pass_through(self.dialog_dim_scrim, True)
            self.dialog_dim_scrim.show()
        self._sync_startup_spinner()
        self._update_animations_paused_banner()

    def _exit_dimmed_dialog(self):
        self._dim_depth = max(0, getattr(self, "_dim_depth", 0) - 1)
        if self._dim_depth > 0:
            return
        self._dimming_dialog_open = False
        if hasattr(self, "dialog_dim_scrim") and self.dialog_dim_scrim.get_parent() is not None:
            self.main_overlay.remove(self.dialog_dim_scrim)
        self._sync_startup_spinner()
        self._update_animations_paused_banner()

    def _animations_paused_reason_text(self) -> str:
        if self.running_procs or getattr(self, "detected_running", None):
            return self._("animations_paused_short_game_running")
        return self._("animations_paused_short_window_inactive")

    def _update_decorative_animation_pause_state(self):
        paused = self._should_pause_decorative_animations()
        logger.debug(
            f"[ANIM_PAUSE_DEBUG] recheck: running_procs={bool(self.running_procs)} "
            f"iconified={self._window_iconified} is_active={self.is_active()} "
            f"-> paused={paused} (was {self._decorative_animations_paused})"
        )
        self._decorative_animations_paused = paused
        # Each animation's frame callback runs only while it has something
        # to do: one left registered wakes the app every frame of the
        # monitor even when it draws nothing.
        # (hidden too - behind Console Mode they went on stepping unseen)
        if paused or not self.show_bubbles or not self.bubble_canvas.get_mapped():
            self._pause_bubble_animation()
        else:
            self._resume_bubble_animation()
        if not paused:
            self._anim_player.wake()
            from plunderhub.ui import animated_hero
            animated_hero.wake_all()
        self._sync_startup_spinner()
        self._update_animations_paused_banner()

    def _sync_startup_spinner(self):
        """The loading spinner turns only where it's seen: under a dialog
        or in a window in the background it repainted the whole window
        every frame - for the full 30 s when the welcome card came up
        at the first start (the covers wait for the window meanwhile)."""
        if getattr(self, "startup_overlay", None) is None:
            return
        if self._decorative_animations_paused or getattr(self, "_dim_depth", 0):
            self.startup_spinner.stop()
        else:
            self.startup_spinner.start()

    def _update_animations_paused_banner(self):
        if not hasattr(self, "animations_paused_bar"):
            return
        show = self._should_show_animations_paused_banner()
        text = self._animations_paused_reason_text() if show else None
        logger.debug(f"[ANIM_PAUSE_DEBUG] banner: show={show}")
        copies = [("desktop", self.animations_paused_bar, self.animations_paused_label)]
        console_bar = getattr(self, "console_animations_paused_bar", None)
        console_label = getattr(self, "console_animations_paused_label", None)
        if console_bar is not None:
            copies.append(("console", console_bar, console_label))
        for name, bar, label in copies:
            try:
                if show and text is not None:
                    label.set_text(text)
                    # Set here too, so a language change reaches it.
                    bar.plunderhub_title.set_text(self._("animations_paused_title"))
                bar.set_reveal_child(show)
                logger.debug(
                    f"[ANIM_PAUSE_DEBUG] {name} banner widget state after update: "
                    f"visible={bar.get_visible()} mapped={bar.get_mapped()} "
                    f"reveal_child={bar.get_reveal_child()} "
                    f"child_revealed={bar.get_child_revealed()} "
                    f"allocation={bar.get_allocation()}"
                )
            except Exception as e:
                logger.exception(f"[ANIM_PAUSE_DEBUG] Failed to update {name} animations_paused_bar: {e}")

    def _on_window_state_changed(self, _widget, event) -> bool:
        self._maybe_enable_anim_pause_debug_logging()
        iconified = bool(event.new_window_state & Gdk.WindowState.ICONIFIED)
        logger.debug(f"[ANIM_PAUSE_DEBUG] window-state-event: iconified={iconified}")
        if iconified != self._window_iconified:
            self._window_iconified = iconified
            self._update_decorative_animation_pause_state()
        # _toggle_maximize() (our own maximize button) already updates
        # _is_maximized and its icon directly, but that's only one of
        # several ways the actual window state can change - double-
        # clicking this HeaderBar (GTK's own built-in behavior for
        # any titlebar set via set_titlebar, regardless of whether we
        # draw close/minimize/maximize buttons on it ourselves) and
        # window-manager shortcuts (e.g. Super+Up) both maximize/
        # restore the window without ever going through our button at
        # all. Without this, _is_maximized silently drifts out of sync
        # with the real state, and the button's icon keeps showing
        # what it USED to be able to do rather than what clicking it
        # will actually do next.
        maximized = bool(event.new_window_state & Gdk.WindowState.MAXIMIZED)
        if maximized != self._is_maximized:
            self._is_maximized = maximized
            icon_name = "window-restore-symbolic" if maximized else "window-maximize-symbolic"
            self.maximize_btn.set_image(self._icon_image(icon_name, Gtk.IconSize.BUTTON))
        # Same class of gap, for Console Mode: it uses self.fullscreen()
        # internally, but a window-manager fullscreen shortcut (or an
        # F11-style binding some WMs apply globally) can un-fullscreen
        # the window without ever calling _exit_console_mode() - console_
        # mode_active would then stay stuck True while the window quietly
        # isn't fullscreen anymore, leaving the header bar/scroller/etc
        # hidden with no clean way back short of restarting. Only the
        # "externally left fullscreen while still marked active" case is
        # handled - the window becoming fullscreen from some unrelated
        # cause must NOT auto-enter Console Mode, since that's a real,
        # deliberate mode switch this app makes, not just a window
        # decoration state.
        # only a real change of fullscreen: a focus / tiling event before
        # the fullscreen lands has it unset too
        fullscreen = bool(event.new_window_state & Gdk.WindowState.FULLSCREEN)
        if not fullscreen and self.console_mode_active and event.changed_mask & Gdk.WindowState.FULLSCREEN:
            if self._console_hidden_for_game or self._any_game_running() or self.launching_uids:
                # A game going fullscreen took it (Hyprland, among others,
                # keeps one fullscreen window per workspace) - Console
                # Mode stays, and is fullscreen again once back.
                self._console_lost_fullscreen = True
            else:
                self._exit_console_mode()
        return False

    def _on_window_active_changed(self, _widget, _pspec):
        self._maybe_enable_anim_pause_debug_logging()
        logger.debug(f"[ANIM_PAUSE_DEBUG] notify::is-active fired: is_active={self.is_active()}")
        self._update_decorative_animation_pause_state()

    def _on_scroll_value_changed(self, adjustment):
        # Back to top's glide included: the bubbles follow it down.
        value = adjustment.get_value()
        if self._last_scroll_pos is not None:
            self._bubble_field.on_scroll(value - self._last_scroll_pos)
        self._last_scroll_pos = value
        # an animated cover - or a list row's animated hero - may have come into view
        self._anim_player.wake()
        from plunderhub.ui import animated_hero
        animated_hero.wake_all()
        self._occluders_cache = None

    def _redraw_all_bubbles(self):
        """The next frame repaints the whole canvas, not just the spots
        bubbles moved through - after a resize, a theme change, or the
        bubbles being switched off."""
        self._bubble_full_redraw = True
        if hasattr(self, "bubble_canvas"):
            self.bubble_canvas.queue_draw()

    def _bubble_pointer(self) -> Optional[Tuple[float, float]]:
        """Where the pointer is over the bubble canvas - the events the
        cards on top get are in the scrolled content's coordinates."""
        win = self.bubble_canvas.get_window()
        display = Gdk.Display.get_default()
        seat = display.get_default_seat() if display is not None else None
        if win is None or seat is None or not self.is_active():
            return None
        _w, x, y, _mask = win.get_device_position(seat.get_pointer())
        alloc = self.bubble_canvas.get_allocation()
        if 0 <= x < alloc.width and 0 <= y < alloc.height:
            return (x, y)
        return None

    def _tick_bubbles(self, widget, frame_clock) -> bool:
        """Once per frame of the monitor. Calm, the field steps on a beat
        of about 60 fps - every 3rd frame at 165 Hz, always the same gap,
        or the slow bubbles would judder; while the pointer moves or a
        bubble is pushed, on every frame. Motion is counted in 60 fps
        frames of elapsed time, so it's the same pace at any rate; only
        the spots bubbles were and are now get redrawn."""
        if not self.show_bubbles:
            self._bubble_last_frame_us = None
            return True
        field = self._bubble_field
        beat = frame_clock.get_frame_counter() % self._bubble_beat_frames(frame_clock) == 0
        if not beat and not field.lively and not self._bubble_full_redraw:
            mouse = self._bubble_pointer()
            if not field.pointer_moved(mouse):
                return True
        now_us = frame_clock.get_frame_time()
        last = self._bubble_last_frame_us
        frames = min(4.0, (now_us - last) / 1_000_000 * 60) if last is not None and now_us > last else 1.0
        self._bubble_last_frame_us = now_us
        t = now_us / 1_000_000
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        field.mouse = self._bubble_pointer()
        field.tick(frames, t, w, h)
        self._bubble_frame_t = t
        full = self._bubble_full_redraw
        self._bubble_full_redraw = False
        areas = field.redraw_areas(t, w, h, beat, full)
        if full:
            widget.queue_draw()
            return True
        # A bubble behind an opaque cover or the hero can't be seen:
        # redrawing its spot would only repaint the art over it.
        hidden = self._bubble_occluders()
        for x, y, rw, rh in areas:
            if not any(ox <= x and oy <= y and x + rw <= ox + ow and y + rh <= oy + oh
                       for ox, oy, ow, oh in hidden):
                widget.queue_draw_area(x, y, rw, rh)
        return True

    def _bubble_beat_frames(self, frame_clock) -> int:
        """Every how many frames of the monitor the calm bubbles step - and
        the card lift, the animated covers and heroes with them, so they all
        change in the same repaints: the whole number that comes nearest
        60 fps (165 Hz: 3, 144 Hz: 2).
        Looked up again every few seconds - the window may move to
        another monitor."""
        now = frame_clock.get_frame_time()
        cached = getattr(self, "_bubble_beat", None)
        if cached is not None and now - cached[1] < 3_000_000:
            return cached[0]
        rate = 0.0
        win = self.bubble_canvas.get_window()
        if win is not None:
            monitor = win.get_display().get_monitor_at_window(win)
            if monitor is not None:
                rate = monitor.get_refresh_rate() / 1000
        frames = max(1, round(rate / 60)) if rate > 0 else 1
        self._bubble_beat = (frames, now)
        return frames

    # how far in from an occluder's edges a bubble counts as hidden: the
    # rounded corners, and a hovered card rising by 10 px
    _OCCLUDER_INSET = 12

    @staticmethod
    def _image_size(image: Gtk.Image) -> Tuple[int, int]:
        """What an Image shows, in its own pixels - (0, 0) when unknown."""
        storage = image.get_storage_type()
        if storage == Gtk.ImageType.PIXBUF and image.get_pixbuf() is not None:
            pb = image.get_pixbuf()
            return pb.get_width(), pb.get_height()
        if storage == Gtk.ImageType.SURFACE and image.props.surface is not None:
            surface = image.props.surface
            sx, sy = surface.get_device_scale()
            return int(surface.get_width() / sx), int(surface.get_height() / sy)
        return 0, 0

    def _bubble_occluders(self) -> List[Tuple[int, int, int, int]]:
        """The opaque covers' and the hero's areas on the bubble canvas,
        shrunk to be sure - worked out at most ten times a second."""
        now = time.monotonic()
        cached = getattr(self, "_occluders_cache", None)
        if cached is not None and now - cached[0] < 0.1:
            return cached[1]
        canvas = self.bubble_canvas
        cw, ch = canvas.get_allocated_width(), canvas.get_allocated_height()
        inset = self._OCCLUDER_INSET
        rects = []
        widgets = [w for uid, ws in self.cover_widgets.items() if self._cover_opaque.get(uid) for w in ws]
        hero = getattr(self, "_home_hero_area", None)
        if hero is not None:
            widgets.append(hero)
        for widget in widgets:
            try:
                if not widget.get_mapped():
                    continue
                coords = widget.translate_coordinates(canvas, 0, 0)
            except Exception:
                continue
            if coords is None:
                continue
            x, y = coords
            aw, ah = widget.get_allocated_width(), widget.get_allocated_height()
            if isinstance(widget, Gtk.Image):
                # the picture itself, centred in what may be a bigger box
                pw, ph = self._image_size(widget)
                pw, ph = min(pw, aw), min(ph, ah)
                x, y, aw, ah = x + (aw - pw) // 2, y + (ah - ph) // 2, pw, ph
            w, h = aw - 2 * inset, ah - 2 * inset
            if w > 0 and h > 0 and x + inset < cw and y + inset < ch and x + w > 0 and y + h > 0:
                rects.append((x + inset, y + inset, w, h))
        self._occluders_cache = (now, rects)
        return rects

    def _on_bubble_canvas_draw(self, widget, cr):
        with diagnostics.section("bubble draw"):
            return self._draw_bubble_field(widget, cr)

    def _draw_bubble_field(self, widget, cr):
        if not self.show_bubbles:
            return False
        w = widget.get_allocated_width()
        h = widget.get_allocated_height()
        if w <= 0 or h <= 0:
            return False
        self._bubble_field.draw(cr, self._bubble_frame_t, w, h)
        return False

    def _watch_css_errors(self, provider: Gtk.CssProvider) -> None:
        def on_parsing_error(_provider, section, error):
            try:
                message = getattr(error, "message", None) or str(error)
                logger.error(f"CSS rejected by GTK: {message}")
            except Exception:
                logger.error("CSS rejected by GTK (details unavailable)")
            try:
                from plunderhub import selftest
                selftest.record_css_error(section, error)
            except Exception:
                pass

        try:
            provider.connect("parsing-error", on_parsing_error)
        except Exception as e:
            logger.debug(f"Couldn't watch for CSS parse errors: {e}")

    def _load_css(self):
        css_provider = Gtk.CssProvider()
        font_css = f'window, .background {{ font-family: "{FONT_FAMILY}", sans-serif; }}\n'
        css = font_css + """
        /* Generic, clearly-visible focus indicator for ordinary dialog
           buttons (Yes/No confirm prompts, error dialogs, the Save/
           Close buttons in Settings, ...) - this app's custom dark
           theme can make the desktop's own default focus rendering too
           subtle to see, which is what made keyboard/controller
           navigation in these dialogs hard to follow (no visible
           feedback for which button is currently selected) - important
           enough to get its own dedicated, deliberately bold treatment
           (thick border + soft glow + a filled background) rather than
           something that could still blend into the surroundings on
           some themes, since a controller has no cursor to fall back
           on - this outline IS the only way to tell which option is
           currently selected.
           "dialog" and "messagedialog" are Gtk.Dialog's and
           Gtk.MessageDialog's own distinct CSS node names respectively
           (confirmed via get_style_context().get_path()) - EVERY plain
           Yes/No/OK confirmation and error prompt in this app (see
           _show_info_dialog, request_launch's launch-confirmation, ...)
           is a Gtk.MessageDialog specifically, so a selector matching
           only "dialog" (as this rule originally did) silently never
           matched any of them at all - only the separate, plain
           Gtk.Dialog-based windows (Options, About, ...) actually got
           highlighted focus before this fix. */
        /* Generic, clearly-visible focus indicator for dialog controls
           (Yes/No confirm prompts, error dialogs, and - most of all -
           every Settings tab's checkboxes, radio buttons, dropdowns,
           text entries and sliders) - this app's custom dark theme can
           make the desktop's own default focus rendering too subtle
           to see, which is what made keyboard/controller navigation in
           these dialogs hard to follow (no visible feedback for which
           option is currently selected) - important enough to get its
           own dedicated, deliberately bold treatment (thick border +
           soft glow + a filled background on plain buttons; border +
           glow, without the fill, on everything else, so a checked
           checkbox/radio dot or typed entry text still reads clearly
           against it) rather than something that could still blend
           into the surroundings on some themes, since a controller has
           no cursor to fall back on - this outline IS the only way to
           tell which option is currently selected. Covers Settings'
           four Gtk.Scale sliders (Cover size, Console Mode cover size,
           Console Mode UI size, Font size) too, now that the right
           stick's left/right handling in
           App._handle_dialog_joystick_event actually lets a controller
           adjust them - a slider a controller can't even tell it has
           landed on would still be just as unusable in practice.
           "dialog" and "messagedialog" are Gtk.Dialog's and
           Gtk.MessageDialog's own distinct CSS node names respectively
           (confirmed via get_style_context().get_path()) - EVERY plain
           Yes/No/OK confirmation and error prompt in this app (see
           _show_info_dialog, request_launch's launch-confirmation, ...)
           is a Gtk.MessageDialog specifically, so a selector matching
           only "dialog" (as this rule originally did) silently never
           matched any of them at all - only the separate, plain
           Gtk.Dialog-based windows (Options, About, ...) actually got
           highlighted focus before that fix. checkbutton/radiobutton/
           combobox/entry/scale are each their OWN distinct CSS node
           name, separate from plain "button" despite GtkCheckButton/
           GtkRadioButton being GtkButton subclasses under the hood -
           none of them were matched by the button-only selector this
           rule started as, which is why Settings' other controls
           still had no visible focus state at all even after that
           first fix. */
        /* (Plain ":focus" highlights used to live here - they lit up
           whatever a mouse click had just focused too. Keyboard and
           controller focus is marked by .controller-focus-ring below,
           set only while those are in use.) */
        /* The keyboard / controller focus mark, for buttons as for
           everything else in the dialogs (Export/Import,
           "Sink the config", every Sources tab action, Save/Close,
           ...) - in practice, plain ":focus" turned out NOT to be
           reliably visible on these even with the dedicated rule
           above already in place, on top of this app's own generic
           per-theme "button:not(.game-cover)" rule (see theme.py's
           _build_theme_css), which sets its own background/border
           unconditionally and, worked out on paper, has SLIGHTLY
           higher specificity than a plain "dialog button:focus" ever
           could (an extra type selector beats it even before
           source-order/load-order come into it at all) - close enough
           that relying on it was clearly the wrong call once this got
           checked properly. This rule instead is toggled EXPLICITLY,
           in Python, exactly when focus actually enters/leaves a
           widget (see _install_controller_focus_rings /
           _on_focus_ring_widget_focus_in - called on every Settings
           dialog and the simple Yes/No/OK ones), rather than relying
           on GTK's own ":focus" state-flag styling working out - and
           the class is deliberately repeated (.controller-focus-ring
           TWICE) specifically to out-specificity that same generic
           button rule for certain, regardless of source order, so a
           winning conflict here can't quietly regress again later
           just because CSS elsewhere in this app happens to get
           reordered or extended. Applies to every widget type
           uniformly (buttons, checkboxes, radios, comboboxes, entries,
           sliders) with this one rule, rather than needing a
           per-type selector the way the plain ":focus" versions above
           do. */
        .controller-focus-ring.controller-focus-ring {
            background-color: alpha(@theme_selected_bg_color, 0.10);
            background-image: none;
            border-radius: 6px;
        }
        /* Belt-and-suspenders on TOP of the belt-and-suspenders rule
           just above: even the doubled ".controller-focus-ring" class
           selector - which, worked out on paper, should already have
           beaten every competing rule in this app on specificity alone
           - still wasn't reliably showing on plain Gtk.Buttons in
           practice (Export/Import, "Sink the config", every Sources
           tab action, Save/Close, ...). "#controller-focus-ring-active"
           is a GTK widget NAME selector (set via Gtk.Widget.set_name(),
           the same thing gtk_widget_set_name() maps to on the C side)
           - in GTK CSS, same as in web CSS, an ID selector's
           specificity tier is categorically higher than any number of
           classes or type selectors combined, no exceptions, so this
           wins regardless of whatever else in this app's CSS does or
           ever will target "button".

           Debug logging (printing each widget's own
           get_style_context().to_string() right after focusing it -
           see _on_focus_ring_widget_notify, temporarily, while this
           was being tracked down) confirmed the class AND this ID
           WERE both being applied correctly to focused buttons, every
           time, with has-focus reading True - so specificity was never
           actually the remaining problem: Gtk.Button just never
           visibly reflected ANY of this rule's properties in
           practice, border/outline/box-shadow included, for a reason
           that was never conclusively pinned down. Buttons get their
           own, completely CSS-independent focus indicator instead now
           (see _prepare_button_focus_marker's "▶" marker) - nothing
           here needs to work for Gtk.Button specifically anymore, so
           there's no reason left to keep the heavier border/outline/
           box-shadow properties an earlier version of this rule had:
           just a faint background tint, which checkbutton/radiobutton/
           entry/scale all do pick this up correctly, kept deliberately
           subtle (10% opacity) rather than the much louder ring an
           earlier version of this drew, which read as overly harsh -
           particularly boxy and glaring - on a full-width radio/
           checkbox row specifically. */
        #controller-focus-ring-active {
            background-color: alpha(@theme_selected_bg_color, 0.10);
            background-image: none;
            border-radius: 6px;
        }
        .shortcut-key {
            background-color: alpha(@theme_fg_color, 0.08);
            border: 1px solid alpha(@theme_fg_color, 0.25);
            border-radius: 5px;
            padding: 2px 8px;
            min-width: 70px;
        }
        /* _open_combo_picker's floating "there's more below" hint
           (_update_combo_picker_scroll_hint toggles its visibility) -
           a small pill using this app's own theme bg/fg colors (the
           same pairing already used all over the rest of the UI)
           rather than an accent color, so it stays legibly contrasted
           against its down-chevron icon (see icons.py - recolored to
           the active theme's FOREGROUND color) regardless of which of
           this app's many palettes happens to be active. */
        .combo-picker-scroll-hint {
            background-color: alpha(@theme_bg_color, 0.85);
            border: 1px solid alpha(@theme_fg_color, 0.25);
            border-radius: 999px;
            padding: 3px 10px;
        }
        /* GTK/Adwaita draws its own themed ~1px border around a
           Gtk.Notebook's page content by default (separate from the
           tab row itself, which looks fine as-is) - in the Options
           dialog (see show_options) this showed up as a stray box
           framing the tab content, between the tab row above and the
           Save/Close buttons below. Purely cosmetic, so it's turned off
           here - scoped to .options-notebook specifically (rather than
           "notebook" generally) so this doesn't also affect the
           Dashboard's own notebook (see _build_dashboard_dialog),
           which isn't affected by this and shouldn't change. Covers
           both the "stack" node (GTK 3.20+, where the notebook's
           content pages actually live) and the notebook node itself,
           since exactly which one draws the border varies slightly by
           GTK/theme version. */
        .options-notebook,
        .options-notebook stack {
            border: none;
            box-shadow: none;
        }
        .game-cover {
            border: none;
            outline: none;
            box-shadow: none;
            border-radius: 8px;
            background-color: rgba(0, 0, 0, 0);
            background-image: none;
            transition: 0.2s;
        }
        /* .game-list-row (list-view rows specifically, alongside the
           shared .game-cover class every card/row gets - see
           _render_section) had no rule of its own at all, so every
           row's hover/selection highlight was scoped to just its
           packed content's own width rather than a consistent row
           shape - and rows with vs without a playtime badge ended up
           visibly different heights. min-height keeps every row the
           same height regardless of what it happens to be showing;
           the small extra padding gives the hover/selected background
           (see .game-cover / .game-cover-selected) a little breathing
           room around the icon and title rather than hugging them
           tightly. */
        .game-list-row {
            min-height: 40px;
            padding: 2px 6px;
        }
        /* Note: the system theme (Adwaita) normally "tints" Gtk.Image
           widgets INSIDE buttons on hover/click (-gtk-icon-effect:
           dim/highlight) - this gave a grayish/washed-out effect on the
           cover image ITSELF, regardless of what background color is set
           on the .game-cover button (since that sits BEHIND the image,
           while the image's own effect draws OVER the image). This is
           what caused the hover highlight to always look gray. This
           disables that effect, so the cover image keeps its own,
           original colors on hover too - the highlight is provided
           exclusively by the background color set in _accent_css(). */
        .game-cover image {
            -gtk-icon-effect: none;
        }
        .game-cover:hover image, .game-cover:active image, .game-cover:focus image {
            -gtk-icon-effect: none;
        }
        /* The hover/active background color is DELIBERATELY not set here -
           it's provided exclusively by the theme-specific _accent_css()
           (reloaded by App._apply_theme whenever the theme changes), so
           there's never two conflicting rules on the same node (this used
           to cause a grayish, theme-independent hover highlight). */
        /* Note: removing the outline from .game-cover (the button INSIDE
           the card) alone wasn't enough, since Gtk.FlowBox automatically
           wraps every added item in an OUTER "flowboxchild" node (see
           flow.add(banner_btn) in _render_section) - and the system theme
           (Adwaita) can give THIS its own default border/background/focus
           ring, REGARDLESS of what's set on the inner button. So even
           with the .game-cover button itself outline-free, a thin border
           could still show around the card - this rule strips the
           outline/background from this outer wrapper too. */
        flowboxchild {
            border: none;
            outline: none;
            box-shadow: none;
            background-color: transparent;
            padding: 0;
        }
        flowboxchild:selected, flowboxchild:focus, flowboxchild:hover {
            border: none;
            outline: none;
            box-shadow: none;
            background-color: transparent;
        }
        /* Selection mode: selected cards stand out with a border/
           background (see _toggle_selection / _update_selection_visuals).
           The actual color is theme-dependent, provided by _accent_css()
           (reloaded by App._apply_theme) - see its .game-cover-selected rule. */

        /* Card-like, rounded section frames instead of the old, square
           GtkFrame-bevel look - a more modern, "professional" effect. */
        /* Structural only - the palette themes override the colours (see
           theme.py's .section-frame). No border, and a translucent fill:
           the bubble field behind the content is meant to show through.
           */
        .section-frame {
            border-radius: 12px;
            border: none;
            background-color: rgba(127, 127, 127, 0.05);
        }
        /* The launcher name moved off the frame's border and into the
           header row - see RenderingMixin._render_section. */
        .section-title {
            font-weight: 600;
            font-size: 115%;
        }
        .card-title {
            font-weight: 600;
        }
        .card-playtime {
            font-size: 112%;
        }

        /* Flat header toolbar - the palette themes restate this with
           their own colours (see theme.py); this is what the "System
           default" theme gets, since it loads no palette stylesheet. */
        headerbar button,
        .titlebar button {
            background-color: transparent;
            border-color: transparent;
            box-shadow: none;
        }
        /* Restored for linked groups by a more specific rule - GTK3's
           :not() takes a simple selector only. */
        headerbar .linked button,
        .titlebar .linked button {
            border-color: rgba(127, 127, 127, 0.35);
        }
        .window-control-btn {
            background-color: transparent;
            border-color: transparent;
            box-shadow: none;
        }
        .window-close-btn:hover {
            background-color: #c0392b;
            color: #ffffff;
        }

        /* "Danger zone" frame (e.g. Delete config on the Settings -> Data
           tab) - DELIBERATELY always red, regardless of the active theme,
           since it's a universal "careful, irreversible" signal, not a
           brand-level highlight. */
        .danger-frame {
            border-radius: 12px;
            border: 1px solid rgba(224, 27, 36, 0.4);
            background-color: rgba(224, 27, 36, 0.07);
        }
        .danger-frame > label {
            font-weight: 600;
            padding: 2px 6px;
        }

        /* Section headings (e.g. "Launch behavior", "Language" across the
           various Settings tabs) - see App._settings_heading(). */
        .settings-heading {
            font-weight: 700;
            opacity: 0.85;
        }

        /* "Installed games: N" pill-shaped badge */
        .count-badge {
            border-radius: 999px;
            padding: 1px 10px;
            background-color: rgba(127, 127, 127, 0.15);
            font-size: 0.9em;
        }

        /* Header bar icon buttons: rounded, subtler hover effect */
        headerbar button {
            border-radius: 8px;
        }

        /* Launchers/All view-toggle buttons: whichever is active should
           have a clearly visible outline, so it's obvious which view
           you're on. The outline COLOR is theme-dependent - see
           _accent_css's .view-toggle-btn:checked rule. */
        .view-toggle-btn {
            border-radius: 8px;
        }

        /* Note: some system themes (e.g. certain AUR "dracula-gtk-theme"
           builds) don't draw the checkbutton/radiobutton indicator on
           GTK 3.20+'s "check"/"radio" CSS node structure - the widget
           still fully works (clickable, saves its state), it just stays
           visually invisible. This rule forces the indicator to be
           visible, regardless of whether the system theme handles this
           node correctly. The CHECKED state's color is theme-dependent -
           see _accent_css's checkbutton/radiobutton:checked rule. */
        checkbutton check, radiobutton radio {
            min-width: 14px;
            min-height: 14px;
            border: 2px solid rgba(255, 255, 255, 0.6);
            border-radius: 3px;
            background-color: rgba(255, 255, 255, 0.05);
        }
        radiobutton radio {
            border-radius: 999px;
        }
        /* Performance, for every window (the self-test checks these hold):
           Adwaita lightens an icon under the pointer - GTK3 throws the
           icon away for that and lays the WHOLE window out again, at every
           hover change of every button or row with an icon. Disabled
           icons stay dimmed. */
        image { -gtk-icon-effect: none; }
        image:disabled { -gtk-icon-effect: dim; }
        /* the same with the text and icon shadows Adwaita gives a button
           and what's in it under the pointer (a shadow wanted on a label
           is set on that label, which wins) */
        button, button * { text-shadow: none; -gtk-icon-shadow: none; }
        /* Adwaita fades "all" on buttons and rows: a fading text or icon
           colour lays the window out again on every frame of it. Only the
           surfaces fade; a rule of our own that names its transition
           still wins. */
        button, row, flowboxchild, modelbutton {
            transition-property: background-color, border-color;
        }
        /* the slider passing under the pointer while scrolling kept a
           fade going, every frame of it repainting the view */
        scrollbar, scrollbar * { transition: none; }
        """
        try:
            self._watch_css_errors(css_provider)
            css_provider.load_from_data(css.encode())
        except Exception as e:
            logger.warning(f"Failed to load structural CSS: {e}")
        # loaded again (the first run's language prompt, then __init__):
        # the old copy goes, or every style lookup matches both
        screen = Gdk.Screen.get_default()
        old = getattr(self, "_structural_css_provider", None)
        if old is not None:
            Gtk.StyleContext.remove_provider_for_screen(screen, old)
        self._structural_css_provider = css_provider
        Gtk.StyleContext.add_provider_for_screen(screen, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)


    def _load_launcher_icon(self, name: str, size: int) -> Gtk.Image:
        custom_path = None
        try:
            for f in self.icons_dir.glob(f"{name.lower()}.*"):
                custom_path = f
                break
        except OSError:
            pass
        if custom_path is not None:
            try:
                return Gtk.Image.new_from_pixbuf(pixbuf_at_size(custom_path, size, size))
            except Exception as e:
                logger.debug(f"Couldn't load custom cover image {custom_path!r}: {e}")
        self._maybe_restore_icon(name.lower())
        # The logo bundled with PlunderHub - there whether or not the
        # launcher is installed.
        bundled = bundled_launcher_icon(name)
        if bundled is not None:
            try:
                return Gtk.Image.new_from_pixbuf(pixbuf_at_size(bundled, size, size))
            except GLib.Error as e:
                logger.debug(f"Couldn't load the bundled icon {bundled}: {e}")
        # Otherwise the launcher's own icon, as its package installed it -
        # Bottles' is com.usebottles.bottles, not "bottles", and a
        # Flatpak's is always its app id.
        icon_theme = Gtk.IconTheme.get_default()
        for candidate in [name.lower()] + LAUNCHER_ICON_NAMES.get(name, []):
            try:
                if not icon_theme.has_icon(candidate):
                    continue
                pixbuf = theme_icon_pixbuf(icon_theme, candidate, size, Gtk.IconLookupFlags.FORCE_SIZE)
                if pixbuf:
                    return Gtk.Image.new_from_pixbuf(pixbuf)
            except Exception as e:
                logger.debug(f"Icon theme lookup for {candidate!r} failed: {e}")
        return self._icon_image("system-run", Gtk.IconSize.DIALOG)

    def _show_launcher_context_menu(self, widget, event, launcher_name: str):
        if event.button != 3:
            return
        self._launcher_menu_at(widget, launcher_name, event)

    def _launcher_menu_at(self, widget: Gtk.Widget, launcher_name: str, event=None):
        """The launcher's menu, pointing where it was clicked - or at the
        widget itself (its ⋯ button)."""
        from plunderhub.ui.game_page import LauncherMenu
        menu = LauncherMenu(self, launcher_name, widget)
        if event is not None:
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(event.x), int(event.y), 1, 1
            menu.set_pointing_to(rect)
        menu.popup()
        return menu

    def has_custom_launcher_icon(self, launcher_name: str) -> bool:
        try:
            return any(self.icons_dir.glob(f"{launcher_name.lower()}.*"))
        except OSError:
            return False

    def reset_launcher_icon(self, launcher_name: str):
        """Back to the logo PlunderHub brings (or the launcher's own)."""
        key = launcher_name.lower()
        for old in self.icons_dir.glob(f"{key}.*"):
            old.unlink(missing_ok=True)
        self.icon_map.pop(key, None)
        save_icon_map(self.icon_map)
        self._icons_restore_failed.discard(key)
        self._render_launchers()

    def apply_launcher_sgdb_icon(self, launcher_name: str, item: Dict, preview: Path):
        """What the SteamGridDB picker chose: its preview at once, the
        full file once it's downloaded."""
        self._on_icon_selected({"file_path": preview, "url": item.get("url", ""),
                                "animated": item.get("animated", False)}, launcher_name, None)

    def _set_launcher_icon_from_file(self, launcher_name: str):
        from plunderhub.ui.image_picker import pick_image_file
        src = pick_image_file(self, "icon", launcher_name)
        if not src:
            return
        ext = os.path.splitext(src)[1]
        target = self.icons_dir / f"{launcher_name.lower()}{ext}"
        # copied beside first: a failing copy (a dropped mount, the icon
        # file itself picked again) keeps the icon there was
        staged = self.icons_dir / f".{launcher_name.lower()}{ext}.plunderhub-tmp"
        try:
            self.icons_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, staged)
            for old in self.icons_dir.glob(f"{launcher_name.lower()}.*"):
                if old != target:
                    old.unlink(missing_ok=True)
            os.replace(staged, target)
        except OSError as e:
            staged.unlink(missing_ok=True)
            logger.warning(f"Couldn't use {src} as the {launcher_name} icon: {e}")
            self._show_info_dialog(self._("icon_copy_failed").format(error=e), message_type=Gtk.MessageType.ERROR)
            return
        self.icon_map[launcher_name.lower()] = {"custom": True}
        save_icon_map(self.icon_map)
        self._icons_restore_failed.discard(launcher_name.lower())
        self.reload()


    def _remember_icon_source(self, launcher_key: str, url: str, ext: str, animated: bool = False):
        self.icon_map[launcher_key] = {
            "url": url,
            "ext": ext,
            "animated": bool(animated),
        }
        save_icon_map(self.icon_map)
        self._icons_restore_failed.discard(launcher_key)


    def _maybe_restore_icon(self, launcher_key: str):
        if launcher_key in self._icons_restoring or launcher_key in self._icons_restore_failed:
            return
        entry = self.icon_map.get(launcher_key)
        if not entry or not entry.get("url"):
            return
        self._icons_restoring.add(launcher_key)
        thread = threading.Thread(target=self._restore_icon_thread, args=(launcher_key, entry), daemon=True)
        thread.start()

    def _restore_icon_thread(self, launcher_key: str, entry: Dict):
        url = entry.get("url", "")
        ext = entry.get("ext") or os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".png"
        target = self.icons_dir / f"{launcher_key}{ext}"
        ok = False
        try:
            ok = self.download_cover_from_url(url, target)
        except Exception as e:
            logger.warning(f"Automatic restore of launcher icon failed ({launcher_key}): {e}")
        GLib.idle_add(self._finish_restore_icon, launcher_key, ok)

    def _finish_restore_icon(self, launcher_key: str, ok: bool = True):
        self._icons_restoring.discard(launcher_key)
        if not ok:
            self._icons_restore_failed.add(launcher_key)
            return
        self._render_launchers()


    def _stop_animation(self, uid: str):
        self._anim_player.stop(uid)

    def _stop_all_animations(self):
        self._anim_player.stop_all()

    def _reveal_card(self, widget: Gtk.Widget, delay_ms: int, epoch: int):
        widget.set_opacity(0.0)
        steps = max(1, REVEAL_FADE_DURATION_MS // REVEAL_FADE_STEP_MS)
        state = {"step": 0}

        def tick():
            if epoch != self._reveal_epoch:
                return False
            state["step"] += 1
            widget.set_opacity(min(1.0, state["step"] / steps))
            return state["step"] < steps

        def start_fade():
            if epoch != self._reveal_epoch:
                return False
            GLib.timeout_add(REVEAL_FADE_STEP_MS, tick)
            return False

        if delay_ms <= 0:
            start_fade()
        else:
            GLib.timeout_add(delay_ms, start_fade)

    def _widget_on_screen(self, widget: Gtk.Widget) -> bool:
        """Whether any of widget shows in its scrolled view now."""
        try:
            if not widget.get_mapped():
                return False
            scroller = widget.get_ancestor(Gtk.ScrolledWindow)
            adjustment = scroller.get_vadjustment() if scroller is not None else None
            if adjustment is None:
                return True
            top = adjustment.get_value()
            bottom = top + adjustment.get_page_size()
            if bottom <= top:
                return True
            allocation = widget.get_allocation()
            return allocation.y + allocation.height > top and allocation.y < bottom
        except Exception:
            return True

    def _cover_is_on_screen(self, uid: str) -> bool:
        widgets = self.cover_widgets.get(uid)
        if not widgets:
            return False
        if getattr(self, "startup_overlay", None) is not None:
            return True
        for widget in widgets:
            try:
                if not widget.get_mapped():
                    continue
                scroller = widget.get_ancestor(Gtk.ScrolledWindow)
                if scroller is None:
                    return True
                adjustment = scroller.get_vadjustment()
                if adjustment is None:
                    return True
                top = adjustment.get_value()
                bottom = top + adjustment.get_page_size()
                if bottom <= top:
                    return True
                allocation = widget.get_allocation()
                if allocation.y + allocation.height > top and allocation.y < bottom:
                    return True
            except Exception:
                return True
        return False

    _ERROR_HEX = "#e5534b"

    def _summary_stat(self, value: int, caption: str, tone_class: str) -> Gtk.ToggleButton:
        card = Gtk.ToggleButton()
        card.get_style_context().add_class("summary-stat")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        number = Gtk.Label(label=str(value))
        number.get_style_context().add_class("summary-stat-value")
        number.get_style_context().add_class(tone_class)
        inner.pack_start(number, False, False, 0)
        label = Gtk.Label(label=caption)
        label.get_style_context().add_class("summary-stat-label")
        inner.pack_start(label, False, False, 0)
        card.add(inner)
        return card

    def _summary_list(self, rows: List[Tuple[str, str]], row_class: str) -> Gtk.ScrolledWindow:
        """Titles, each with its reason (if any) in smaller text under it."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(12)
        for title_text, reason in rows:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            if title_text:
                name = Gtk.Label(label=title_text, xalign=0)
                name.set_line_wrap(True)
                name.get_style_context().add_class(row_class)
                row.pack_start(name, False, False, 0)
            if reason:
                why = Gtk.Label(label=reason, xalign=0)
                why.set_line_wrap(True)
                why.set_selectable(True)
                why.get_style_context().add_class("summary-row-reason")
                row.pack_start(why, False, False, 0)
            box.pack_start(row, False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # Wrapped labels report their height only once they have a width,
        # too late for the scroller to size itself - so it gets an
        # estimate: about a line for a title, two for a reason.
        estimate = 24 + sum(26 + (36 if reason else 0) for _, reason in rows)
        scroller.set_min_content_height(min(240, estimate))
        scroller.set_max_content_height(240)
        scroller.set_propagate_natural_height(True)
        scroller.get_style_context().add_class("summary-list")
        scroller.add(box)
        return scroller

    def _dialog_transient_parent(self) -> Gtk.Window:
        if getattr(self, "_options_dialog", None) is not None and self._options_dialog.get_visible():
            return self._options_dialog
        return self

    def _center_message_dialog_text(self, dialog: Gtk.MessageDialog, icon_name: Optional[str] = None):
        area = dialog.get_message_area()
        if area is None:
            return
        area.set_halign(Gtk.Align.CENTER)

        def center(widget: Gtk.Widget):
            if isinstance(widget, Gtk.Label):
                widget.set_justify(Gtk.Justification.CENTER)
                widget.set_xalign(0.5)
                widget.set_halign(Gtk.Align.CENTER)
            elif isinstance(widget, Gtk.Box):
                widget.set_halign(Gtk.Align.CENTER)
            if hasattr(widget, "get_children"):
                for child in widget.get_children():
                    center(child)

        center(area)

        # GtkMessageDialog's own built-in icon (its message-type
        # default, or whatever set_image() set) lives in a SEPARATE
        # sibling Box next to the message area at a fixed width, not
        # inside it - since the message area itself is packed with
        # expand=True to fill whatever space is left over, its own
        # centered content ends up centered WITHIN that leftover
        # space rather than the dialog as a whole, so the text
        # visibly drifts right by roughly half the icon's width
        # (reported directly, with a screenshot; confirmed live that
        # the drift gets worse the wider the dialog - a one-line
        # message barely showed it, a two-line one made it obvious).
        # Every CSS and set_child_packing approach tried against that
        # outer Box was confirmed, live, to have no effect at all -
        # GtkMessageDialog re-asserts its own internal packing. The
        # reliable fix is to remove the icon from that outer Box
        # entirely and place our own copy inside the message area
        # instead, stacked above the text - once both live in the
        # same, already-centered container, the whole thing reads as
        # genuinely centered no matter how wide the dialog gets.
        default_image = dialog.get_image()
        if default_image is not None:
            default_image.set_no_show_all(True)
            default_image.hide()
        if icon_name is not None:
            icon = self._icon_image_at_size(icon_name, 48)
            icon.set_halign(Gtk.Align.CENTER)
            icon.set_margin_bottom(6)
            area.pack_start(icon, False, False, 0)
            area.reorder_child(icon, 0)
            icon.show()

    def _dialog_tone(self, message_type, icon_name: Optional[str]) -> str:
        if icon_name in ("emblem-ok-symbolic", "circle-check-symbolic"):
            return "success"
        return {Gtk.MessageType.ERROR: "error", Gtk.MessageType.WARNING: "warning",
                Gtk.MessageType.QUESTION: "question"}.get(message_type, "info")

    def _dialog_heading_and_text(self, text: str, heading: Optional[str],
                                 fallback_key: str) -> Tuple[str, Optional[str]]:
        """A short message is its own heading; a longer one gets
        fallback_key's heading above it."""
        if heading:
            return heading, text
        if len(text) <= 70 and "\n" not in text:
            return text, None
        return self._(fallback_key), text

    def _show_info_dialog(self, text: str, message_type=Gtk.MessageType.INFO,
                           icon_name: Optional[str] = None, heading: Optional[str] = None,
                           transient_for: Optional[Gtk.Window] = None):
        tone = self._dialog_tone(message_type, icon_name)
        fallback = {"error": "dialog_heading_error", "warning": "dialog_heading_warning"}.get(
            tone, "dialog_heading_info")
        title, body = self._dialog_heading_and_text(text, heading, fallback)
        # The tone's own icon: the old per-call icons were picked for the
        # old dialog's plain glyph.
        self._card_dialog(title, body, [(self._("dialog_ok"), Gtk.ResponseType.OK, "accent")],
                          tone=tone, transient_for=transient_for)

    def _confirm_yes_no(self, text: str, *, message_type=Gtk.MessageType.QUESTION,
                         transient_for: Optional[Gtk.Window] = None,
                         title: Optional[str] = None,
                         default_response=Gtk.ResponseType.NO,
                         icon_name: Optional[str] = None,
                         controller_hints: bool = False,
                         heading: Optional[str] = None,
                         yes_label: Optional[str] = None,
                         no_label: Optional[str] = None,
                         destructive: bool = False) -> bool:
        """True for yes. The generic "Confirm" title isn't a heading
        worth showing; any other title is."""
        if heading is None and title and title != self._("confirm_generic_title"):
            heading = title
        heading, body = self._dialog_heading_and_text(text, heading, "dialog_heading_confirm")
        tone = "danger" if destructive else self._dialog_tone(message_type, icon_name)
        response = self._card_dialog(
            heading, body,
            [(no_label or self._("dialog_no"), Gtk.ResponseType.NO, "ghost"),
             (yes_label or self._("dialog_yes"), Gtk.ResponseType.YES, "danger" if destructive else "accent")],
            tone=tone, default=default_response, transient_for=transient_for)
        return response == Gtk.ResponseType.YES

    def _add_button_controller_hint(self, button: Gtk.Button, role: str, glyph_first: bool):
        text = button.get_label()
        if text is None:
            return
        old_child = button.get_child()
        if old_child is not None:
            button.remove(old_child)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(Gtk.Align.CENTER)
        label = Gtk.Label(label=text)
        label.set_use_underline(True)
        glyph = self._console_controller_glyph(self._controller_glyph_text(role), diameter=22)
        if glyph_first:
            box.pack_start(glyph, False, False, 0)
            box.pack_start(label, False, False, 0)
        else:
            box.pack_start(label, False, False, 0)
            box.pack_start(glyph, False, False, 0)
        button.add(box)
        button.show_all()

    def _ask_workspace_choice(self) -> bool:
        """The first launch: the "Open games on the next workspace" switch
        on a card of its own, with Save. False when it's closed unsaved -
        the game doesn't start, and the question comes again next time."""
        _ = self._
        dlg, column = card_dialog(self._dialog_transient_parent(), _("ws_ask_title"))
        top = Gtk.Box(spacing=12)
        title = Gtk.Label(label=_("ws_ask_title"), xalign=0)
        title.set_line_wrap(True)
        title.get_style_context().add_class("home-dialog-title")
        top.pack_start(title, True, True, 0)
        close = Gtk.Button()
        close.get_style_context().add_class("about-close")
        cross = get_icon_pixbuf("window-close-symbolic", 16, self._widget_fg_hex(self))
        close.add(Gtk.Image.new_from_pixbuf(cross) if cross is not None else Gtk.Label(label="×"))
        close.set_valign(Gtk.Align.START)
        close.set_focus_on_click(False)
        close.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.CANCEL))
        top.pack_end(close, False, False, 0)
        column.pack_start(top, False, False, 0)

        row = Gtk.Box(spacing=14)
        row.get_style_context().add_class("settings-row")
        row.get_style_context().add_class("welcome-feature")
        row.get_style_context().add_class("ws-ask-row")      # the switch as in Settings
        row.set_margin_top(14)
        row.pack_start(self._icon_image("go-next-symbolic", Gtk.IconSize.LARGE_TOOLBAR), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.set_valign(Gtk.Align.CENTER)
        name = Gtk.Label(label=_("next_workspace_check"), xalign=0)
        name.set_line_wrap(True)
        name.get_style_context().add_class("settings-row-title")
        sub = Gtk.Label(label=_("next_workspace_sub").format(wm=self.workspace_mover().compositor.name), xalign=0)
        sub.set_line_wrap(True)
        sub.set_max_width_chars(48)
        sub.get_style_context().add_class("settings-row-sub")
        texts.pack_start(name, False, False, 0)
        texts.pack_start(sub, False, False, 0)
        row.pack_start(texts, True, True, 0)
        switch = Gtk.Switch()
        switch.set_active(self.launch_on_next_workspace)
        switch.set_valign(Gtk.Align.CENTER)
        row.pack_end(switch, False, False, 0)
        column.pack_start(row, False, False, 0)

        hint = Gtk.Label(label=_("ws_ask_hint").format(settings=_("options")), xalign=0)
        hint.set_line_wrap(True)
        hint.set_max_width_chars(56)
        hint.get_style_context().add_class("dim-label")
        hint.set_margin_top(8)
        column.pack_start(hint, False, False, 0)

        buttons = Gtk.Box()
        buttons.set_margin_top(16)
        save = self._dialog_button(_("save_btn"), "accent", "activate", "Enter")
        save.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.OK))
        buttons.pack_end(save, False, False, 0)
        column.pack_start(buttons, False, False, 0)

        def on_key(_w, event) -> bool:
            if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                dlg.response(Gtk.ResponseType.OK)
                return True
            return False
        dlg.connect("key-press-event", on_key)
        dlg.show_all()
        answer = dlg.run()
        chosen = switch.get_active()
        dlg.destroy()
        if answer != Gtk.ResponseType.OK:
            return False
        self.launch_on_next_workspace = chosen
        self.settings["launch_on_next_workspace"] = chosen
        self.settings["workspace_choice_asked"] = True
        save_settings(self.settings)
        if getattr(self, "next_workspace_check", None) is not None:
            self.next_workspace_check.set_active(chosen)
        return True

    def request_launch(self, cmd: List[str], label: str = "", env: Optional[Dict[str, str]] = None,
                        uid: Optional[str] = None, install_dir: Optional[str] = None):
        if self._saves_busy(uid):
            return      # its saves are being synced
        if uid in self._proton_waits:
            return      # its Proton is being downloaded
        if self.launch_mode == "confirm" and not self._confirm_launch(label, uid):
            return
        self.launch(cmd, label, env, uid, install_dir)

    def launch(self, cmd: List[str], label: str = "", env: Optional[Dict[str, str]] = None,
               uid: Optional[str] = None, install_dir: Optional[str] = None):
        if uid and uid in self.detected_running:
            return      # already running, started from its own launcher
        if uid and (uid in self.running_procs or uid in self.launching_uids):
            return      # started from here and still running / starting
        if self._saves_busy(uid):
            return      # its saves are being synced
        if uid in self._proton_waits:
            return      # its Proton is being downloaded
        if uid and uid.startswith("steam:"):
            # Right before Steam starts, so a closed Steam picks it up.
            self._apply_steam_proton_default([uid.split(":", 1)[1]])
        # The first game launched: whether games open on the next workspace,
        # asked once (where moving them is possible at all).
        if uid and "workspace_choice_asked" not in self.settings and self.workspace_mover() is not None:
            if not self._ask_workspace_choice():
                return
        # Where PlunderHub is now, before the launch hides it.
        mover = self.workspace_mover() if uid else None
        workspace = mover.target_for_launch() if mover is not None and self.launch_on_next_workspace else None
        heroic = self._start_heroic_launch(uid, cmd, env, label) if uid and uid.startswith("heroic:") else None
        try:
            # A Heroic launch may start Heroic alone first, or wait for
            # its update without starting anything yet.
            run_cmd = cmd if heroic is None else heroic[1]
            proc = run(run_cmd, env) if run_cmd else None
        except Exception as exc:
            md = Gtk.MessageDialog(transient_for=self._dialog_transient_parent(), flags=0,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.NONE,
                                   text=self._("error_launch"))
            md.add_button(self._("dialog_ok"), Gtk.ResponseType.OK)
            md.format_secondary_text(f"{label}\n\n{exc}")
            self._center_message_dialog_text(md, icon_name="circle-alert-symbolic")
            self._install_controller_focus_rings(md)
            self._enable_keyboard_focus_ring_detection(md)
            md.run()
            md.destroy()
            return
        if uid:
            self.recent[uid] = time.time()
            try:
                save_recent(self.recent)
            except OSError as e:        # a full disk mustn't lose the game that just started
                logger.warning(f"Couldn't save the recent list: {e}")
            if proc is not None:
                self.running_procs[uid] = proc
            if heroic is not None:
                self._heroic_launches[uid] = heroic[0]
            self.launching_uids[uid] = time.time()
            self._launch_cmds[uid] = (cmd, env)
            self._steam_asked_again.discard(uid)
            self._launch_settled_at.pop(uid, None)
            if mover is not None and uid.startswith("steam:"):
                # Steam was started -silent, but brings its window up
                # itself at some steps (shaders) - back to the tray
                mover.keep_steam_in_tray(uid, lambda u=uid: self._steam_window_unwanted(u))
            self._update_decorative_animation_pause_state()
            self._directory_confirmed_running.discard(uid)
            process_match = self._game_process_match(uid, install_dir, label)
            if process_match is not None:
                self._running_process_match[uid] = process_match
            else:
                self._running_process_match.pop(uid, None)
            if workspace is not None:
                mover.watch(uid, process_match or GameProcessMatch(directory=install_dir), workspace)
            self._render_launchers()
        if self.console_mode_active:
            self._console_hidden_for_game = True
            return
        if self.auto_close:
            self._close_to_tray_or_quit()
        elif self.minimize_on_launch:
            self.iconify()

    def _start_heroic_launch(self, uid: str, cmd: List[str], env, label: str
                             ) -> Tuple[HeroicLaunch, Optional[List[str]]]:
        """The launch's follower, and what to run now: Heroic alone when
        it's closed and installs updates (the game follows its update
        check), nothing while a running Heroic is updating the game,
        else the launch itself."""
        cold = not heroic_running()
        wait = updates_before_launch(uid) and bool(cmd) and cmd[-1].startswith("heroic://")
        follower = HeroicLaunch(uid, label, cold and wait, time.time())
        follower.cmd, follower.env = cmd, env
        if follower.started_heroic:
            return follower, cmd[:-1]
        if wait and not follower.poll(time.time()).ready:
            return follower, None
        follower.handed_over, follower.handed_at = True, time.time()
        return follower, cmd

    def _follow_heroic_launches(self) -> List[str]:
        """Hands each waiting Heroic launch over once Heroic is ready;
        returns the ones Heroic failed to start."""
        failed = []
        now = time.time()
        for uid, follower in list(self._heroic_launches.items()):
            stage = follower.poll(now)
            self._heroic_stages[uid] = stage
            if stage.ready and not follower.handed_over:
                follower.handed_over, follower.handed_at = True, now
                try:
                    proc = run(follower.cmd, follower.env)
                except Exception as e:
                    logger.warning(f"Couldn't hand {uid}'s launch to Heroic: {e}")
                    failed.append(uid)
                    continue
                self.running_procs.setdefault(uid, proc)
            elif stage.stage == "failed":
                failed.append(uid)
        return failed

    def _end_heroic_launch(self, uid: str):
        self._heroic_launches.pop(uid, None)
        self._heroic_stages.pop(uid, None)

    def _release_card_after_dialog(self, source: Optional[Gtk.Widget]):
        if source is None:
            return
        try:
            source.unset_state_flags(Gtk.StateFlags.PRELIGHT | Gtk.StateFlags.ACTIVE)
        except Exception as e:
            logger.debug(f"Couldn't clear card state after a dialog: {e}")
        if self._keyboard_nav_active:
            return
        GLib.idle_add(self._drop_focus_from_card, source)

    def _drop_focus_from_card(self, source: Gtk.Widget) -> bool:
        try:
            if self._keyboard_nav_active:
                return False
            focused = self.get_focus()
            if focused is source or (focused is not None
                                     and focused.is_ancestor(source)):
                self.set_focus(None)
                source.unset_state_flags(Gtk.StateFlags.PRELIGHT | Gtk.StateFlags.ACTIVE)
        except Exception as e:
            logger.debug(f"Couldn't drop focus from a card: {e}")
        return False

    def request_launch_launcher(self, cmd: List[str], launcher_name: str,
                                source: Optional[Gtk.Widget] = None):
        if self.launch_mode == "confirm":
            confirmed = self._confirm_launch(launcher_name, None, launcher=True)
            self._release_card_after_dialog(source)
            if not confirmed:
                return
        self._launch_launcher_process(cmd, launcher_name)

    def _launch_launcher_process(self, cmd: List[str], launcher_name: str):
        try:
            proc = run(cmd, None)
        except Exception as exc:
            md = Gtk.MessageDialog(transient_for=self._dialog_transient_parent(), flags=0,
                                   message_type=Gtk.MessageType.ERROR,
                                   buttons=Gtk.ButtonsType.NONE,
                                   text=self._("error_launch"))
            md.add_button(self._("dialog_ok"), Gtk.ResponseType.OK)
            md.format_secondary_text(f"{launcher_name}\n\n{exc}")
            self._center_message_dialog_text(md, icon_name="circle-alert-symbolic")
            self._install_controller_focus_rings(md)
            self._enable_keyboard_focus_ring_detection(md)
            md.run()
            md.destroy()
            return
        self.running_launcher_procs[launcher_name] = proc
        self._render_launchers()
        if self.console_mode_active:
            self._console_hidden_for_game = True
            return
        if self.auto_close:
            self._close_to_tray_or_quit()
        elif self.minimize_on_launch:
            self.iconify()

    def _launcher_is_running(self, name: str) -> bool:
        return name in self.running_launcher_procs or name in self._launchers_running

    def _on_close_launcher_clicked(self, name: str):
        """Close on a running launcher's sidebar row: the same way out as
        on Launch options - and one PlunderHub started has its process
        group ended if asking didn't."""
        if launcher_pids(name):
            if not self.close_launcher_asking(name, self):
                return
        proc = self.running_launcher_procs.pop(name, None)
        if proc is not None and (proc.poll() is None or self._process_group_alive(proc)):
            self._terminate_process(proc)
        self._launchers_running.discard(name)
        self._render_launchers()

    _LAUNCHER_CHECK_SECONDS = 2.0

    def _on_launchers_checked(self, running) -> bool:
        self._launchers_checking = False
        if running is not None and running != self._launchers_running:
            closed = self._launchers_running - running
            self._launchers_running = running
            self._refresh_home_sidebar()
            if closed and getattr(self, "_proton_upkeep", None) is not None:
                # what waited for it to close (Proton-GE Latest as its
                # default, games moved off a removed build) is done now,
                # not at the next scan
                self._proton_upkeep.after_scan()
        return False

    def _poll_running_launchers(self) -> bool:
        now = time.monotonic()
        if now - self._launchers_checked_at >= self._LAUNCHER_CHECK_SECONDS:
            # every launcher in the sidebar, started from it or not
            self._launchers_checked_at = now
            names = {s["name"] for s in getattr(self, "_cached_launchers", None) or []}
            if not getattr(self, "_launchers_checking", False):
                # a walk over /proc per launcher: off the GTK thread
                self._launchers_checking = True

                def check(names=names):
                    try:
                        running = {name for name in names if launcher_pids(name)}
                    except Exception as e:
                        logger.debug(f"Launcher check failed: {e}")
                        running = None
                    GLib.idle_add(self._on_launchers_checked, running)
                threading.Thread(target=check, name="plunderhub-launcher-check", daemon=True).start()
        if not self.running_launcher_procs:
            return True
        finished = [
            name for name, proc in self.running_launcher_procs.items()
            if proc.poll() is not None and not self._process_group_alive(proc)
        ]
        if finished:
            for name in finished:
                self.running_launcher_procs.pop(name, None)
            self._render_launchers()
        return True

    def _process_group_alive(self, proc: subprocess.Popen) -> bool:
        try:
            os.killpg(proc.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def _unconfirmed_run_timed_out(self, uid: str, proc: subprocess.Popen) -> bool:
        if uid in self._directory_confirmed_running:
            return False
        started_at = self.running_since.get(uid)
        if started_at is None:
            return False
        if time.monotonic() - started_at < self._UNCONFIRMED_RUNNING_TIMEOUT_SECONDS:
            return False
        if proc.poll() is None or self._process_group_alive(proc):
            return False
        logger.info(
            f"Giving up on the running-state of {uid}: nothing confirmed it actually "
            f"running within {int(self._UNCONFIRMED_RUNNING_TIMEOUT_SECONDS)}s and the "
            f"launched process group has exited - clearing its running badge."
        )
        return True

    def _game_process_match(self, uid: Optional[str], install_dir: Optional[str],
                            title: str = "") -> Optional[GameProcessMatch]:
        """How to recognise this game's own processes - see GameProcessMatch.
        Only Steam and Bottles games need it: their launch command hands
        off to an already-running launcher, so the process PlunderHub
        spawned says nothing about whether the game itself still runs."""
        if not uid:
            return None
        if uid.startswith("steam:"):
            return GameProcessMatch(directory=install_dir, steam_appid=uid.split(":", 1)[1])
        if uid.startswith("bottles:") and install_dir:
            exe_names = bottles_program_executables(install_dir, uid.split(":", 1)[1], title)
            return GameProcessMatch(wineprefix=install_dir, exe_names=exe_names)
        return None

    def _sync_detected_games(self) -> None:
        """Folds the background detector's findings into the running state."""
        now = time.monotonic()
        snapshot = self._running_detector.snapshot()
        self._detection_ignored_until = {u: t for u, t in self._detection_ignored_until.items() if t > now}
        started = [u for u in snapshot
                   if u not in self.running_procs and u not in self.detected_running
                   and u not in self._detection_ignored_until]
        ended = [u for u in self.detected_running if u not in snapshot or u in self.running_procs]
        for uid in started:
            detection = snapshot[uid]
            self.detected_running[uid] = detection
            # Its real start, so the timer is right even for a game that
            # was already running before PlunderHub was.
            self.running_since[uid] = now - detection.age_seconds
        for uid in ended:
            self.detected_running.pop(uid, None)
            self._end_detected_session(uid)
        for uid in self.detected_running:
            if uid in snapshot:
                self.detected_running[uid] = snapshot[uid]
        if started or ended:
            self._render_launchers()
            self._update_decorative_animation_pause_state()
            if ended:
                # The launchers record their own playtime - resync it.
                self.reload()

    def _end_detected_session(self, uid: str) -> None:
        started_at = self.running_since.pop(uid, None)
        # Only Bottles keeps no playtime of its own; the rest are synced
        # from the launcher, so crediting here would count them twice.
        if started_at is not None and uid.startswith("bottles:"):
            self._credit_session_playtime(uid, started_at)

    # Steam launch steps that can take minutes, and do say so in its logs
    _STEAM_SLOW_STAGES = ("steam_updating", "updating", "shaders_download", "shaders", "setup", "cloud",
                          "waiting", "stalled")
    # how long after the game started Steam's window is still put back
    _STEAM_TRAY_AFTER_START_SECONDS = 15.0

    def _steam_window_unwanted(self, uid: str) -> Optional[bool]:
        """For keep_steam_in_tray: True while a new Steam window would
        only be in the way, False while Steam asks the player something
        (that window is left alone), None once the launch is over."""
        if uid in self.launching_uids:
            stage = self._process_probe.stage(uid)
            return not (stage is not None and stage.stage == "waiting")
        settled = self._launch_settled_at.get(uid)
        if settled is not None and time.time() - settled < self._STEAM_TRAY_AFTER_START_SECONDS:
            return True
        return None

    def _ask_stalled_steam_again(self):
        """Steam sometimes finishes a download the launch waited for and
        then never starts the game - it's asked once more."""
        for uid in list(self.launching_uids):
            if not uid.startswith("steam:") or uid in self._steam_asked_again:
                continue
            stage = self._process_probe.stage(uid)
            if stage is None or stage.stage != "stalled" or time.time() - stage.since < STALL_SECONDS:
                continue
            self._steam_asked_again.add(uid)
            cmd, env = self._launch_cmds.get(uid, ([], None))
            if not cmd:
                continue
            logger.info(f"Steam finished {uid}'s download but didn't start it - asking again")
            try:
                run(cmd, env)
            except Exception as e:
                logger.warning(f"Couldn't ask Steam again to launch {uid}: {e}")
    _STEAM_QUIET_LAUNCH_SECONDS = 90.0

    # Heroic steps that take a while, and say when they're done
    _HEROIC_SLOW_STAGES = ("updating", "cloud", "redist", "prefix", "winetricks")
    _HEROIC_QUIET_LAUNCH_SECONDS = 90.0

    def _launch_settled(self, uid: str, started_at: float) -> bool:
        """Whether a launch is over, and the game's time starts. A Steam
        launch waits for Steam to say the game runs - an update or the
        shaders can take minutes - unless Steam goes quiet about it; a
        Heroic one for Heroic to start the game itself."""
        follower = self._heroic_launches.get(uid)
        if follower is not None:
            heroic_stage = self._heroic_stages.get(uid)
            if not follower.handed_over or heroic_stage is None:
                return False
            if heroic_stage.stage == "launched":
                return True
            if heroic_stage.stage in self._HEROIC_SLOW_STAGES:
                return False
            return time.time() - follower.handed_at >= self._HEROIC_QUIET_LAUNCH_SECONDS
        stage = self._process_probe.stage(uid) if uid.startswith("steam:") else None
        if stage is None:
            return time.time() - started_at >= self._LAUNCHING_GRACE_SECONDS
        if stage.stage == "running":
            return True
        if stage.stage in self._STEAM_SLOW_STAGES:
            return False
        return time.time() - stage.since >= self._STEAM_QUIET_LAUNCH_SECONDS

    def launch_status(self, uid: str) -> Optional[str]:
        """What Steam or Heroic is doing to launch the game (or Heroic
        with its cloud saves), for under Setting sail; None when there's
        nothing to say."""
        if uid in self._proton_waits:
            from plunderhub.ui.proton_upkeep import upkeep
            return upkeep(self).wait_text(uid)
        sync = self._cloud_syncs.get(uid)
        if sync is not None:
            done = sync.startswith("done:")
            mode = sync.split(":", 1)[1] if done else sync
            if mode in ("backup", "restore"):
                return self._(f"saves_stage_{mode}" + ("_done" if done else ""))
            return self._("cloud_stage_done" if done else
                          "cloud_stage_upload" if mode.endswith("upload") else "cloud_stage_download")
        if uid not in self.launching_uids:
            return None
        heroic_stage = self._heroic_stages.get(uid)
        if uid in self._heroic_launches:
            if heroic_stage is None or heroic_stage.stage in ("launched", "failed"):
                return None
            text = self._(f"heroic_stage_{heroic_stage.stage}")
            if not heroic_stage.progress:
                return text
            parts = [heroic_stage.progress]
            if heroic_stage.downloaded:
                parts.append(re.sub(r"(\d)([KMGT]?i?B)$", r"\1 \2", heroic_stage.downloaded))
            if heroic_stage.speed:
                parts.append(f"{heroic_stage.speed} MB/s")
            eta = heroic_stage.eta
            if eta.count(":") == 2 and eta.startswith("00:"):
                eta = eta[3:]       # 00:01:23 -> 01:23
            if eta.strip("0:"):
                parts.append(self._("download_time_left").format(time=eta))
            return f"{text} " + "  ·  ".join(parts)
        stage = self._process_probe.stage(uid)
        if stage is None or stage.stage in ("running", "failed"):
            return None
        text = self._(f"steam_stage_{stage.stage}")
        if stage.total > 0:
            percent = int(100 * stage.done / stage.total)
            text += f" {percent}%  ·  {format_bytes(stage.done)} / {format_bytes(stage.total)}"
        return text

    def _show_launch_failed(self, uid: str, error: str) -> bool:
        game = self.game_index.get(uid)
        title = game.title if game is not None else uid
        which = "heroic" if uid.startswith("heroic:") else "steam"
        text = self._(f"{which}_launch_failed_text")
        if error:
            text += f"\n\n{error}"
        self._show_info_dialog(text, Gtk.MessageType.ERROR,
                               heading=self._(f"{which}_launch_failed").format(title=title))
        return False

    def start_cloud_sync(self, game: GameItem, mode: str):
        """Heroic's Manual Sync Saves for the game (see heroic_cloud),
        said on its cards while it runs."""
        choice = self._pick_save_choice(game, "sync")
        if choice is None or choice.cloud is None:
            return
        saves = choice.cloud
        if mode.startswith("force_") and not self._confirm_yes_no(
                self._(f"cloud_{mode}_confirm"), title=self._(f"cloud_{mode}"), destructive=True):
            return
        self._run_saves_job(game, mode, lambda: run_cloud_sync(saves, mode))

    def back_up_game_saves(self, game: GameItem):
        """A zip of the game's save folders (see save_backup) - what
        Heroic asks for before a cloud sync."""
        choice = self._pick_save_choice(game, "backup")
        if choice is None or not self._steam_closed_for_saves(game, "backup"):
            return
        made: List[Path] = []

        def job():
            made.append(back_up_saves(game.uid, game.title, choice.locations, variant=choice.variant))
            return True, ""

        def done():
            # where it went, so the backups can be found
            target = made[0]
            response = self._card_dialog(
                self._("saves_backed_up_title"),
                self._("saves_backed_up_text").format(path=self._home_path(target)),
                [(self._("dialog_ok"), Gtk.ResponseType.OK, "ghost"),
                 (self._("saves_open_backups"), Gtk.ResponseType.APPLY, "accent")],
                tone="success", default=Gtk.ResponseType.OK)
            if response == Gtk.ResponseType.APPLY:
                self.open_save_backups(game)
        self._run_saves_job(game, "backup", job, done)

    def restore_game_saves(self, game: GameItem):
        """Picks one of the game's backups and puts it back - the saves
        it replaces backed up first."""
        backups = list_save_backups(game.uid)
        if not backups:
            return
        choice = self._pick_save_choice(game, "restore")
        if choice is None:
            return
        locations = choice.locations
        both = len(self.save_location_choices(game)) > 1
        chooser = Gtk.ListBox()
        chooser.get_style_context().add_class("saves-backup-list")
        chooser.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for backup in backups[:30]:
            row = Gtk.Box(spacing=10)
            row.set_border_width(8)
            when = Gtk.Label(label=time.strftime("%Y-%m-%d  %H:%M:%S", time.localtime(backup.created)),
                             xalign=0)
            row.pack_start(when, True, True, 0)
            if backup.before_restore:
                tag = Gtk.Label(label=self._("saves_before_restore_tag"))
                tag.get_style_context().add_class("dim-label")
                row.pack_start(tag, False, False, 0)
            if both and backup.variant:
                # which install's saves it holds
                variant = Gtk.Label(label=self._(f"saves_variant_{backup.variant}"))
                variant.get_style_context().add_class("dim-label")
                row.pack_start(variant, False, False, 0)
            size = Gtk.Label(label=format_bytes(backup.size))
            size.get_style_context().add_class("dim-label")
            row.pack_end(size, False, False, 0)
            chooser.add(row)
        chooser.select_row(chooser.get_row_at_index(0))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(min(len(backups), 5) * 38)
        scroller.set_propagate_natural_height(True)
        scroller.add(chooser)
        response = self._card_dialog(
            self._("saves_restore_title").format(title=game.title),
            self._("saves_restore_where").format(
                source=self._home_path(save_backup_dir(game.uid)),
                target="\n".join(self._home_path(Path(folder)) for _name, folder in locations))
            + "\n\n" + self._("saves_restore_text"),
            [(self._("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost"),
             (self._("saves_restore"), Gtk.ResponseType.OK, "danger")],
            tone="warning", icon_name="rotate-ccw-symbolic", extra=scroller)
        selected = chooser.get_selected_row()
        if response != Gtk.ResponseType.OK or selected is None:
            return
        backup = backups[selected.get_index()]
        if not self._steam_closed_for_saves(game, "restore"):
            return

        def job():
            restore_saves(backup.path, game.uid, game.title, locations, choice.variant)
            return True, ""
        self._run_saves_job(game, "restore", job)

    def _steam_closed_for_saves(self, game: GameItem, mode: str) -> bool:
        """A Steam game's saves are Steam Cloud's folder, which a running
        Steam syncs - and may overwrite. Offers to close Steam first;
        True once it isn't running."""
        if not game.uid.startswith("steam:"):
            return True
        steam = SteamBackend()
        if not steam.blocking_process_running():
            return True
        response = self._card_dialog(
            self._("saves_steam_running_title"), self._(f"saves_steam_running_{mode}"),
            [(self._("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost"),
             (self._("saves_close_steam"), Gtk.ResponseType.OK, "accent")],
            tone="warning")
        if response != Gtk.ResponseType.OK:
            return False
        if not self._lo_close_steam(self._dialog_transient_parent(), steam):
            self._show_info_dialog(self._("saves_steam_still_running"), Gtk.MessageType.ERROR)
            return False
        return True

    def open_save_backups(self, game: GameItem):
        folder = save_backup_dir(game.uid)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Couldn't make {folder}: {e}")
            return
        self._show_folder(folder)

    def _saves_busy(self, uid: Optional[str]) -> bool:
        return bool(uid) and not self._cloud_syncs.get(uid, "done:").startswith("done:")

    def save_location_choices(self, game: GameItem) -> List[SaveChoice]:
        """Where the game's saves are, per install of its launcher - a
        native and a Flatpak Steam or Heroic side by side each have
        their own. Heroic's synced folders, Steam Cloud's, else the one
        PlunderHub finds - only a precise one, as a restore replaces
        everything in it."""
        choices: List[SaveChoice] = []
        for cloud in cloud_saves_all(game.uid):
            if cloud.locations:
                choices.append(SaveChoice("flatpak" if cloud.flatpak else "native", cloud.locations, cloud))
        if not choices and game.uid.startswith("steam:"):
            for remote in self._steam_cloud_remotes(game.uid.split(":", 1)[1]):
                choices.append(SaveChoice("flatpak" if "/.var/app/" in str(remote) else "native",
                                          [("saves", str(remote))]))
        if not choices:
            try:
                folder = self._resolve_save_dir(game, broad=False)
            except Exception as e:
                logger.debug(f"Couldn't find {game.title}'s saves: {e}")
                folder = None
            if folder is not None and folder.is_dir():
                choices.append(SaveChoice("flatpak" if "/.var/app/" in str(folder) else "native",
                                          [("saves", str(folder))]))
        unique: List[SaveChoice] = []
        for choice in choices:
            if all(c.locations != choice.locations for c in unique):
                unique.append(choice)
        return unique

    def save_locations(self, game: GameItem) -> List[Tuple[str, str]]:
        choices = self.save_location_choices(game)
        return choices[0].locations if choices else []

    def _pick_save_choice(self, game: GameItem, purpose: str) -> Optional[SaveChoice]:
        """The install whose saves to use - asked when there's both a
        native and a Flatpak one - once nothing else is at them."""
        if self._saves_busy(game.uid):
            return None
        if self._is_game_running(game.uid) or game.uid in self.launching_uids:
            self._show_info_dialog(self._("cloud_close_game_first"), Gtk.MessageType.WARNING)
            return None
        choices = self.save_location_choices(game)
        if not choices:
            self._show_info_dialog(self._("cloud_no_folder"), Gtk.MessageType.WARNING)
            return None
        if len(choices) == 1:
            return choices[0]
        where = "\n".join(f"{self._(f'saves_variant_{c.variant}')}: "
                          + ", ".join(self._home_path(Path(folder)) for _n, folder in c.locations)
                          for c in choices)
        buttons = [(self._("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost")]
        for i, choice in enumerate(choices):
            buttons.append((self._(f"saves_variant_{choice.variant}"), i + 1, "accent"))
        response = self._card_dialog(
            self._(f"saves_which_{purpose}").format(launcher=game.launcher),
            self._("saves_which_text").format(launcher=game.launcher) + "\n\n" + where,
            buttons, tone="question", default=Gtk.ResponseType.CANCEL)
        if isinstance(response, int) and 1 <= response <= len(choices):
            return choices[response - 1]
        return None

    def _steam_cloud_remotes(self, appid: str) -> List[Path]:
        """Steam Cloud's folder of the game in every Steam - native and
        Flatpak each keep their own userdata."""
        found = []
        for root in steam_roots():
            try:
                accounts = list((root / "userdata").iterdir())
            except OSError:
                continue
            found += [acc / appid / "remote" for acc in accounts if (acc / appid / "remote").is_dir()]
        return found

    @staticmethod
    def _home_path(path: Path) -> str:
        text = str(path)
        home = str(Path.home())
        return "~" + text[len(home):] if text.startswith(home + "/") else text

    def _run_saves_job(self, game: GameItem, mode: str, job: Callable[[], Tuple[bool, str]],
                       on_done: Optional[Callable[[], None]] = None):
        """Runs a sync, backup or restore off the main thread, said on
        the game's cards while it runs; on_done once it worked."""
        self._cloud_syncs[game.uid] = mode
        self._render_launchers()

        def work():
            try:
                ok, out = job()
            except Exception as e:
                logger.warning(f"{mode} of {game.uid}'s saves failed: {e}")
                ok, out = False, str(e)
            GLib.idle_add(self._finish_saves_job, game, mode, ok, out, on_done)
        threading.Thread(target=work, name="plunderhub-saves", daemon=True).start()

    def _finish_saves_job(self, game: GameItem, mode: str, ok: bool, out: str,
                          on_done: Optional[Callable[[], None]] = None) -> bool:
        if ok:
            # said on the card for a moment, then gone
            self._cloud_syncs[game.uid] = f"done:{mode}"
            GLib.timeout_add(3000, self._clear_saves_job, game.uid)
        else:
            self._cloud_syncs.pop(game.uid, None)
        self._render_launchers()
        if not ok:
            which = mode if mode in ("backup", "restore") else "sync"
            tail = "\n".join(out.strip().splitlines()[-4:])
            self._show_info_dialog(self._(f"saves_{which}_failed_text") + (f"\n\n{tail}" if tail else ""),
                                   Gtk.MessageType.ERROR,
                                   heading=self._(f"saves_{which}_failed").format(title=game.title))
        elif on_done is not None:
            on_done()
        return False

    def _clear_saves_job(self, uid: str) -> bool:
        if self._cloud_syncs.get(uid, "").startswith("done:"):
            self._cloud_syncs.pop(uid, None)
            self._render_launchers()
        return False

    def workspace_mover(self) -> Optional[WorkspaceMover]:
        """What moves game windows on this desktop - looked for again
        (every 10 s at most) while there's none: started with the login,
        PlunderHub can come up before the window manager does."""
        if self._workspace_mover is None and time.time() - self._workspace_detected_at >= 10:
            self._workspace_detected_at = time.time()
            compositor = detect_compositor()
            if compositor is not None:
                self._workspace_mover = WorkspaceMover(compositor)
        return self._workspace_mover

    def _ignore_detection_briefly(self, uid: str) -> None:
        self._detection_ignored_until[uid] = time.monotonic() + 8.0

    def _poll_running_games(self) -> bool:
        self._sync_detected_games()
        self._process_probe.set_targets({
            uid: (uid.split(":", 1)[1] if uid.startswith("steam:") else None,
                  self._running_process_match.get(uid))
            for uid in self.running_procs
            if uid.startswith("steam:") or uid.startswith("bottles:")
        }, launches={uid: started_at for uid, started_at in self.launching_uids.items()
                     if uid.startswith("steam:")})

        finished = []
        detected = self._running_detector.snapshot()
        for uid, proc in self.running_procs.items():
            if uid.startswith("steam:"):
                steam_running = self._process_probe.reading(uid)
                if steam_running is True:
                    self._directory_confirmed_running.add(uid)
                elif steam_running is False and uid in self._directory_confirmed_running:
                    finished.append(uid)
                elif self._unconfirmed_run_timed_out(uid, proc):
                    finished.append(uid)
                continue
            if uid.startswith("bottles:"):
                bottles_running = self._process_probe.reading(uid)
                if bottles_running is True:
                    self._directory_confirmed_running.add(uid)
                elif bottles_running is False and uid in self._directory_confirmed_running:
                    finished.append(uid)
                elif self._unconfirmed_run_timed_out(uid, proc):
                    finished.append(uid)
                continue
            if uid.startswith(("heroic:", "lutris:")):
                # What PlunderHub ran only hands the launch to the launcher
                # (often one already open: the forwarder exits at once; or
                # Heroic itself, staying in its tray) - the game's own
                # processes, as the running detector finds them (Wine ones
                # by their command line too), say whether it runs.
                if uid in detected:
                    self._directory_confirmed_running.add(uid)
                elif uid in self._directory_confirmed_running:
                    finished.append(uid)
                elif uid not in self._heroic_launches and self._unconfirmed_run_timed_out(uid, proc):
                    finished.append(uid)
                continue
            if uid in self._heroic_launches:
                # what PlunderHub ran only hands the launch to Heroic
                continue
            if proc.poll() is not None and not self._process_group_alive(proc):
                finished.append(uid)
        failed_launches = []
        for uid in self.launching_uids:
            stage = self._process_probe.stage(uid)
            if stage is not None and stage.stage == "failed" and uid not in finished:
                finished.append(uid)
                failed_launches.append((uid, stage.error))
        for uid in self._follow_heroic_launches():
            if uid not in finished:
                finished.append(uid)
                failed_launches.append((uid, ""))
        self._ask_stalled_steam_again()
        for uid in finished:
            self.launching_uids.pop(uid, None)
        expired_launching = [
            uid for uid, started_at in self.launching_uids.items()
            if self._launch_settled(uid, started_at)
        ]
        for uid in expired_launching:
            self.launching_uids.pop(uid, None)
            self._launch_settled_at[uid] = time.time()
            self._end_heroic_launch(uid)
            # Monotonic, not wall-clock: it stops while the machine is
            # suspended, so a game left running overnight in sleep
            # doesn't come back with hours of playtime it never had.
            self.running_since[uid] = time.monotonic()
        if finished or expired_launching:
            for uid in finished:
                self._ignore_detection_briefly(uid)
                self.running_procs.pop(uid, None)
                self._end_heroic_launch(uid)
                if self._workspace_mover is not None:
                    # it quit by itself: back to PlunderHub's workspace
                    self._workspace_mover.back_home(uid)
                started_at = self.running_since.pop(uid, None)
                self._directory_confirmed_running.discard(uid)
                self._running_process_match.pop(uid, None)
                if started_at is not None and not uid.startswith("steam:"):
                    self._credit_session_playtime(uid, started_at)
            self._render_launchers()
            if self._console_hidden_for_game and not self.running_procs:
                self._restore_main_window(by_user=False)
            if finished:
                self.reload()
                # a game's first start may have made the prefix a restore waited for
                # (Wine writes the registry a few seconds after the game quits)
                GLib.timeout_add_seconds(10, self._bilge_check_waiting)
            self._update_decorative_animation_pause_state()
            for uid, error in failed_launches:
                GLib.idle_add(self._show_launch_failed, uid, error)
            return True

        for uid, widgets in self.running_time_widgets.items():
            if not widgets:
                continue
            text = self._format_running_badge_text(uid)
            for lbl in widgets:
                lbl.set_text(text)
        self._update_decorative_animation_pause_state()
        return True

    def _apply_tabular_figures(self, label: "Gtk.Label"):
        try:
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_font_features_new("tnum=1"))
            label.set_attributes(attrs)
        except Exception as e:
            logger.debug(f"Tabular figures unavailable on this Pango build: {e}")

    def _format_elapsed_time(self, seconds: float) -> str:
        total = max(0, int(seconds))
        hours, rem = divmod(total, 3600)
        mins, secs = divmod(rem, 60)
        if hours:
            return f"{hours}:{mins:02d}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"

    def _format_running_badge_text(self, uid: str) -> str:
        if uid in self.launching_uids:
            return self._("game_launching_badge")
        started_at = self.running_since.get(uid)
        if started_at is None:
            return self._("game_running_badge")
        elapsed = self._format_elapsed_time(time.monotonic() - started_at)
        return f"{self._('game_running_badge')} · {elapsed}"

    def _on_stop_game_clicked(self, _btn, game: GameItem):
        self._ignore_detection_briefly(game.uid)
        detection = self.detected_running.pop(game.uid, None)
        if detection is not None and game.uid not in self.running_procs:
            self._end_detected_session(game.uid)
            self._update_decorative_animation_pause_state()
            if detection.pids:
                self._terminate_pids(detection.pids, resync_playtime=True)
            else:
                match = self._game_process_match(game.uid, game.install_dir, game.title)
                if match is not None:
                    self._terminate_game_processes(match, resync_playtime=True)
            self._render_launchers()
            return
        proc = self.running_procs.pop(game.uid, None)
        started_at = self.running_since.pop(game.uid, None)
        self.launching_uids.pop(game.uid, None)
        self._end_heroic_launch(game.uid)
        if self._workspace_mover is not None:
            self._workspace_mover.forget(game.uid)
        self._directory_confirmed_running.discard(game.uid)
        process_match = (self._running_process_match.pop(game.uid, None)
                         or self._game_process_match(game.uid, game.install_dir, game.title))
        if proc is None:
            return
        self._update_decorative_animation_pause_state()
        if game.uid.startswith("bottles:") and process_match is not None:
            still_running = bool(is_game_process_running(process_match))
        else:
            still_running = proc.poll() is None or self._process_group_alive(proc)
        if still_running and game.launcher != "Steam" and started_at is not None:
            self._credit_session_playtime(game.uid, started_at)
        if process_match is not None:
            self._terminate_game_processes(process_match, resync_playtime=True)
        else:
            self._terminate_process(proc, resync_playtime=True)
        self._render_launchers()

    def _terminate_game_processes(self, process_match: GameProcessMatch, resync_playtime: bool = False):
        self._terminate_pids(find_game_pids(process_match), resync_playtime)

    def _terminate_pids(self, pids: List[int], resync_playtime: bool = False):
        if not pids:
            if resync_playtime:
                self.reload()
            return
        # Remembered so the SIGKILL below only ever hits these exact
        # processes - a pid that exited and got reused by something
        # unrelated in the meantime has a different start time.
        start_times = {pid: process_start_time(pid) for pid in pids}
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        def _force_kill():
            for pid, started in start_times.items():
                if started is None or process_start_time(pid) != started:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            if resync_playtime:
                self.reload()
            return False

        GLib.timeout_add(4000, _force_kill)

    def _credit_session_playtime(self, uid: str, started_at: float):
        # started_at is time.monotonic() - see running_since.
        per_day = split_minutes_by_day(datetime.now(), time.monotonic() - started_at)
        if not per_day:
            return
        for day, minutes in per_day.items():
            day_entry = self.playtime_history.setdefault(day, {})
            day_entry[uid] = day_entry.get(uid, 0) + minutes
        save_playtime_history(self.playtime_history)
        self.playtime[uid] = self.playtime.get(uid, 0) + sum(per_day.values())
        save_playtime(self.playtime)

    def _process_group_members(self, pgid: int) -> List[int]:
        members: List[int] = []
        try:
            proc_entries = [e for e in Path("/proc").iterdir() if e.name.isdigit()]
        except OSError:
            return members
        for entry in proc_entries:
            pid = int(entry.name)
            if pid == pgid:
                continue
            try:
                stat_text = (entry / "stat").read_text()
            except OSError:
                continue
            close = stat_text.rfind(')')
            if close == -1:
                continue
            fields = stat_text[close + 1:].split()
            if len(fields) < 3:
                continue
            try:
                proc_pgid = int(fields[2])
            except ValueError:
                continue
            if proc_pgid == pgid:
                members.append(pid)
        return members

    def _terminate_process(self, proc: subprocess.Popen, resync_playtime: bool = False):
        other_members = self._process_group_members(proc.pid)
        for pid in other_members:
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass

        def _terminate_top_level():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.terminate()
                except Exception as e:
                    logger.debug(f"Couldn't terminate process {proc.pid}: {e}")

            def _force_kill():
                # the wrapper may have ended on SIGTERM while the hung game in its group didn't
                if proc.poll() is None or self._process_group_alive(proc):
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError, OSError):
                        try:
                            proc.kill()
                        except Exception as e:
                            logger.debug(f"Couldn't force-kill process {proc.pid}: {e}")
                if resync_playtime:
                    self.reload()
                return False

            GLib.timeout_add(4000, _force_kill)
            return False

        if other_members:
            GLib.timeout_add(2000, _terminate_top_level)
        else:
            _terminate_top_level()

    _ICON_MARKER = "ph-tinted-icon"

    _LAUNCHING_GRACE_SECONDS = 6.0

    _UNCONFIRMED_RUNNING_TIMEOUT_SECONDS = 300.0

    def _build_running_dimmer(self) -> Gtk.Box:
        dimmer = Gtk.Box()
        dimmer.set_halign(Gtk.Align.FILL)
        dimmer.set_valign(Gtk.Align.FILL)
        dimmer.get_style_context().add_class("game-running-dimmer")
        return dimmer

    def _show_game_context_menu(self, widget, event, game: GameItem):
        if event.button != 3:
            return
        self._game_menu_at(widget, event, game)

    def _game_menu_at(self, widget: Gtk.Widget, event, game: GameItem):
        """The game's right-click menu, pointing where it was clicked."""
        from plunderhub.ui.game_page import GameMenu
        menu = GameMenu(self, game, widget)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(event.x), int(event.y), 1, 1
        menu.set_pointing_to(rect)
        menu.popup()
        return menu

    def _menu_item_with_icon(self, icon_name: str, label_text: str) -> Gtk.MenuItem:
        """A regular Gtk.MenuItem with an icon in front of its label.
        Gtk.ImageMenuItem exists for exactly this, but has been
        deprecated since GTK 3.10, and plenty of themes/gtk-menu-
        images=false setups hide its icon outright - building the row
        ourselves (a plain MenuItem holding an HBox of icon+label)
        renders consistently regardless of theme or that setting."""
        item = Gtk.MenuItem()
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        icon = self._icon_image(icon_name, Gtk.IconSize.MENU)
        row.pack_start(icon, False, False, 0)
        label = Gtk.Label(label=label_text)
        label.set_xalign(0)
        row.pack_start(label, True, True, 0)
        item.add(row)
        return item

    def _open_store_page(self, game: GameItem):
        webbrowser.open(resolve_store_url(game))

    def _open_protondb(self, game: GameItem):
        webbrowser.open(resolve_protondb_url(game))

    def _show_patch_notes(self, game: GameItem):
        """Opens the game's Steam news/patch-notes page in the
        browser (store.steampowered.com/news/app/{appid}) - this
        works the same way for every launcher, not just Steam itself.
        Steam's own game is used directly when this already IS a
        Steam game; for anything else (Heroic/GOG/Epic/Amazon), a
        best-effort title search against Steam's own storefront finds
        the most likely matching Steam release instead (see
        _resolve_steam_appid_for_patch_notes) - GOG's only public
        changelog access is a raw, unstyled JSON API with no readable
        webpage, and Epic/Amazon don't appear to have any equivalent
        public per-game patch-notes page at all, so there's no
        uniform way to do this using each store's own data instead.
        A network lookup is only needed (and only shown as a loading
        dialog) for that non-Steam case; a Steam game's own appid is
        already known locally, so it opens immediately."""
        if game.uid.startswith("steam:"):
            webbrowser.open(f"https://store.steampowered.com/news/app/{game.uid.split(':', 1)[1]}")
            return
        if self._browse_in_progress:
            return
        self._browse_in_progress = True
        self._loading_dialog = self._show_loading_dialog(self._("patch_notes_loading"))
        thread = threading.Thread(target=self._show_patch_notes_thread, args=(game,), daemon=True)
        thread.start()

    def _show_patch_notes_thread(self, game: GameItem):
        try:
            appid = self._resolve_steam_appid_for_patch_notes(game)
        except Exception as e:
            logger.warning(f"Patch notes lookup error ({game.title}): {e}")
            appid = None
        GLib.idle_add(self._finish_patch_notes_lookup, appid)

    def _finish_patch_notes_lookup(self, appid: Optional[str]):
        self._close_loading_dialog()
        self._browse_in_progress = False
        if appid:
            webbrowser.open(f"https://store.steampowered.com/news/app/{appid}")
        else:
            self._show_info_dialog(self._("patch_notes_not_found"), icon_name="dialog-information-symbolic")

    def _resolve_steam_appid_for_patch_notes(self, game: GameItem) -> Optional[str]:
        """A best-effort Steam appid match by title, via Steam's own
        storefront search (undocumented, but long-established and
        widely used for exactly this purpose - see the conversation
        that led to this). Returns None if nothing came back, or the
        response didn't look like the shape this endpoint normally
        returns - this can genuinely happen for a game that was never
        released on Steam at all, not just for a network failure
        (http_get_json already retries transient failures on its
        own), so a None here isn't necessarily an error."""
        safe_title = urllib.parse.quote(game.title, safe="")
        url = f"https://store.steampowered.com/api/storesearch/?term={safe_title}&l=english&cc=US"
        data = http_get_json(url, HEADERS)
        if not data:
            return None
        items = data.get("items")
        if not items:
            return None
        appid = items[0].get("id")
        return str(appid) if appid is not None else None

    def _show_folder(self, path, transient_for: Optional[Gtk.Window] = None) -> None:
        """Opens a folder in the file manager; on a desktop with nothing
        set to open folders, says so and shows the path instead."""
        try:
            Gtk.show_uri_on_window(transient_for or self, Path(path).as_uri(), Gdk.CURRENT_TIME)
        except GLib.Error as e:
            logger.warning(f"Couldn't open {path}: {e}")
            self._show_info_dialog(self._("folder_no_opener").format(path=self._home_path(Path(path))),
                                   message_type=Gtk.MessageType.WARNING, transient_for=transient_for)

    def _open_game_folder(self, game: GameItem):
        if not game.install_dir:
            return
        path = Path(game.install_dir)
        if not path.exists():
            self._show_info_dialog(self._("folder_not_found"), message_type=Gtk.MessageType.WARNING)
            return
        self._show_folder(path)

    def _open_wineprefix_folder(self, game: GameItem):
        prefix = self._resolve_wine_prefix(game)
        if prefix is None or not prefix.exists():
            self._show_info_dialog(self._("folder_not_found"), message_type=Gtk.MessageType.WARNING)
            return
        self._show_folder(prefix)

    @staticmethod
    def _maybe_enable_save_debug_logging() -> None:
        if os.environ.get("PLUNDERHUB_SAVE_DEBUG") == "1":
            logger.setLevel(logging.DEBUG)

    def _find_steam_cloud_remote(self, appid: str) -> Optional[Path]:
        self._maybe_enable_save_debug_logging()
        roots = list(steam_roots())
        logger.debug(f"[SAVE_DEBUG] Steam appid {appid}: steam_roots = {roots}")
        for root in roots:
            userdata = root / "userdata"
            if not userdata.is_dir():
                logger.debug(f"[SAVE_DEBUG]   {userdata} does not exist")
                continue
            try:
                account_dirs = list(userdata.iterdir())
            except OSError:
                continue
            for acc_dir in account_dirs:
                remote = acc_dir / appid / "remote"
                logger.debug(f"[SAVE_DEBUG]   checking {remote} "
                             f"(exists={remote.is_dir()})")
                if remote.is_dir():
                    return remote
        return None

    def _find_wine_user_dir(self, prefix: Path) -> Optional[Path]:
        users_dir = prefix / "drive_c" / "users"
        if not users_dir.is_dir():
            return None
        preferred = users_dir / "steamuser"
        if preferred.is_dir():
            return preferred
        system_user = os.environ.get("USER") or os.environ.get("LOGNAME")
        if system_user:
            candidate = users_dir / system_user
            if candidate.is_dir():
                return candidate
        try:
            for child in sorted(users_dir.iterdir()):
                if child.is_dir() and child.name not in ("Public", "Default", "Default User", "All Users"):
                    return child
        except OSError:
            pass
        return None

    def _lutris_game_config_path(self, game: GameItem) -> Optional[Path]:
        if not game.uid.startswith("lutris:"):
            return None
        game_id = game.uid.split(":", 1)[1]
        return lutris_game_config_path(read_lutris_config_id(game_id) or "")

    def _lutris_wine_prefix_from_config(self, game: GameItem) -> Optional[Path]:
        cfg = self._lutris_game_config_path(game)
        if cfg is None:
            return None
        try:
            text = cfg.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        for key in ("prefix", "wine_prefix"):
            m = re.search(rf'^\s*{key}:\s*(.+)$', text, re.MULTILINE)
            if m:
                raw = m.group(1).strip().strip("'\"")
                if raw:
                    p = Path(os.path.expanduser(raw))
                    if (p / "drive_c").is_dir():
                        return p
        return None

    def _heroic_game_config(self, game: GameItem) -> Optional[dict]:
        parts = game.uid.split(":", 2)
        if len(parts) < 3 or parts[0] != "heroic":
            return None
        app_name = parts[2]
        for base in HEROIC_CONFIG_ROOTS:
            cfg_path = base / "heroic" / "GamesConfig" / f"{app_name}.json"
            data = load_json(cfg_path)
            if isinstance(data, dict):
                inner = data.get(app_name)
                if isinstance(inner, dict):
                    return inner
        return None

    def _heroic_legendary_cloud_save_folder(self, game: GameItem) -> Optional[str]:
        parts = game.uid.split(":", 2)
        if len(parts) < 3 or parts[0] != "heroic":
            return None
        app_name = parts[2]
        for base in HEROIC_CONFIG_ROOTS:
            meta_path = base / "heroic" / "legendaryConfig" / "legendary" / "metadata" / f"{app_name}.json"
            data = load_json(meta_path)
            if not isinstance(data, dict):
                continue
            metadata = data.get("metadata")
            if not isinstance(metadata, dict):
                continue
            attrs = metadata.get("customAttributes")
            if not isinstance(attrs, dict):
                continue
            entry = attrs.get("CloudSaveFolder")
            if isinstance(entry, dict):
                value = entry.get("value")
                if isinstance(value, str) and value.strip():
                    return value
        return None

    def _find_legendary_binary(self) -> Optional[Path]:
        if self._legendary_bin_checked:
            return self._legendary_bin
        self._legendary_bin_checked = True
        which = shutil.which("legendary")
        if which:
            self._legendary_bin = Path(which)
            return self._legendary_bin
        candidates = [
            Path("/opt/Heroic/resources/app.asar.unpacked/build/bin"),
            Path("/app/bin/heroic/resources/app.asar.unpacked/build/bin"),
            Path("/app/opt/heroic/resources/app.asar.unpacked/build/bin"),
        ]
        for base in candidates:
            if not base.is_dir():
                continue
            try:
                found = sorted(base.rglob("legendary"))
            except OSError:
                continue
            for p in found:
                if p.is_file() and os.access(p, os.X_OK):
                    self._legendary_bin = p
                    return p
        return None

    def _heroic_legendary_sync_saves_path(self, game: GameItem) -> Optional[Path]:
        """Legendary's save path for an Epic game - remembered: asking it goes
        over the network (up to 15 s). On the GTK thread (a menu, the game
        page) it's asked in the background and known from the next look."""
        cache = self.__dict__.setdefault("_legendary_save_paths", {})
        if game.uid in cache:
            return cache[game.uid]
        if threading.current_thread() is not threading.main_thread():
            cache[game.uid] = self._heroic_legendary_sync_saves_path_now(game)
            return cache[game.uid]
        asking = self.__dict__.setdefault("_legendary_save_asking", set())
        if game.uid not in asking:
            asking.add(game.uid)

            def work():
                path = self._heroic_legendary_sync_saves_path_now(game)
                GLib.idle_add(lambda: (cache.__setitem__(game.uid, path), asking.discard(game.uid)) and False)
            threading.Thread(target=work, daemon=True, name="plunderhub-legendary-saves").start()
        return None

    def _heroic_legendary_sync_saves_path_now(self, game: GameItem) -> Optional[Path]:
        if (game.store_platform or "epic") != "epic":
            return None
        parts = game.uid.split(":", 2)
        if len(parts) < 3 or parts[0] != "heroic":
            return None
        app_name = parts[2]
        binary = self._find_legendary_binary()
        if binary is None:
            return None
        config_path = None
        for base in HEROIC_CONFIG_ROOTS:
            candidate = base / "heroic" / "legendaryConfig" / "legendary"
            if candidate.is_dir():
                config_path = candidate
                break
        if config_path is None:
            return None
        env = dict(os.environ)
        env["LEGENDARY_CONFIG_PATH"] = str(config_path)
        try:
            result = subprocess.run(
                [str(binary), "sync-saves", app_name,
                 "--skip-upload", "--skip-download", "--accept-path"],
                env=env, capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return None
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        m = re.search(r"[Ss]ave path:\s*(.+)", output)
        if not m:
            return None
        raw_path = m.group(1).strip().strip('"')
        if not raw_path:
            return None
        p = Path(raw_path)
        return p if p.is_dir() else None

    def _heroic_wine_prefix(self, game: GameItem) -> Optional[Path]:
        cfg = self._heroic_game_config(game)
        if not cfg:
            return None
        raw = cfg.get("winePrefix")
        if not raw:
            return None
        p = Path(os.path.expanduser(str(raw)))
        if (p / "drive_c").is_dir():
            return p
        return None

    def _normalize_title_for_match(self, s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    def _guess_save_dir_by_title(self, game: GameItem, user_dir: Path) -> Optional[Path]:
        target = self._normalize_title_for_match(game.title)
        if not target:
            return None
        roots = [
            user_dir / "AppData" / "LocalLow",
            user_dir / "AppData" / "Roaming",
            user_dir / "AppData" / "Local",
            user_dir / "Documents" / "My Games",
            user_dir / "Saved Games",
            user_dir / "Documents",
        ]
        exact_matches: List[Path] = []
        substring_matches: List[Path] = []
        for root in roots:
            if not root.is_dir():
                continue
            try:
                level1_names = os.listdir(root)
            except OSError:
                continue
            for name1 in level1_names:
                d1 = root / name1
                if not d1.is_dir():
                    continue
                self._match_title_candidate(name1, target, d1, exact_matches, substring_matches)
                try:
                    level2_names = os.listdir(d1)
                except OSError:
                    continue
                for name2 in level2_names:
                    d2 = d1 / name2
                    if d2.is_dir():
                        self._match_title_candidate(name2, target, d2, exact_matches, substring_matches)
        unique_exact = list({str(m): m for m in exact_matches}.values())
        if len(unique_exact) == 1:
            return self._prefer_save_subfolder(unique_exact[0])
        unique_substring = list({str(m): m for m in substring_matches}.values())
        return self._prefer_save_subfolder(unique_substring[0]) if len(unique_substring) == 1 else None

    def _prefer_save_subfolder(self, path: Path) -> Path:
        try:
            entries = os.listdir(path)
        except OSError:
            return path
        save_names = ("savegames", "save games", "saves", "save", "savedata", "save data")
        candidates = [path / e for e in entries
                      if (path / e).is_dir() and e.lower().replace(" ", "") in
                      {n.replace(" ", "") for n in save_names}]
        return candidates[0] if len(candidates) == 1 else path

    def _match_title_candidate(self, name: str, target: str, path: Path,
                                exact_matches: List[Path], substring_matches: List[Path]):
        normalized = self._normalize_title_for_match(name)
        if not normalized:
            return
        if normalized == target:
            exact_matches.append(path)
        elif len(normalized) >= 4 and len(target) >= 4 and (normalized in target or target in normalized):
            substring_matches.append(path)

    def _resolve_path_case_insensitive(self, base: Path, parts: List[str]) -> List[Path]:
        candidates = [base]
        for part in parts:
            if not part or part == ".":
                continue
            next_candidates: List[Path] = []
            for current in candidates:
                if not current.is_dir():
                    continue
                try:
                    entries = os.listdir(current)
                except OSError:
                    continue
                if part == "*":
                    next_candidates.extend(current / e for e in entries if (current / e).is_dir())
                else:
                    match = next((e for e in entries if e.lower() == part.lower()), None)
                    if match is not None and (current / match).is_dir():
                        next_candidates.append(current / match)
            candidates = next_candidates
            if not candidates:
                return []
        return candidates

    def _heroic_resolve_save_template(self, template: str, user_dir: Path) -> Optional[Path]:
        if not template:
            return None
        raw = template.strip().replace("\\", "/")
        known = {"appdata": "AppData/Roaming", "localappdata": "AppData/Local",
                 "userdir": "", "userprofile": ""}
        raw = re.sub(r"\{([^}]*)\}", lambda m: known.get(m.group(1).lower(), m.group(0)), raw)
        norm_parts = [p for p in os.path.normpath(raw).split("/") if p not in ("", ".")]
        norm_parts = ["*" if re.fullmatch(r"\{[^}]*\}", p) else p for p in norm_parts]
        matches = self._resolve_path_case_insensitive(user_dir, norm_parts)
        return matches[0] if len(matches) == 1 else None

    def _heroic_save_dir(self, game: GameItem, user_dir: Path) -> Optional[Path]:
        self._maybe_enable_save_debug_logging()

        def dbg(msg: str):
            logger.debug(f"[SAVE_DEBUG] {game.title}: {msg}")

        cloud_save_folder = self._heroic_legendary_cloud_save_folder(game)
        dbg(f"Legendary CloudSaveFolder template = {cloud_save_folder!r}")
        if cloud_save_folder:
            resolved = self._heroic_resolve_save_template(cloud_save_folder, user_dir)
            dbg(f"  -> resolved to {resolved}")
            if resolved is not None:
                return resolved

        cfg = self._heroic_game_config(game)
        dbg(f"GamesConfig keys = {sorted(cfg.keys()) if cfg else cfg}")
        if cfg:
            saves_path = cfg.get("savesPath")
            dbg(f"savesPath template = {saves_path!r}")
            if isinstance(saves_path, str) and saves_path.strip():
                resolved = self._heroic_resolve_save_template(saves_path, user_dir)
                dbg(f"  -> resolved to {resolved}")
                if resolved is not None:
                    return resolved
            gog_saves = cfg.get("gogSaves")
            dbg(f"gogSaves = {gog_saves!r}")
            if isinstance(gog_saves, list):
                for entry in gog_saves:
                    if not isinstance(entry, dict):
                        continue
                    path_str = entry.get("location") or entry.get("path")
                    if not isinstance(path_str, str) or not path_str.strip():
                        continue
                    path_str = path_str.strip()
                    if path_str.startswith("/"):
                        direct = Path(path_str)
                        dbg(f"  gogSaves entry (absolute) {path_str!r} -> exists={direct.is_dir()}")
                        if direct.is_dir():
                            return direct
                        continue
                    resolved = self._heroic_resolve_save_template(path_str, user_dir)
                    dbg(f"  gogSaves entry (template) {path_str!r} -> resolved to {resolved}")
                    if resolved is not None:
                        return resolved

        if (game.store_platform or "epic") == "epic":
            binary = self._find_legendary_binary()
            dbg(f"legendary binary = {binary}")
            cli_resolved = self._heroic_legendary_sync_saves_path(game)
            dbg(f"`legendary sync-saves` result = {cli_resolved}")
            if cli_resolved is not None:
                return cli_resolved

        dbg("no precise save dir found - falling back to the Wine user directory")
        return None

    def _resolve_wine_prefix(self, game: GameItem) -> Optional[Path]:
        if game.env and game.env.get("WINEPREFIX"):
            p = Path(game.env["WINEPREFIX"])
            if (p / "drive_c").is_dir():
                return p
        if game.launcher == "Lutris":
            p = self._lutris_wine_prefix_from_config(game)
            if p is not None:
                return p
        if game.launcher == "Heroic":
            p = self._heroic_wine_prefix(game)
            if p is not None:
                return p
        if game.install_dir:
            p = Path(game.install_dir)
            if (p / "drive_c").is_dir():
                return p
        if game.uid.startswith("steam:"):
            appid = game.uid.split(":", 1)[1]
            for lib in steam_libraries():
                pfx = lib / "steamapps" / "compatdata" / appid / "pfx"
                if (pfx / "drive_c").is_dir():
                    return pfx
        return None

    def _resolve_save_dir(self, game: GameItem, broad: bool = True) -> Optional[Path]:
        """broad: the prefix's whole user folder when nothing closer
        is found - fine to open, never to back up and restore over."""
        self._maybe_enable_save_debug_logging()
        if game.uid.startswith("steam:"):
            appid = game.uid.split(":", 1)[1]
            remote = self._find_steam_cloud_remote(appid)
            logger.debug(f"[SAVE_DEBUG] {game.title}: Steam Cloud remote = {remote}")
            if remote is not None:
                return remote
        prefix = self._resolve_wine_prefix(game)
        logger.debug(f"[SAVE_DEBUG] {game.title}: launcher={game.launcher!r} "
                     f"wine prefix = {prefix}")
        if prefix is not None:
            user_dir = self._find_wine_user_dir(prefix)
            logger.debug(f"[SAVE_DEBUG] {game.title}: wine user dir = {user_dir}")
            if user_dir is not None:
                if game.launcher == "Heroic":
                    precise = self._heroic_save_dir(game, user_dir)
                    logger.debug(f"[SAVE_DEBUG] {game.title}: Heroic precise save dir "
                                 f"= {precise}")
                    if precise is not None:
                        return precise
                guessed = self._guess_save_dir_by_title(game, user_dir)
                logger.debug(f"[SAVE_DEBUG] {game.title}: title-match guess "
                             f"= {guessed}")
                if guessed is not None:
                    return guessed
                return user_dir if broad else None
        return None

    def _open_game_saves(self, game: GameItem):
        save_dir = self._resolve_save_dir(game)
        if save_dir is None or not save_dir.exists():
            self._show_info_dialog(self._("folder_not_found"), message_type=Gtk.MessageType.WARNING)
            return
        self._show_folder(save_dir)

    def _steam_proton_version(self, game: GameItem) -> Optional[str]:
        appid = game.uid.split(":", 1)[1]
        for lib in steam_libraries():
            version_file = lib / "steamapps" / "compatdata" / appid / "version"
            if version_file.is_file():
                try:
                    text = version_file.read_text(encoding="utf-8", errors="ignore").strip()
                except OSError:
                    continue
                if text:
                    return text
        return None

    def _steam_game_version(self, game: GameItem) -> Optional[str]:
        appid = game.uid.split(":", 1)[1]
        for lib in steam_libraries():
            manifest = lib / "steamapps" / f"appmanifest_{appid}.acf"
            if manifest.is_file():
                try:
                    text = manifest.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                m = re.search(r'"buildid"\s*"(\d+)"', text)
                if m:
                    return f"Build {m.group(1)}"
        return None

    def _lutris_runner_version(self, game: GameItem) -> Optional[str]:
        cfg = self._lutris_game_config_path(game)
        if cfg is None:
            return None
        try:
            text = cfg.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        m = re.search(r'^\s*version:\s*(.+)$', text, re.MULTILINE)
        if m:
            return m.group(1).strip().strip("'\"") or None
        return None

    def _heroic_runner_version(self, game: GameItem) -> Optional[str]:
        cfg = self._heroic_game_config(game)
        if not cfg:
            return None
        wine_version = cfg.get("wineVersion")
        if isinstance(wine_version, dict):
            name = wine_version.get("name")
            if name:
                return str(name)
        return None

    def _heroic_game_version(self, game: GameItem) -> Optional[str]:
        parts = game.uid.split(":", 2)
        if len(parts) < 3:
            return None
        game_id = parts[2]
        for path in heroic_roots():
            data = load_json(path)
            items = []
            if isinstance(data, dict) and isinstance(data.get("installed"), list):
                items = [(None, item) for item in data["installed"]]
            elif isinstance(data, list):
                items = [(None, item) for item in data]
            elif isinstance(data, dict):
                # Legendary's installed.json: {app_name: {...}} - the
                # Epic games, whose version was never found before.
                items = list(data.items())
            for key, item in items:
                if not isinstance(item, dict):
                    continue
                candidate_id = (key or item.get("app_name") or item.get("game_id")
                                or item.get("appName") or item.get("id"))
                if str(candidate_id) == game_id:
                    version = item.get("version")
                    if not version and isinstance(item.get("install"), dict):
                        version = item["install"].get("version")
                    if version:
                        return str(version)
        return None

    def _faugus_runner_version(self, game: GameItem) -> Optional[str]:
        game_id = game.uid.split(":", 1)[1] if game.uid.startswith("faugus:") else None
        if not game_id:
            return None
        data = load_json(faugus_games_path())
        if not isinstance(data, list):
            return None
        for item in data:
            if isinstance(item, dict) and str(item.get("gameid")) == game_id:
                for key in ("runner", "proton", "proton_version", "wine_version"):
                    val = item.get(key)
                    if val:
                        return str(val)
        return None

    def _bottles_runner_version(self, game: GameItem) -> Optional[str]:
        if not game.uid.startswith("bottles:") or not game.install_dir:
            return None
        try:
            bottle_cfg = yaml.safe_load(
                (Path(game.install_dir) / "bottle.yml").read_text(encoding="utf-8")
            )
        except Exception:
            return None
        if not isinstance(bottle_cfg, dict):
            return None
        for key in ("Runner", "runner"):
            val = bottle_cfg.get(key)
            if val:
                return str(val)
        return None

    def _resolve_runner_version(self, game: GameItem) -> Optional[str]:
        if game.uid.startswith("steam:"):
            return self._steam_proton_version(game)
        if game.launcher == "Faugus":
            return self._faugus_runner_version(game)
        if game.launcher == "Lutris":
            return self._lutris_runner_version(game)
        if game.launcher == "Bottles":
            return self._bottles_runner_version(game)
        if game.launcher == "Heroic":
            return self._heroic_runner_version(game)
        return None

    def _resolve_game_version(self, game: GameItem) -> Optional[str]:
        if game.uid.startswith("steam:"):
            return self._steam_game_version(game)
        if game.launcher == "Heroic":
            return self._heroic_game_version(game)
        return None

    def _dir_size_bytes(self, path: Path) -> int:
        total = 0
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            total += self._dir_size_bytes(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except (OSError, PermissionError):
                        continue
        except (OSError, PermissionError):
            pass
        return total

    def _toggle_favorite(self, game: GameItem):
        if game.uid in self.favorites:
            self.favorites.discard(game.uid)
        else:
            self.favorites.add(game.uid)
        save_favorites(self.favorites)
        self._render_launchers()

    def _hide_game(self, game: GameItem):
        self.scanner.hide(game.launcher, game.uid)
        self._render_launchers()
        self._show_undo_toast(
            self._("hide_toast_text").format(name=game.title),
            lambda launcher=game.launcher, uid=game.uid: self._undo_hide_game(launcher, uid),
            icon_name="view-conceal-symbolic",
        )

    def _undo_hide_game(self, launcher: str, uid: str):
        self.scanner.unhide(launcher, uid)
        self._render_launchers()

    def _on_game_card_clicked(self, btn, game: GameItem):
        if self.selection_mode:
            self._toggle_selection(game.uid)
            return
        asked = self.launch_mode == "confirm"
        self.request_launch(game.launch_cmd, game.title, game.env, game.uid, game.install_dir)
        if asked:
            # The "Ready to set sail?" dialog took the pointer and gave
            # focus back to the card - which then stayed highlighted,
            # whichever button was pressed.
            self._release_card_after_dialog(btn)

    def _enter_selection_mode(self):
        if self.selection_mode:
            return
        self.selection_mode = True
        self._update_bulk_bar_visibility()
        self._render_launchers()

    def _exit_selection_mode(self):
        self.selection_mode = False
        self.selected_uids.clear()
        self._update_bulk_bar_visibility()
        self._render_launchers()

    def _set_bar_visible(self, bar: "Gtk.Widget", visible: bool) -> None:
        # no_show_all(True) silently blocks show_all() - must flip it off first
        if visible:
            bar.set_no_show_all(False)
            bar.show_all()
        else:
            bar.hide()
            bar.set_no_show_all(True)

    def _build_update_banner(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.get_style_context().add_class("cover-hint-bar")

        self.update_banner_icon = self._icon_image("dialog-information-symbolic", Gtk.IconSize.BUTTON)
        bar.pack_start(self.update_banner_icon, False, False, 6)

        self.update_banner_label = Gtk.Label(label="")
        self.update_banner_label.set_xalign(0)
        self.update_banner_label.set_line_wrap(True)
        self.update_banner_label.set_hexpand(True)
        bar.pack_start(self.update_banner_label, True, True, 0)

        self.update_banner_view_btn = Gtk.Button(label=self._("update_check_view_btn"))
        self.update_banner_view_btn.connect("clicked", lambda *_: self._on_update_banner_view_clicked())
        bar.pack_start(self.update_banner_view_btn, False, False, 0)

        self.update_banner_close_btn = Gtk.Button()
        self.update_banner_close_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.update_banner_close_btn.set_image(self._icon_image("window-close-symbolic", Gtk.IconSize.BUTTON))
        self.update_banner_close_btn.set_tooltip_text(self._("dismiss_tooltip"))
        self.update_banner_close_btn.get_accessible().set_name(self._("dismiss_tooltip"))
        self.update_banner_close_btn.connect("clicked", lambda *_: self._dismiss_update_banner())
        bar.pack_end(self.update_banner_close_btn, False, False, 0)

        return bar

    def _build_soft_deps_banner(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.get_style_context().add_class("cover-hint-bar")

        self.soft_deps_banner_icon = self._icon_image("dialog-warning-symbolic", Gtk.IconSize.BUTTON)
        bar.pack_start(self.soft_deps_banner_icon, False, False, 6)

        self.soft_deps_banner_label = Gtk.Label(label="")
        self.soft_deps_banner_label.set_xalign(0)
        self.soft_deps_banner_label.set_line_wrap(True)
        self.soft_deps_banner_label.set_hexpand(True)
        bar.pack_start(self.soft_deps_banner_label, True, True, 0)

        self.soft_deps_install_btn = Gtk.Button(label=self._("soft_deps_install_btn"))
        self.soft_deps_install_btn.connect("clicked", lambda *_: self._on_install_missing_deps_clicked())
        bar.pack_start(self.soft_deps_install_btn, False, False, 0)

        self.soft_deps_banner_close_btn = Gtk.Button()
        self.soft_deps_banner_close_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.soft_deps_banner_close_btn.set_image(self._icon_image("window-close-symbolic", Gtk.IconSize.BUTTON))
        self.soft_deps_banner_close_btn.set_tooltip_text(self._("dismiss_tooltip"))
        self.soft_deps_banner_close_btn.get_accessible().set_name(self._("dismiss_tooltip"))
        self.soft_deps_banner_close_btn.connect(
            "clicked", lambda *_: self._set_bar_visible(self.soft_deps_banner_bar, False))
        bar.pack_end(self.soft_deps_banner_close_btn, False, False, 0)

        return bar

    def _maybe_show_soft_deps_banner(self):
        """Called once at startup. cairo (rounded cover/icon corners)
        and libsecret (secure API-key storage) are both "soft" - the
        app already runs fine without them (see covers_mixin.py's own
        guarded `import cairo` and secret_store.py's guarded `import
        gi.repository.Secret`) - so this only ever informs and offers
        a fix, it never blocks startup the way the hard dependency
        check in plunderhub.py does."""
        from plunderhub.core.dependency_check import SOFT_DEPS, missing_dependencies
        self._missing_soft_deps = missing_dependencies(SOFT_DEPS)
        if not self._missing_soft_deps:
            return
        self._update_soft_deps_banner_text()
        self._set_bar_visible(self.soft_deps_banner_bar, True)

    def _update_soft_deps_banner_text(self):
        from plunderhub.core.dependency_check import DEP_LABELS
        labels = ", ".join(DEP_LABELS.get(d, d) for d in self._missing_soft_deps)
        self.soft_deps_banner_label.set_text(self._("soft_deps_missing_text").format(deps=labels))

    def _on_install_missing_deps_clicked(self):
        if self._soft_deps_install_in_progress:
            return
        self._soft_deps_install_in_progress = True
        self.soft_deps_install_btn.set_sensitive(False)
        self.soft_deps_banner_label.set_text(self._("soft_deps_installing_text"))
        thread = threading.Thread(target=self._install_missing_deps_thread, daemon=True)
        thread.start()

    def _install_missing_deps_thread(self):
        from plunderhub.core.dependency_check import detect_pkg_manager, run_install
        try:
            pkg_manager, _distro_name = detect_pkg_manager()
            ok, output = run_install(pkg_manager, self._missing_soft_deps)
        except Exception as e:
            # Both of the above are documented as "never raises" and
            # already catch everything internally - this is a cheap
            # extra net rather than a fix for a known gap, matching
            # the same reasoning as the update-check thread just
            # above: _soft_deps_install_in_progress is only ever reset
            # inside _on_install_missing_deps_finished, so a slipped-
            # through exception here would permanently disable the
            # Install button for the rest of the session.
            logger.warning(f"Soft-dependency install failed unexpectedly: {e}")
            ok, output = False, str(e)
        GLib.idle_add(self._on_install_missing_deps_finished, ok, output)

    def _on_install_missing_deps_finished(self, ok: bool, output: str) -> bool:
        from plunderhub.core.dependency_check import SOFT_DEPS, missing_dependencies
        self._soft_deps_install_in_progress = False
        self.soft_deps_install_btn.set_sensitive(True)
        # Re-checking here confirms the packages actually landed, but
        # cairo/libsecret support inside THIS already-running process
        # was only ever attempted once, at startup (covers_mixin.py's
        # module-level `import cairo`, secret_store.py's cached _Secret
        # lookup) - a fresh install doesn't retroactively un-cache
        # that, so a restart is always the honest answer even on
        # success, not an optional suggestion.
        still_missing = missing_dependencies(SOFT_DEPS)
        # Kept in sync with what's actually still missing (not just
        # left at its startup value) - a partial success (only some of
        # several missing packages actually installed, e.g. one
        # package failed to download) must neither be reported as
        # full success nor have a retry silently re-attempt packages
        # that already installed fine the first time.
        self._missing_soft_deps = still_missing
        if not still_missing:
            self._show_info_dialog(self._("soft_deps_installed_text"))
            self._set_bar_visible(self.soft_deps_banner_bar, False)
        else:
            self._update_soft_deps_banner_text()
            self._show_info_dialog(self._("soft_deps_install_failed_text"))
        return False

    def _on_update_banner_view_clicked(self):
        # Used to open the GitHub release page directly; now routes
        # through the Updates tab instead, since that's where the
        # actual install choice (Manual/Auto) and version info live -
        # opening the release page itself is still one click away
        # from there via the install-choice dialog's Manual button.
        self.show_options(initial_tab="updates")

    def _dismiss_update_banner(self):
        # remembers the SPECIFIC version dismissed, not just "dismissed" -
        # so the banner stays away for this release, but comes right back
        # the moment a newer one shows up
        if self._available_update_tag:
            self.settings["dismissed_update_version"] = self._available_update_tag
            save_settings(self.settings)
        self._set_bar_visible(self.update_banner_bar, False)
        self._set_update_card(None)

    def _show_update_banner(self, tag: str):
        # The card at the bottom of the sidebar (HomeMixin) rather than
        # the old strip above the library.
        self._set_update_card(tag)

    def _maybe_check_for_updates(self, force: bool = False):
        """Kicks off a background GitHub API check.

        force=True (the manual "Check for updates" button) ignores the
        update_check_enabled toggle entirely - an explicit click
        should always do something, even with automatic checks turned
        off.

        Otherwise this is the automatic check - called only once, from
        the app's own startup reload() (see is_startup there), never
        from a Resupply click or any other reload() during the
        session. It simply respects the toggle, with no time-based
        throttle at all: every fresh launch of the app means a fresh
        check, if enabled - not "once a day" regardless of how many
        times you've actually started the app since."""
        if self._update_check_in_progress:
            return
        if not force and not self.update_check_enabled:
            return
        self._update_check_in_progress = True
        thread = threading.Thread(target=self._update_check_thread, daemon=True)
        thread.start()

    def _update_check_thread(self):
        try:
            tag = fetch_latest_release_tag()
        except Exception as e:
            # fetch_latest_release_tag()/http_get_json() are already
            # careful (retries, catches everything, returns None on
            # failure), so this is a cheap extra net rather than a fix
            # for a known failure - but self._update_check_in_progress
            # is only ever reset inside _on_update_check_finished
            # below, so without it, any future change that let an
            # exception slip through here would silently make the
            # "Check now" button/automatic check stop working for the
            # rest of the session.
            logger.warning(f"Update check failed unexpectedly: {e}")
            tag = None
        GLib.idle_add(self._on_update_check_finished, tag)

    def _on_update_check_finished(self, tag: Optional[str]) -> bool:
        self._update_check_in_progress = False
        # tag is None specifically means fetch_latest_release_tag()
        # couldn't complete the request at all (no connection, GitHub
        # unreachable, repo/releases missing, etc) - that's a distinct
        # outcome from "checked fine, and it turns out there's nothing
        # newer than what's installed", which also leaves tag unused
        # by is_newer_version() below but is a real, successful check.
        # Conflating the two used to let a failed check silently claim
        # "You're on the latest version." - only bump the "last
        # successful check" timestamp when a check actually succeeded.
        self._last_update_check_failed = tag is None
        if tag is not None:
            self.settings["last_update_check_ts"] = time.time()
            save_settings(self.settings)
        if tag and is_newer_version(tag, APP_VERSION):
            self._available_update_tag = tag
            dismissed = self.settings.get("dismissed_update_version", "")
            if tag != dismissed:
                self._show_update_banner(tag)
        else:
            self._available_update_tag = None
        self._refresh_update_check_status_label()
        self._refresh_update_check_button()
        return False

    def _refresh_update_check_status_label(self):
        # only set while the About tab is actually open/built - a
        # background check finishing after it's closed has nothing to
        # update, and set_text on an already-destroyed widget is best
        # avoided rather than relied on being a harmless no-op
        if not hasattr(self, "update_check_status_label"):
            return
        try:
            if self._available_update_tag:
                text = self._("update_check_available_status_text").format(version=self._available_update_tag)
            elif self._last_update_check_failed:
                text = self._("update_check_error_text")
            elif self.settings.get("last_update_check_ts", 0) > 0:
                text = self._("update_check_up_to_date_text")
            else:
                text = ""  # never checked yet - stay quiet rather than claim "up to date"
            self.update_check_status_label.set_text(text)
        except Exception:
            pass

    def _refresh_update_check_button(self):
        # Swaps the button between its two roles in place, rather than
        # keeping two separate buttons or reconnecting its "clicked"
        # signal on every refresh (which would stack up duplicate
        # handlers) - _on_check_now_or_install_clicked below reads
        # _available_update_tag itself to decide what a click should
        # actually do, so only the label needs to change here.
        if not hasattr(self, "update_check_now_btn"):
            return
        try:
            label = self._("update_install_btn") if self._available_update_tag \
                else self._("update_check_now_btn")
            self.update_check_now_btn.set_label(label)
        except Exception:
            pass

    def _on_check_now_or_install_clicked(self):
        if self._available_update_tag:
            self._show_update_install_choice_dialog()
        else:
            self._on_check_now_clicked()

    def _show_update_install_choice_dialog(self):
        """How to install a new release, as one of the app's card
        dialogs: Automatic and Manual as two big choices (Automatic
        marked recommended, or greyed out with the reason), Cancel below."""
        tag = self._available_update_tag
        if not tag:
            return
        dlg = Gtk.Dialog(transient_for=self._dialog_transient_parent(), modal=True)
        dlg.set_title(self._("update_choose_method_title"))
        dlg.set_decorated(False)
        dlg.set_resizable(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.get_style_context().add_class("home-launch-dialog")
        action_area = dlg.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()
        area = dlg.get_content_area()
        area.set_border_width(0)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        column.set_border_width(24)
        title = Gtk.Label(label=self._("update_choose_method_title"), xalign=0)
        title.get_style_context().add_class("home-dialog-title")
        column.pack_start(title, False, False, 0)
        current = f"v{APP_VERSION}"
        new = tag if tag.startswith("v") else f"v{tag}"
        versions = Gtk.Label(label=f"{current}  →  {new}", xalign=0)
        versions.get_style_context().add_class("home-update-versions")
        column.pack_start(versions, False, False, 0)

        choice = []

        def pick(which: str):
            choice.append(which)
            dlg.response(Gtk.ResponseType.OK)

        method, unavailable_reason = self._auto_update_availability()

        def option(icon_name: str, label: str, desc: str, which: str, recommended: bool = False,
                   disabled_reason: Optional[str] = None) -> Gtk.Widget:
            btn = Gtk.Button()
            btn.get_style_context().add_class("install-choice")
            row = Gtk.Box(spacing=14)
            row.pack_start(self._icon_image_at_size(icon_name, 22), False, False, 0)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            head = Gtk.Box(spacing=8)
            name = Gtk.Label(label=label, xalign=0)
            name.get_style_context().add_class("home-update-title")
            head.pack_start(name, False, False, 0)
            if recommended:
                pill = Gtk.Label(label=self._("task_recommended"))
                pill.get_style_context().add_class("settings-pill")
                pill.set_valign(Gtk.Align.CENTER)
                head.pack_start(pill, False, False, 0)
            texts.pack_start(head, False, False, 0)
            sub = Gtk.Label(label=disabled_reason or desc, xalign=0)
            sub.set_line_wrap(True)
            sub.set_max_width_chars(44)
            sub.get_style_context().add_class("dim-label")
            texts.pack_start(sub, False, False, 0)
            row.pack_start(texts, True, True, 0)
            btn.add(row)
            btn.connect("clicked", lambda *_: pick(which))
            if disabled_reason is not None:
                btn.set_sensitive(False)
            return btn

        auto = option("system-software-install-symbolic", self._("update_auto_btn"), self._("update_auto_desc"),
                      "auto", recommended=unavailable_reason is None, disabled_reason=unavailable_reason)
        auto.set_margin_top(12)
        column.pack_start(auto, False, False, 0)
        column.pack_start(option("web-browser-symbolic", self._("update_manual_btn"), self._("update_manual_desc"),
                                 "manual"), False, False, 0)

        cancel = self._dialog_button(self._("dialog_cancel"), "ghost", "back", "Esc")
        cancel.set_halign(Gtk.Align.END)
        cancel.set_margin_top(10)
        cancel.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CANCEL))
        column.pack_start(cancel, False, False, 0)
        area.pack_start(column, True, True, 0)

        def on_key(_w, event) -> bool:
            if event.keyval == Gdk.KEY_Escape:
                dlg.response(Gtk.ResponseType.CANCEL)
                return True
            return False
        dlg.connect("key-press-event", on_key)
        dlg.plunderhub_nav = lambda action: action == "back" and dlg.response(Gtk.ResponseType.CANCEL)

        dlg.show_all()
        dlg.set_focus(None)
        self._enter_dimmed_dialog()
        self._install_controller_focus_rings(dlg)
        self._enable_keyboard_focus_ring_detection(dlg)
        try:
            dlg.run()
        finally:
            self._exit_dimmed_dialog()
            dlg.destroy()
        if choice == ["manual"]:
            webbrowser.open(release_page_url(tag))
        elif choice == ["auto"]:
            # Started only once the choice is gone, so the progress
            # window isn't stacked on top of it.
            self._start_auto_update(tag, method)

    def _on_check_now_clicked(self):
        if hasattr(self, "update_check_status_label"):
            try:
                self.update_check_status_label.set_text(self._("update_check_checking_text"))
            except Exception:
                pass
        self._maybe_check_for_updates(force=True)

    def _update_bulk_bar_visibility(self):
        if not hasattr(self, "bulk_action_bar"):
            return
        self._set_bar_visible(self.bulk_action_bar, self.selection_mode)
        # The undo toast ("N hidden - Undo") sits above the floating bar.
        self.undo_toast.set_margin_bottom(96 if self.selection_mode else 20)
        if self.selection_mode:
            self._update_bulk_bar()

    def _update_bulk_bar(self):
        if not hasattr(self, "bulk_count_label"):
            return
        count = len(self.selected_uids)
        self.bulk_count_label.set_text(self._("bulk_selected_count").format(count=count))
        sensitive = count > 0
        for btn in (self.bulk_fav_btn, self.bulk_unfav_btn, self.bulk_hide_btn, self.bulk_clear_sel_btn):
            btn.set_sensitive(sensitive)

    def _grab_focus_on_hover(self, widget: Gtk.Widget):
        widget.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK)
        widget.connect("enter-notify-event", lambda w, _e: self._suppress_focus_ring_except(w) or False)

    def _suppress_focus_ring_except(self, hovered: Gtk.Widget):
        focused = self.get_focus()
        if focused is None or focused is hovered:
            return
        if focused.get_style_context().has_class("game-cover-selected"):
            return
        focused.get_style_context().add_class("game-cover-focus-suppressed")

    def _grab_keyboard_focus(self, widget: Gtk.Widget):
        widget.grab_focus()
        widget.get_style_context().remove_class("game-cover-focus-suppressed")

    def _direct_child_of(self, widget: Optional[Gtk.Widget], container: Gtk.Widget) -> Optional[Gtk.Widget]:
        w = widget
        while w is not None and w.get_parent() is not container:
            w = w.get_parent()
        return w

    def _get_active_dialog(self) -> Optional[Gtk.Window]:
        try:
            toplevels = Gtk.Window.list_toplevels()
        except Exception:
            return None
        for win in toplevels:
            if win is self:
                continue
            try:
                if win.get_visible() and win.is_active():
                    return win
            except Exception:
                continue
        return None

    def _find_combo_ancestor(self, widget: Optional[Gtk.Widget]) -> Optional[Gtk.ComboBox]:
        combo = widget
        while combo is not None and not isinstance(combo, Gtk.ComboBox):
            combo = combo.get_parent()
        return combo

    def _find_descendant_notebook(self, widget: Gtk.Widget) -> Optional[Gtk.Notebook]:
        if isinstance(widget, Gtk.Notebook):
            return widget
        if hasattr(widget, "get_children"):
            for child in widget.get_children():
                found = self._find_descendant_notebook(child)
                if found is not None:
                    return found
        return None

    def _enable_keyboard_focus_ring_detection(self, dialog: Gtk.Window):
        dialog.connect("key-press-event", self._on_dialog_key_press_for_focus_ring)
        dialog.connect("button-press-event", self._on_dialog_button_press_for_focus_ring)
        dialog.connect("key-press-event", self._on_dialog_key_press_for_navigation)
        dialog.connect("key-release-event", self._on_dialog_key_release_for_navigation)

    def _on_dialog_key_press_for_focus_ring(self, _widget, event) -> bool:
        nav_keys = (
            Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Left, Gdk.KEY_Right,
            Gdk.KEY_KP_Up, Gdk.KEY_KP_Down, Gdk.KEY_KP_Left, Gdk.KEY_KP_Right,
            Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab,
        )
        if event.keyval in nav_keys:
            self._keyboard_nav_active = True
        return False

    def _on_dialog_button_press_for_focus_ring(self, _widget, _event) -> bool:
        self._keyboard_nav_active = False
        return False

    _SPIN_HOLD_ARROW_KEYS = (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_KP_Left, Gdk.KEY_KP_Right)

    def _on_dialog_key_press_for_navigation(self, dialog: Gtk.Window, event) -> bool:
        if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
            return False
        focus_widget = dialog.get_focus()
        # SpinButton is an Entry subclass, but Left/Right should spin the
        # value here (see _start_spin_hold), not move the text cursor
        if isinstance(focus_widget, Gtk.SpinButton) and event.keyval in self._SPIN_HOLD_ARROW_KEYS:
            step = focus_widget.get_adjustment().get_step_increment() or 1
            delta = step if event.keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right) else -step
            self._start_spin_hold(focus_widget, delta)
            return True
        if isinstance(focus_widget, (Gtk.Entry, Gtk.TextView)):
            return False
        if event.keyval in self.ARROW_KEY_DIRECTIONS:
            self._handle_dialog_joystick_event(dialog, self.ARROW_KEY_DIRECTIONS[event.keyval])
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self._find_combo_ancestor(focus_widget) is not None:
            self._handle_dialog_joystick_event(dialog, "activate")
            return True
        return False

    def _on_dialog_key_release_for_navigation(self, dialog: Gtk.Window, event) -> bool:
        if event.keyval in self._SPIN_HOLD_ARROW_KEYS:
            self._stop_spin_hold()
        return False

    def _install_controller_focus_rings(self, root: Gtk.Widget):
        if isinstance(root, Gtk.Window) and not getattr(root, "_plunderhub_combo_ring_wired", False):
            root._plunderhub_combo_ring_wired = True
            root.connect("set-focus", self._on_dialog_set_focus_for_combo_ring)
        if hasattr(root, "get_can_focus") and root.get_can_focus():
            if type(root) is Gtk.Button:
                reserve_space = getattr(root, "_plunderhub_reserve_marker_space", False)
                marker_halign = getattr(root, "_plunderhub_marker_halign", Gtk.Align.CENTER)
                self._prepare_button_focus_marker(root, reserve_marker_space=reserve_space, halign=marker_halign)
            root.connect("notify::has-focus", self._on_focus_ring_widget_notify)
        if hasattr(root, "get_children"):
            for child in root.get_children():
                self._install_controller_focus_rings(child)

    def _prepare_button_focus_marker(self, button: Gtk.Button, reserve_marker_space: bool = False,
                                      halign: Gtk.Align = Gtk.Align.CENTER):
        label_text = button.get_label()
        icon = button.get_image()
        if label_text is None and icon is None:
            return
        if icon is not None and icon.get_parent() is not None:
            icon.get_parent().remove(icon)
        old_child = button.get_child()
        if old_child is not None:
            button.remove(old_child)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_halign(halign)
        arrow_label = Gtk.Label(label="▶")
        if reserve_marker_space:
            arrow_label.set_opacity(0)
        else:
            arrow_label.set_no_show_all(True)
            arrow_label.set_visible(False)
        box.pack_start(arrow_label, False, False, 0)
        if icon is not None:
            box.pack_start(icon, False, False, 0)
        if label_text is not None:
            text_label = Gtk.Label(label=label_text)
            box.pack_start(text_label, False, False, 0)
        button.add(box)
        # The box is shown on its own as well: a button with no_show_all
        # (kept hidden until needed, like the update window's) ignores
        # show_all(), and would later appear without its label.
        box.show_all()
        button.show_all()
        button._plunderhub_focus_arrow = arrow_label
        button._plunderhub_focus_arrow_reserve_space = reserve_marker_space

    def _on_focus_ring_widget_notify(self, widget: Gtk.Widget, _pspec):
        if not (self._joystick_manager.is_connected() or self._keyboard_nav_active):
            widget.get_style_context().remove_class("controller-focus-ring")
            if widget.get_name() == "controller-focus-ring-active":
                widget.set_name("")
            self._set_focus_marker_arrow_shown(widget, False)
            return
        if widget.has_focus():
            widget.get_style_context().add_class("controller-focus-ring")
            widget.set_name("controller-focus-ring-active")
            self._set_focus_marker_arrow_shown(widget, True)
        else:
            widget.get_style_context().remove_class("controller-focus-ring")
            widget.set_name("")
            self._set_focus_marker_arrow_shown(widget, False)

    def _set_focus_marker_arrow_shown(self, widget: Gtk.Widget, shown: bool):
        arrow = getattr(widget, "_plunderhub_focus_arrow", None)
        if arrow is None:
            return
        if getattr(widget, "_plunderhub_focus_arrow_reserve_space", False):
            arrow.set_opacity(1.0 if shown else 0.0)
        else:
            arrow.set_visible(shown)

    def _on_dialog_set_focus_for_combo_ring(self, _dialog: Gtk.Window, widget: Optional[Gtk.Widget]):
        combo = self._find_combo_ancestor(widget)
        prev = self._ringed_combo
        if prev is not None and prev is not combo:
            prev.get_style_context().remove_class("controller-focus-ring")
            if prev.get_name() == "controller-focus-ring-active":
                prev.set_name("")
        if combo is not None:
            if self._joystick_manager.is_connected() or self._keyboard_nav_active:
                combo.get_style_context().add_class("controller-focus-ring")
                combo.set_name("controller-focus-ring-active")
        self._ringed_combo = combo

    def _find_descendant_scrolled_window(self, widget: Gtk.Widget) -> Optional[Gtk.ScrolledWindow]:
        if isinstance(widget, Gtk.ScrolledWindow):
            return widget
        if hasattr(widget, "get_children"):
            for child in widget.get_children():
                found = self._find_descendant_scrolled_window(child)
                if found is not None:
                    return found
        return None

    def _scroll_active_notebook_page(self, dialog: Gtk.Window, delta: float):
        notebook = self._find_descendant_notebook(dialog)
        if notebook is not None:
            page = notebook.get_nth_page(notebook.get_current_page())
            if page is None:
                return
            scroller = page if isinstance(page, Gtk.ScrolledWindow) else self._find_descendant_scrolled_window(page)
        else:
            scroller = self._find_descendant_scrolled_window(dialog)
        if scroller is None:
            return
        adj = scroller.get_vadjustment()
        if adj is None:
            return
        lower = adj.get_lower()
        upper = adj.get_upper() - adj.get_page_size()
        adj.set_value(max(lower, min(upper, adj.get_value() + delta)))

    _SPIN_HOLD_START_MS = 350
    _SPIN_HOLD_FAST_MS = 60
    _SPIN_HOLD_ACCELERATE_AFTER_S = 2.0

    def _spin_button_step(self, widget: Gtk.SpinButton, delta: float):
        adj = widget.get_adjustment()
        adj.set_value(max(adj.get_lower(), min(adj.get_upper(), adj.get_value() + delta)))

    def _start_spin_hold(self, widget: Gtk.SpinButton, delta: float, focus_widget: Optional[Gtk.Widget] = None):
        """Keyboard/controller left-right on a focused SpinButton (e.g.
        the Custom fps spinner under Animated Covers) - normally a
        SpinButton only reacts to Up/Down, and Left/Right just moves the
        text cursor, same as a plain Entry. Steps once immediately, then
        keeps stepping on its own timer - starting slow, speeding up
        after a few seconds - independent of how fast the underlying
        input actually repeats (OS key-repeat and the joystick's own
        poll-repeat run at different, fixed rates neither one matching
        what "hold to spin, then speed up" needs). Repeated presses
        while already holding the same widget/direction are absorbed as
        no-ops - this timer is what's actually driving the stepping.

        focus_widget is what's actually FOCUSED right now - normally
        the spinbutton itself, but for a paired radio (see _radio_
        paired_spin) it's the radio, not the spinbutton being adjusted.
        _on_spin_hold_tick checks THIS widget's focus to decide whether
        to keep going, not the spinbutton's own - checking the
        spinbutton's own focus unconditionally would kill the hold on
        its very first tick whenever it's driven from a paired radio,
        since the spinbutton itself never actually has focus in that
        case (that was the reported bug: single steps from repeat
        events, but the hold's own timer - and so the acceleration -
        never got a chance to run)."""
        focus_widget = focus_widget or widget
        if (self._spin_hold is not None and self._spin_hold["widget"] is widget
                and self._spin_hold["delta"] == delta):
            return
        self._stop_spin_hold()
        self._spin_button_step(widget, delta)
        self._spin_hold = {
            "widget": widget, "delta": delta, "focus_widget": focus_widget,
            "started_at": time.monotonic(), "timeout_id": None,
        }
        self._spin_hold["timeout_id"] = GLib.timeout_add(self._SPIN_HOLD_START_MS, self._on_spin_hold_tick)

    def _on_spin_hold_tick(self) -> bool:
        state = self._spin_hold
        if state is None:
            return False
        if not state["focus_widget"].has_focus():
            self._spin_hold = None
            return False
        self._spin_button_step(state["widget"], state["delta"])
        elapsed = time.monotonic() - state["started_at"]
        next_delay = (self._SPIN_HOLD_FAST_MS if elapsed >= self._SPIN_HOLD_ACCELERATE_AFTER_S
                      else self._SPIN_HOLD_START_MS)
        state["timeout_id"] = GLib.timeout_add(next_delay, self._on_spin_hold_tick)
        return False

    def _stop_spin_hold(self):
        state = self._spin_hold
        if state is not None and state["timeout_id"] is not None:
            GLib.source_remove(state["timeout_id"])
        self._spin_hold = None

    def _handle_dialog_joystick_event(self, dialog: Gtk.Window, action: str):
        # Dialogs with their own navigation model (Settings) take it all.
        own_navigation = getattr(dialog, "plunderhub_nav", None)
        if own_navigation is not None:
            own_navigation(action)
            return
        self._dialog_nav_default(dialog, action)

    def _dialog_nav_default(self, dialog: Gtk.Window, action: str):
        """A dialog's controller / arrow-key navigation: focus moves,
        spin buttons and sliders change, combos open their picker - what
        a dialog of its own (the game page) falls back to."""
        from plunderhub.ui import keynav
        if action in ("up", "down", "left", "right") and keynav.restore(dialog):
            return
        if action == "release":
            self._stop_spin_hold()
            return
        if action in ("prev_tab", "next_tab"):
            notebook = self._find_descendant_notebook(dialog)
            if notebook is not None and notebook.get_n_pages() > 1:
                n_pages = notebook.get_n_pages()
                current = notebook.get_current_page()
                step = 1 if action == "next_tab" else -1
                notebook.set_current_page((current + step) % n_pages)
            return
        focused = dialog.get_focus()
        combo = self._find_combo_ancestor(focused)
        if action == "down" and focused is None:
            notebook = self._find_descendant_notebook(dialog)
            if notebook is not None:
                page = notebook.get_nth_page(notebook.get_current_page())
                if page is not None and page.child_focus(Gtk.DirectionType.DOWN):
                    return
        if action in ("up", "down") and focused is not None:
            for group in self._manual_radio_groups:
                buttons = group["buttons"]
                if focused in buttons:
                    step = 1 if action == "down" else -1
                    next_index = buttons.index(focused) + step
                    if 0 <= next_index < len(buttons):
                        buttons[next_index].grab_focus()
                    else:
                        target = group["after" if action == "down" else "before"]
                        if target is not None:
                            target.grab_focus()
                    return
        if action in ("up", "down") and combo is not None and combo in self._manual_combo_stops:
            target = self._manual_combo_stops[combo]["after" if action == "down" else "before"]
            if target is not None:
                target.grab_focus()
            return
        if action in ("left", "right") and isinstance(focused, Gtk.Range):
            adj = focused.get_adjustment()
            step = adj.get_step_increment() or 1
            delta = step if action == "right" else -step
            adj.set_value(max(adj.get_lower(), min(adj.get_upper(), adj.get_value() + delta)))
            return
        if action in ("left", "right") and isinstance(focused, Gtk.SpinButton):
            step = focused.get_adjustment().get_step_increment() or 1
            delta = step if action == "right" else -step
            self._start_spin_hold(focused, delta)
            return
        if action in ("left", "right") and focused in self._radio_paired_spin:
            # e.g. the "Custom" radio next to the fps spinner - Left/Right
            # should spin the paired value without first needing a separate
            # navigation step onto the spinbutton itself
            spin = self._radio_paired_spin[focused]
            step = spin.get_adjustment().get_step_increment() or 1
            delta = step if action == "right" else -step
            self._start_spin_hold(spin, delta, focus_widget=focused)
            return
        mapping = {
            "up": Gtk.DirectionType.UP,
            "down": Gtk.DirectionType.DOWN,
        }
        if action in mapping:
            from plunderhub.ui import keynav
            if not keynav.escape_list(dialog, action):
                dialog.child_focus(mapping[action])
            return
        if action in ("left", "right"):
            # In a dialog with tabs (Settings' own notebook, primarily),
            # Left/Right switches tabs instead of falling through to
            # plain child_focus() - GtkRadioButton's own built-in
            # move-focus handling auto-selects (activates) whichever
            # radio button focus lands on next inside its group, so
            # navigating with the arrow keys was silently toggling
            # options instead of doing anything resembling tab
            # switching (reported directly, with screenshots). A combo
            # box (e.g. Language) focused here no longer changes its
            # own value on Left/Right either, for the same reason -
            # that used to happen via a dedicated branch that ran
            # before this one and returned early, so a focused combo
            # never even reached tab-switching at all (reported
            # directly: Left/Right on Language just cycled languages
            # instead of moving between tabs). Enter/Space (the
            # "activate" action) still opens that combo's own picker
            # dialog, which has its own Up/Down-based selection - this
            # is the only way to change a combo's value with the
            # keyboard/controller now.
            # Dialogs with no notebook at all (e.g. a plain Yes/No
            # confirmation) still fall back to child_focus() below, so
            # Left/Right between horizontally-laid-out buttons there
            # keeps working exactly as before.
            notebook = self._find_descendant_notebook(dialog)
            if notebook is not None and notebook.get_n_pages() > 1:
                n_pages = notebook.get_n_pages()
                current = notebook.get_current_page()
                step = 1 if action == "right" else -1
                notebook.set_current_page((current + step) % n_pages)
                return
            dialog.child_focus(Gtk.DirectionType.RIGHT if action == "right" else Gtk.DirectionType.LEFT)
            return
        if action == "activate":
            if combo is not None:
                self._open_combo_picker(combo, dialog)
            elif focused is not None:
                focused.activate()
        elif action == "back":
            if isinstance(dialog, Gtk.Dialog):
                dialog.response(Gtk.ResponseType.CANCEL)
            else:
                dialog.close()

    def _compact_picker_label(self, text: str) -> str:
        return text.split(" (", 1)[0]

    def _combo_title(self, combo: Gtk.ComboBox) -> str:
        """The picker's heading: as registered - else the name of the
        setting the combo sits in (its row's first label)."""
        title = self._combo_picker_titles.get(combo)
        if title:
            return title
        row = combo.get_parent()
        while row is not None and not any(row.get_style_context().has_class(c)
                                          for c in ("gp-setting", "settings-row")):
            row = row.get_parent()
        found = []

        def first_label(w):
            if found:
                return
            if isinstance(w, Gtk.Label) and w.get_text() and not w.is_ancestor(combo):
                found.append(w.get_text())
            elif isinstance(w, Gtk.Container) and w is not combo:
                for child in w.get_children():
                    first_label(child)
        if row is not None:
            first_label(row)
        return found[0] if found else ""

    def _open_combo_picker(self, combo: Gtk.ComboBoxText, transient_for: Gtk.Window):
        """A combo's options as a card of its own, for the keyboard and a
        controller: a row per option, the current one ticked, the one
        stood on lit like a Ship's Controls row; Enter / A picks, Esc / B
        or Close leave it as it was."""
        model = combo.get_model()
        items = [row[0] for row in model] if model is not None else []
        if not items:
            return
        current = combo.get_active()
        dlg = Gtk.Dialog(transient_for=transient_for, modal=True)
        dlg.set_decorated(False)
        dlg.set_resizable(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.get_style_context().add_class("home-launch-dialog")
        action_area = dlg.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()
        area = dlg.get_content_area()
        area.set_border_width(0)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        column.set_border_width(22)
        column.set_size_request(380, -1)
        title = self._combo_title(combo)
        if title:
            heading = Gtk.Label(label=title, xalign=0)
            heading.get_style_context().add_class("home-dialog-title")
            column.pack_start(heading, False, False, 0)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.get_style_context().add_class("combo-picker-list")
        for idx, text in enumerate(items):
            row = Gtk.ListBoxRow()
            row.picker_index = idx
            line = Gtk.Box(spacing=12)
            label = Gtk.Label(label=self._compact_picker_label(text), xalign=0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            line.pack_start(label, True, True, 0)
            if idx == current:
                row.get_style_context().add_class("combo-picker-current")
                tick = Gtk.DrawingArea()
                tick.set_has_window(False)
                tick.set_size_request(18, 18)
                tick.set_valign(Gtk.Align.CENTER)

                def draw_tick(widget, cr):
                    from plunderhub.ui.home_mixin import _accent_rgb
                    cr.set_source_rgba(*_accent_rgb(widget), 1)
                    cr.set_line_width(2.4)
                    cr.set_line_cap(1)
                    cr.set_line_join(1)
                    cr.move_to(3.5, 9.5)
                    cr.line_to(7.5, 13.5)
                    cr.line_to(14.5, 5)
                    cr.stroke()
                    return False
                tick.connect("draw", draw_tick)
                line.pack_end(tick, False, False, 0)
            row.add(line)
            listbox.add(row)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(380)
        scroller.get_style_context().add_class("settings-scroll")
        scroller.add(listbox)
        listbox.set_focus_vadjustment(scroller.get_vadjustment())
        column.pack_start(scroller, True, True, 0)

        def pick(_list, row):
            combo.set_active(row.picker_index)
            dlg.response(Gtk.ResponseType.OK)
        listbox.connect("row-activated", pick)

        buttons = Gtk.Box()
        buttons.set_margin_top(6)
        close = self._dialog_button(self._("close_btn"), "ghost", "back", "Esc")
        close.set_focus_on_click(False)
        close.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CANCEL))
        buttons.pack_end(close, False, False, 0)
        column.pack_start(buttons, False, False, 0)
        area.pack_start(column, True, True, 0)

        def on_key(_w, event) -> bool:
            if event.keyval == Gdk.KEY_Escape:
                dlg.response(Gtk.ResponseType.CANCEL)
                return True
            return False
        dlg.connect("key-press-event", on_key)

        def nav(action: str):
            focus = dlg.get_focus()
            if action == "back":
                dlg.response(Gtk.ResponseType.CANCEL)
            elif action == "activate":
                if focus is not None:
                    focus.activate()
            elif action in ("up", "down"):
                from plunderhub.ui import keynav
                if focus is close and action == "up":
                    rows = listbox.get_children()
                    if rows:
                        rows[-1].grab_focus()
                elif not keynav.escape_list(dlg, action):
                    dlg.child_focus(Gtk.DirectionType.UP if action == "up" else Gtk.DirectionType.DOWN)
        dlg.plunderhub_nav = nav
        from plunderhub.ui import keynav
        keynav.track(dlg)
        dlg.show_all()
        rows = listbox.get_children()
        if rows:
            rows[current if 0 <= current < len(rows) else 0].grab_focus()
        keynav.on(dlg)
        dlg.run()
        dlg.destroy()

    def _update_combo_picker_scroll_hint(self, vadj: Gtk.Adjustment, hint: Gtk.Widget):
        more_below = (vadj.get_value() + vadj.get_page_size()) < (vadj.get_upper() - 1)
        hint.set_visible(more_below)

    def _on_combo_picker_option_clicked(self, _btn, dlg: Gtk.Dialog, combo: Gtk.ComboBoxText, index: int):
        combo.set_active(index)
        dlg.response(Gtk.ResponseType.OK)


    def _on_joystick_family_changed(self, family: str) -> bool:
        if family != self._controller_family:
            self._controller_family = family
            for glyph, role in self._console_family_glyphs:
                glyph.set_text(self._controller_glyph_text(role))
            self.console_view.queue_draw()
        return False

    def _scroll_main_view(self, delta: float):
        if self.console_mode_active:
            return      # Console Mode's shelves move with the d-pad / stick
        scroller = self.scroller
        adj = scroller.get_vadjustment()
        if adj is None:
            return
        lower = adj.get_lower()
        upper = adj.get_upper() - adj.get_page_size()
        adj.set_value(max(lower, min(upper, adj.get_value() + delta)))

    # the right stick pushed all the way scrolls this many px a second;
    # less far, less (by the push to the power of _PAD_SCROLL_CURVE)
    _PAD_SCROLL_SPEED = 2600
    _PAD_SCROLL_CURVE = 1.7

    def _pad_scroll_target(self) -> Optional[Gtk.Window]:
        """The window the right stick scrolls: the dialog in front, else the
        main window - None while none of PlunderHub's has the focus."""
        dialog = self._get_active_dialog()
        if dialog is not None:
            return dialog
        if self.is_active() and not self.console_mode_active:
            return self
        return None

    def _pad_scroll(self, push: float):
        """The right stick moved: the page glides by how far it's pushed,
        a step every frame of the common beat (the old 40 px jumps 20 times
        a second stuttered)."""
        self._pad_scroll_push = push
        target = self._pad_scroll_target() if push else None
        tick = getattr(self, "_pad_scroll_tick", None)
        if tick is not None and tick[0] is not target:
            tick[0].remove_tick_callback(tick[1])
            tick = self._pad_scroll_tick = None
        if target is not None and tick is None:
            self._pad_scroll_last_us = None
            self._pad_scroll_tick = (target, target.add_tick_callback(self._pad_scroll_step))

    def _pad_scroll_step(self, widget: Gtk.Widget, clock) -> bool:
        push = getattr(self, "_pad_scroll_push", 0.0)
        if not push or self._pad_scroll_target() is not widget:
            self._pad_scroll_tick = None
            return GLib.SOURCE_REMOVE
        if clock.get_frame_counter() % self._bubble_beat_frames(clock):
            return GLib.SOURCE_CONTINUE
        now = clock.get_frame_time()
        last, self._pad_scroll_last_us = self._pad_scroll_last_us, now
        if last is None:
            return GLib.SOURCE_CONTINUE
        dt = min(0.1, (now - last) / 1e6)
        delta = self._PAD_SCROLL_SPEED * abs(push) ** self._PAD_SCROLL_CURVE * dt * (1 if push > 0 else -1)
        if widget is self:
            self._scroll_main_view(delta)
        else:
            own = getattr(widget, "plunderhub_scroll", None)
            if own is not None:
                own(delta)
            else:
                self._scroll_active_notebook_page(widget, delta)
        return GLib.SOURCE_CONTINUE

    def _game_switch(self):
        """View + Start (Share + Options) held for a second: from the game
        PlunderHub started (the one started last) to PlunderHub - Console
        Mode or the main window, whichever was up - and back again."""
        now = time.monotonic()
        if now - getattr(self, "_game_switch_at", 0.0) < 1.5:
            return      # Steam's virtual pad and the real one under it
        running = set(self.running_procs) | set(self._heroic_launches)
        uid = max(running, key=lambda u: self.recent.get(u, 0), default=None)
        mover = self.workspace_mover()
        if uid is None or mover is None:
            logger.info("Controller switch: " + ("no game PlunderHub started is running" if uid is None
                                                 else "no window control on this desktop"))
            return
        self._game_switch_at = now
        match, launched_at = self._running_process_match.get(uid), self.recent.get(uid, 0.0)
        if self.is_active() or self._get_active_dialog() is not None:
            def done(ok: bool):
                if ok and self.console_mode_active:
                    # the pad's other buttons are the game's again
                    GLib.idle_add(lambda: setattr(self, "_console_hidden_for_game", True) and False)
            mover.to_game(uid, match, launched_at, done)
            return
        if not self.get_visible():
            self._restore_main_window()     # closed to the tray at the launch
        else:
            self.deiconify()
            self.present()
        if self.console_mode_active:
            self.console_view.toast(self._("game_switch_toast").format(
                combo=self._game_switch_combo()))
        mover.to_plunderhub(uid, match, launched_at)

    def _game_switch_combo(self) -> str:
        """The switch's buttons on the controller in use: View + Start or
        Share + Options."""
        return "Share + Options" if self._controller_family == "sony" else "View + Start"

    def _on_joystick_event(self, action: str, value) -> bool:
        if action == "game_switch":
            self._game_switch()
            return False
        if action == "scroll":
            # let go counts whatever is in front - a page never keeps gliding
            self._pad_scroll(value if self._pad_scroll_target() is not None else 0.0)
            return False
        # The pad is read straight from the system, whatever window has the
        # focus: it's PlunderHub's only while one of its windows has it -
        # not while you're in a game, a browser...
        active_dialog = self._get_active_dialog()
        if active_dialog is None and not self.is_active():
            return False
        if self._any_game_running() and self._console_hidden_for_game:
            return False
        if action == "toggle":
            # also over another of PlunderHub's windows: Console Mode comes
            # over it, and leaving it shows the window again
            self.present()
            self._toggle_console_mode()
            return False
        if active_dialog is not None:
            self._handle_dialog_joystick_event(active_dialog, action)
            return False
        if action == "settings":
            self.show_options()
            return False
        if not self.console_mode_active:
            menu = getattr(self, "_open_menu", None)
            if menu is not None and menu.get_visible():
                if action in ("up", "down", "left", "right", "activate", "back"):
                    menu.nav(action)
                return False
            if action in ("up", "down", "left", "right", "activate", "back") and self._reorder_input(action):
                return False
            if action == "back":
                if self.selection_mode:
                    self._exit_selection_mode()
            elif action == "activate":
                focused = self.get_focus()
                pick = getattr(focused, "plunderhub_pick", None)
                if pick is not None and not self.selection_mode:
                    # a sidebar launcher: its menu, Open first in it
                    pick()
                    self._menu_for_focus()
                elif (not self.selection_mode and focused is not None
                      and focused is not getattr(self, "_home_hero_play", None)
                      and self._focused_game() is not None):
                    # A on a cover or a list row: its menu, Set sail
                    # first in it - a pad has few buttons to spare
                    self._menu_for_focus()
                elif hasattr(focused, "kb_activate"):
                    focused.kb_activate()               # a Treasure Map tile: its card
                elif focused is not None:
                    focused.activate()
            elif action in ("up", "down", "left", "right"):
                self._main_move_focus(action)
            return False
        if action == "back":
            self._exit_console_mode()
        elif action == "activate":
            self._console_activate()
        elif action == "favorite":
            self._console_toggle_favorite()
        elif action == "prev_tab":
            self._console_shift_launcher_filter(-1)
        elif action == "next_tab":
            self._console_shift_launcher_filter(1)
        elif action == "sort":
            self._console_cycle_sort()
        elif action in ("up", "down", "left", "right"):
            self._console_move_focus(action)
        return False

    def _restore_main_window(self, by_user: bool = True):
        self._console_hidden_for_game = False
        if not by_user and self._quit_after_work:
            return      # quitting once the work still running is done
        self._console_take_fullscreen_back()
        if not self.api_key:
            # A still-empty key here can genuinely mean "never set
            # one", but it can just as easily be the boot-time keyring
            # race secret_store.load_api_key's own docstring already
            # describes, made more likely by "Run on startup,
            # minimized" starting the app extra early alongside a
            # pile of other autostart programs competing for the same
            # D-Bus/keyring daemon: the one-time startup retry there
            # (3 attempts, ~1.2s total) can still lose that race, and
            # since self.api_key is only ever set once at App()
            # construction, it then stayed empty for this whole run
            # even once the keyring became available seconds later -
            # reported directly ("works after actually restarting the
            # app, but not just reopening the window from the tray").
            # By the time someone manually reopens the window, the
            # keyring is essentially guaranteed to be ready by now, so
            # retrying here is nearly free (a genuinely-unset key just
            # returns empty again) and fixes exactly that case.
            refreshed_key = self._load_api_key_with_migration()
            if refreshed_key:
                self.api_key = refreshed_key
                privacy.add_secret(refreshed_key)
                self._update_cover_hint_bar_visibility()
        if by_user:
            # brought back: a quit waiting for the work to finish is off
            self._quit_after_work = False
        self.show()
        self.present()
        if self.console_mode_active:
            self.fullscreen()
            self._populate_console_mode()
            self._console_reset_cursor_hide_timer()

    def _enter_reorder_mode(self, launcher: Optional[str] = None):
        # Nothing to put in order - and with no games there'd be no Done
        # button either, the mode left on unseen till games turned up.
        if self.reorder_mode or not self.game_index:
            return
        # On the launcher asked for, or the one the sidebar is on (a
        # Heroic store: Heroic), its own games; anywhere else All
        # Plunder's own order, apart from the launchers' (Favorites keeps
        # to it, Recently sailed has none of its own).
        if launcher is None and self.home_nav.startswith("s:"):
            launcher = "Heroic"
        if launcher is not None:
            self.home_nav = f"l:{launcher}"
        elif self.home_nav not in ("all", "fav") and not self.home_nav.startswith("l:"):
            self.home_nav = "all"
        self.settings["home_nav"] = self.home_nav
        self.reorder_mode = True
        if self.sort_mode != "custom":
            self.sort_mode = "custom"
            self.settings["sort_mode"] = "custom"
            save_settings(self.settings)
        self._render_launchers()

    def _exit_reorder_mode(self):
        self.reorder_mode = False
        self._render_launchers()

    def _toggle_selection(self, uid: str):
        if uid in self.selected_uids:
            self.selected_uids.discard(uid)
        else:
            self.selected_uids.add(uid)
        self._update_selection_visuals(uid)
        self._update_bulk_bar()

    def _update_selection_visuals(self, uid: str):
        selected = uid in self.selected_uids
        for btn, check_icon in self.selection_widgets.get(uid, []):
            ctx = btn.get_style_context()
            if selected:
                ctx.add_class("game-cover-selected")
                ctx.remove_class("game-cover-focus-suppressed")
            else:
                ctx.remove_class("game-cover-selected")
            if check_icon is not None:
                check_icon.set_opacity(1.0 if selected else 0.35)
            btn.queue_draw()

    def _select_all_visible(self):
        for uid in self.selection_widgets.keys():
            if uid not in self.selected_uids:
                self.selected_uids.add(uid)
                self._update_selection_visuals(uid)
        self._update_bulk_bar()

    def _clear_selection(self):
        if not self.selected_uids:
            return
        uids = list(self.selected_uids)
        self.selected_uids.clear()
        for uid in uids:
            self._update_selection_visuals(uid)
        self._update_bulk_bar()

    def _bulk_set_favorite(self, make_favorite: bool):
        if not self.selected_uids:
            return
        if make_favorite:
            self.favorites.update(self.selected_uids)
        else:
            self.favorites.difference_update(self.selected_uids)
        save_favorites(self.favorites)
        self._render_launchers()

    def _bulk_hide(self):
        if not self.selected_uids:
            return
        hidden_pairs: List[Tuple[str, str]] = []
        for uid in list(self.selected_uids):
            game = self.game_index.get(uid)
            if game is None:
                continue
            self.scanner.hide(game.launcher, uid)
            hidden_pairs.append((game.launcher, uid))
        self.selected_uids.clear()
        self._render_launchers()
        if hidden_pairs:
            self._show_undo_toast(
                self._("bulk_hide_toast_text").format(count=len(hidden_pairs)),
                lambda pairs=hidden_pairs: self._undo_bulk_hide(pairs),
                icon_name="view-conceal-symbolic",
            )

    def _undo_bulk_hide(self, pairs: List[Tuple[str, str]]):
        for launcher, uid in pairs:
            self.scanner.unhide(launcher, uid)
        self._render_launchers()


    def _show_loading_dialog(self, message: str) -> Gtk.Dialog:
        dlg = Gtk.Dialog(transient_for=self._dialog_transient_parent())
        dlg.set_modal(True)
        dlg.set_deletable(False)
        dlg.set_default_size(280, 90)
        box = dlg.get_content_area()
        box.set_border_width(20)
        box.set_spacing(12)
        lbl = Gtk.Label(label=message)
        box.pack_start(lbl, False, False, 0)
        # No way to tell how long these take, so the bar bounces back and
        # forth instead of claiming a percentage.
        bar = Gtk.ProgressBar()
        bar.set_pulse_step(0.04)
        bar.get_style_context().add_class("accent-progress")
        box.pack_start(bar, False, False, 0)
        pulse_id = GLib.timeout_add(30, lambda: (bar.pulse(), True)[1])
        dlg.connect("destroy", lambda *_: GLib.source_remove(pulse_id))
        dlg.show_all()
        return dlg

    def _close_loading_dialog(self):
        dlg = self._loading_dialog
        if dlg is not None:
            dlg.destroy()
            self._loading_dialog = None


    def _on_icon_selected(self, img_data: Dict, launcher_name: str, dialog: Gtk.Dialog):
        thumb_path: Path = img_data["file_path"]
        full_url = img_data.get("url") or ""

        for old in self.icons_dir.glob(f"{launcher_name.lower()}.*"):
            old.unlink(missing_ok=True)

        fallback_ext = thumb_path.suffix or ".png"
        fallback_target = self.icons_dir / f"{launcher_name.lower()}{fallback_ext}"
        try:
            shutil.copyfile(str(thumb_path), str(fallback_target))
        except Exception as e:
            logger.debug(f"Couldn't save preview icon for {launcher_name!r}: {e}")
        self.reload()

        if full_url:
            ext = os.path.splitext(urllib.parse.urlparse(full_url).path)[1] or fallback_ext
            self._remember_icon_source(launcher_name.lower(), full_url, ext, img_data.get("animated", False))
            thread = threading.Thread(
                target=self._download_full_icon_async,
                args=(full_url, launcher_name, fallback_target),
                daemon=True,
            )
            thread.start()

    def _download_full_icon_async(self, url: str, launcher_name: str, fallback_target: Path):
        ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or fallback_target.suffix or ".png"
        target = self.icons_dir / f"{launcher_name.lower()}{ext}"
        try:
            ok = self.download_cover_from_url(url, target)
        except Exception:
            ok = False
        if ok:
            if target != fallback_target:
                fallback_target.unlink(missing_ok=True)
            GLib.idle_add(self.reload)


    def _cleanup_temp_files(self):
        for tmp_dir in self._temp_files_to_cleanup:
            try:
                shutil.rmtree(str(tmp_dir), ignore_errors=True)
            except Exception:
                pass
        self._temp_files_to_cleanup.clear()

    def _show_no_api_key_dialog(self):
        self._show_info_dialog(self._("no_api_key"), message_type=Gtk.MessageType.WARNING)


    def reload(self, is_startup: bool = False, manual: bool = False):
        if manual:
            # F5 / the ⋯ menu: say so, and say what it found when done.
            self._reload_manual = True
            self._show_resupply_pill()
        if self._scan_in_progress:
            # Its snapshot of settings/state is already taken, so this
            # request's changes (a game that just exited, an imported
            # backup) would otherwise never be picked up.
            self._reload_pending = True
            return
        self._scan_in_progress = True
        if is_startup:
            self._maybe_check_for_updates()
        enabled_snapshot = set(self.enabled_launchers)
        order_snapshot = list(self.launcher_order)
        game_order_snapshot = dict(self.game_order)
        thread = threading.Thread(
            target=self._scan_thread,
            args=(enabled_snapshot, order_snapshot, game_order_snapshot),
            daemon=True,
        )
        thread.start()

    def _scan_thread(self, enabled: set, launcher_order: List[str], game_order: Dict):
        try:
            scanner = Scanner(enabled)
            launchers = scanner.scan_all()
        except Exception as e:
            # The single most important safety net in the whole app:
            # this thread is what the startup-loading overlay's own
            # dismissal is entirely gated on (see _on_scan_complete
            # below, which is the only place that ever starts the
            # _poll_startup_ready loop that eventually hides it). Every
            # known risk here (Scanner.__init__'s load_hidden(),
            # scan_all() itself) is already independently careful, but
            # if anything ever DID slip through uncaught, skipping
            # GLib.idle_add entirely would leave the app stuck on its
            # loading screen forever, with no scanner and not a single
            # game shown, over what should be a recoverable failure.
            logger.error(f"Scan thread failed unexpectedly: {e}")
            # Built directly rather than via Scanner(set()) - that's
            # still a constructor call, and if the exception above
            # somehow came from inside Scanner.__init__ itself, a
            # second call could just as easily fail the same way and
            # leave this except block without ever reaching
            # GLib.idle_add. __new__ + manually setting the same three
            # plain attributes __init__ sets can't raise.
            scanner = Scanner.__new__(Scanner)
            scanner.hidden = {}
            scanner.enabled = set()
            scanner.scan_errors = {}
            launchers = []
        GLib.idle_add(self._on_scan_complete, scanner, launchers, launcher_order, game_order)

    def _on_scan_complete(self, scanner: "Scanner", launchers: List[Dict],
                           launcher_order: List[str], game_order: Dict):
        # a hide / unhide saved while the scan ran (its list was read at its
        # start) - and the list itself when the scan failed
        try:
            scanner.hidden = load_hidden()
        except Exception as e:
            logger.warning(f"Couldn't read the hidden games: {e}")
        self.scanner = scanner
        self._scan_in_progress = False
        if self._reload_pending:
            self._reload_pending = False
            # idle: runs after this handler has finished rendering.
            GLib.idle_add(lambda: self.reload() and False)
        diagnostics.note("scan complete - rendering")
        self._report_scan_errors(scanner)
        if getattr(self, "_proton_upkeep", None) is not None:
            self._proton_upkeep.after_scan()
        self._give_new_games_recommended([g for section in launchers for g in section.get("games", [])])
        if getattr(self, "startup_overlay", None) is not None:
            self._startup_overlay_deadline = time.monotonic() + self._STARTUP_OVERLAY_MAX_WAIT
            GLib.timeout_add(150, self._poll_startup_ready)

        if launcher_order:
            ordered_launchers = []
            launcher_dict = {l["name"]: l for l in launchers}
            for name in launcher_order:
                if name in launcher_dict:
                    ordered_launchers.append(launcher_dict[name])
            for launcher in launchers:
                if launcher["name"] not in launcher_order:
                    ordered_launchers.append(launcher)
            launchers = ordered_launchers

        for section in launchers:
            if section["name"] in game_order:
                order = game_order[section["name"]]
                game_dict = {g.uid: g for g in section["games"]}
                ordered_games = []
                for uid in order:
                    if uid in game_dict:
                        ordered_games.append(game_dict[uid])
                for game in section["games"]:
                    if game.uid not in order:
                        ordered_games.append(game)
                section["games"] = ordered_games

        launchers = self._deactivate_uninstalled_launchers(launchers)

        first_scan = not getattr(self, "_scanned_once", False)
        self._cached_launchers = launchers
        self._update_first_seen()
        self._update_known_titles()
        self._sync_native_playtimes()
        installed_uids = {game.uid for section in launchers for game in section["games"]}
        prunable_prefixes = {
            uid.split(":", 1)[0]
            for section in launchers
            for uid in (game.uid for game in section["games"])
        }
        self._prune_uninstalled_covers(installed_uids, prunable_prefixes)
        self._prune_uninstalled_game_icons(installed_uids, prunable_prefixes)
        added, removed = self._diff_library(launchers, installed_uids)
        # Only the very first scan builds the page with the reveal; later
        # ones swap it in place, and just the newly found games fade in.
        self._render_launchers(animate=first_scan)
        self._home_fade_uids = set()
        self._scanned_once = True
        manual, self._reload_manual = getattr(self, "_reload_manual", False), False
        if manual or added or removed:
            self._finish_resupply_pill(len(added), len(removed))
        all_games = [game for section in launchers for game in section["games"]]
        # an older reload's thread finishing last mustn't put its list back
        self._detector_gen = gen = getattr(self, "_detector_gen", 0) + 1

        def set_detector_targets(games=list(all_games)):
            targets = build_targets(games)
            if gen == self._detector_gen:
                self._running_detector.set_targets(targets)
        threading.Thread(target=set_detector_targets, name="plunderhub-detector-targets", daemon=True).start()
        steam_appids = [g.uid.split(":", 1)[1] for g in all_games if g.uid.startswith("steam:")]
        if steam_appids:
            GLib.idle_add(lambda: self._apply_steam_proton_default(steam_appids) and False)
        games_missing_art = self._games_missing_art(all_games)
        if games_missing_art:
            self._auto_download_missing_covers(games_missing_art)
        return False


    def _deactivate_uninstalled_launchers(self, launchers: List[Dict]) -> List[Dict]:
        changed = False
        kept: List[Dict] = []
        binary_by_name = {ldef.name: ldef.binary for ldef in LAUNCHER_REGISTRY}
        for section in launchers:
            binary = binary_by_name.get(section["name"])
            flatpak_ids = LAUNCHER_FLATPAK_IDS.get(section["name"], [])
            if binary is not None and resolve_launcher_invocation(binary, flatpak_ids) is None:
                self.enabled_launchers.discard(section["name"])
                changed = True
                continue
            kept.append(section)
        if changed:
            self.settings["enabled_launchers"] = sorted(self.enabled_launchers)
            save_settings(self.settings)
        return kept

    def _update_first_seen(self) -> List["GameItem"]:
        changed = False
        new_games: List["GameItem"] = []
        for section in self._cached_launchers:
            for game in section["games"]:
                if game.uid not in self.first_seen:
                    self.first_seen[game.uid] = time.time()
                    new_games.append(game)
                    changed = True
        if changed:
            save_first_seen(self.first_seen)
        return new_games

    def _update_known_titles(self):
        changed = False
        for section in self._cached_launchers:
            for game in section["games"]:
                entry = {"title": game.title, "launcher": game.launcher}
                if self.known_titles.get(game.uid) != entry:
                    self.known_titles[game.uid] = entry
                    changed = True
        if changed:
            save_known_titles(self.known_titles)

    def _merge_native_playtime(self, uid: str, minutes: int, last_played: Optional[int],
                                today: str) -> Tuple[bool, bool]:
        changed = False
        history_changed = False
        is_first_sync = uid not in self.playtime
        old_minutes = self.playtime.get(uid, 0)
        if is_first_sync:
            if minutes != old_minutes:
                self.playtime[uid] = minutes
                changed = True
        elif minutes > old_minutes:
            delta = minutes - old_minutes
            day_entry = self.playtime_history.setdefault(today, {})
            day_entry[uid] = day_entry.get(uid, 0) + delta
            self.playtime[uid] = minutes
            changed = True
            history_changed = True
        if last_played:
            merged = max(self.recent.get(uid, 0), last_played)
            if self.recent.get(uid) != merged:
                self.recent[uid] = merged
                changed = True
        return changed, history_changed

    def _sync_native_playtimes(self):
        changed = False
        history_changed = False
        today = datetime.now().strftime("%Y-%m-%d")

        known_steam_appids: set = set()
        heroic_uid_by_gameid: Dict[str, str] = {}
        known_faugus_gameids: set = set()
        known_lutris_gameids: set = set()
        for section in self._cached_launchers:
            if section["name"] not in self.enabled_launchers:
                continue
            for game in section["games"]:
                if game.launcher == "Steam" and game.uid.startswith("steam:"):
                    known_steam_appids.add(game.uid.split(":", 1)[1])
                elif game.launcher == "Heroic" and game.uid.startswith("heroic:"):
                    parts = game.uid.split(":", 2)
                    if len(parts) == 3:
                        heroic_uid_by_gameid[parts[2]] = game.uid
                elif game.launcher == "Faugus" and game.uid.startswith("faugus:"):
                    known_faugus_gameids.add(game.uid.split(":", 1)[1])
                elif game.launcher == "Lutris" and game.uid.startswith("lutris:"):
                    known_lutris_gameids.add(game.uid.split(":", 1)[1])

        if known_steam_appids:
            try:
                steam_times = read_steam_playtimes()
            except Exception as e:
                logger.warning(f"Steam playtime sync failed: {e}")
                steam_times = {}
            matched = 0
            for appid, (minutes, last_played) in steam_times.items():
                if appid not in known_steam_appids:
                    continue
                matched += 1
                uid = f"steam:{appid}"
                c, h = self._merge_native_playtime(uid, minutes, last_played, today)
                changed = changed or c
                history_changed = history_changed or h
            logger.info(
                f"Steam playtime sync: localconfig.vdf = {steam_localconfig_path()}, "
                f"{len(steam_times)} entries in the file, matched {matched}/{len(known_steam_appids)} "
                f"installed games."
            )

        if heroic_uid_by_gameid:
            try:
                heroic_times = read_heroic_playtimes()
            except Exception as e:
                logger.warning(f"Heroic playtime sync failed: {e}")
                heroic_times = {}
            matched = 0
            for game_id, uid in heroic_uid_by_gameid.items():
                data = heroic_times.get(game_id)
                if data is None:
                    continue
                matched += 1
                minutes, last_played = data
                c, h = self._merge_native_playtime(uid, minutes, last_played, today)
                changed = changed or c
                history_changed = history_changed or h
            logger.info(
                f"Heroic playtime sync: {len(heroic_times)} entries in store/timestamp.json, "
                f"matched {matched}/{len(heroic_uid_by_gameid)} installed games."
            )

        if known_faugus_gameids:
            try:
                faugus_times = read_faugus_playtimes()
            except Exception as e:
                logger.warning(f"Faugus playtime sync failed: {e}")
                faugus_times = {}
            matched = 0
            for game_id in known_faugus_gameids:
                data = faugus_times.get(game_id)
                if data is None:
                    continue
                matched += 1
                minutes, last_played = data
                uid = f"faugus:{game_id}"
                c, h = self._merge_native_playtime(uid, minutes, last_played, today)
                changed = changed or c
                history_changed = history_changed or h
            logger.info(
                f"Faugus playtime sync: {len(faugus_times)} entries in games.json, "
                f"matched {matched}/{len(known_faugus_gameids)} installed games."
            )

        if known_lutris_gameids:
            try:
                lutris_times = read_lutris_playtimes()
            except Exception as e:
                logger.warning(f"Lutris playtime sync failed: {e}")
                lutris_times = {}
            matched = 0
            for game_id in known_lutris_gameids:
                data = lutris_times.get(game_id)
                if data is None:
                    continue
                matched += 1
                minutes, last_played = data
                uid = f"lutris:{game_id}"
                c, h = self._merge_native_playtime(uid, minutes, last_played, today)
                changed = changed or c
                history_changed = history_changed or h
            logger.info(
                f"Lutris playtime sync: {len(lutris_times)} entries in pga.db, "
                f"matched {matched}/{len(known_lutris_gameids)} installed games."
            )

        if changed:
            save_playtime(self.playtime)
            save_recent(self.recent)
        if history_changed:
            save_playtime_history(self.playtime_history)

    def show_proton_page(self, parent=None, on_closed=None):
        """Shipwright's Proton page (it took ProtonPlus' place; ProtonPlus
        itself stays on Shipwright's shelf)."""
        self.show_shipwright(focus="protons", parent=parent, on_closed=on_closed)


    def _reload_settings_from_disk(self):
        self.settings = load_settings()
        self._load_settings_into_attrs()
        self._refresh_static_ui_language()
        self._apply_theme(self.theme)
        self.reload()

    def _build_tray_menu(self, keep_refs: bool = False) -> Gtk.Menu:
        menu = Gtk.Menu()

        show_item = Gtk.MenuItem()
        show_lbl = Gtk.Label()
        show_lbl.set_markup(f"<b>{GLib.markup_escape_text(self._('tray_show'))}</b>")
        show_lbl.set_xalign(0)
        show_item.add(show_lbl)
        show_item.connect("activate", lambda *_: self._restore_main_window())
        menu.append(show_item)

        games_item = Gtk.MenuItem(label=self._("tray_games"))
        games_submenu = Gtk.Menu()
        games_submenu.connect("show", lambda *_: self._populate_tray_games_menu(games_submenu))
        games_item.set_submenu(games_submenu)
        menu.append(games_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label=self._("tray_quit"))
        quit_item.connect("activate", lambda *_: self.request_quit())
        menu.append(quit_item)

        menu.show_all()

        if keep_refs:
            self._tray_menu = menu
            self._tray_show_label = show_lbl
            self._tray_games_item = games_item
            self._tray_quit_item = quit_item
        return menu

    def _retranslate_tray_menu(self):
        if self._tray_menu is None:
            return
        try:
            if self._tray_show_label is not None:
                self._tray_show_label.set_markup(
                    f"<b>{GLib.markup_escape_text(self._('tray_show'))}</b>"
                )
            if self._tray_games_item is not None:
                self._tray_games_item.set_label(self._("tray_games"))
            if self._tray_quit_item is not None:
                self._tray_quit_item.set_label(self._("tray_quit"))
            self._tray_menu.show_all()
        except Exception as e:
            logger.debug(f"Couldn't retranslate tray menu after language change: {e}")

    def _populate_tray_games_menu(self, submenu: Gtk.Menu):
        for child in list(submenu.get_children()):
            submenu.remove(child)

        games: List[GameItem] = []
        for section in self._cached_launchers:
            if section["name"] not in self.enabled_launchers:
                continue
            for game in section["games"]:
                if self.scanner.is_hidden(game.launcher, game.uid):
                    continue
                games.append(game)
        games.sort(key=lambda g: g.title.lower())

        if not games:
            empty_item = Gtk.MenuItem(label=self._("no_games"))
            empty_item.set_sensitive(False)
            submenu.append(empty_item)
        else:
            for game in games:
                item = Gtk.MenuItem(label=game.title)
                item.connect(
                    "activate",
                    lambda _i, g=game: self.request_launch(g.launch_cmd, g.title, g.env, g.uid, g.install_dir)
                )
                submenu.append(item)

        submenu.show_all()

    def _apply_window_icon(self):
        # Prefer the themed icon (installed builds); otherwise load the
        # bundled SVG directly so a source-tree run still shows the real icon.
        # Set as the default too: every other window (the first-run language
        # card, dialogs without a parent) gets it, or a taskbar that goes by
        # the window's own icon (Cinnamon) shows a generic one for them.
        icon_name = _app_icon_name()
        icon_file = _app_icon_file()
        if icon_name != APP_ICON_NAME and icon_file is not None:
            try:
                Gtk.Window.set_default_icon_from_file(str(icon_file))
                self.set_icon_from_file(str(icon_file))
                return
            except Exception as e:
                logger.warning(f"Couldn't load bundled app icon {icon_file}: {e}")
        Gtk.Window.set_default_icon_name(icon_name)
        self.set_icon_name(icon_name)

    def _setup_tray_icon(self):
        if self.tray_icon is not None:
            return

        if AppIndicator3 is not None:
            try:
                indicator = AppIndicator3.Indicator.new(
                    "plunderhub", _app_icon_spec(),
                    AppIndicator3.IndicatorCategory.APPLICATION_STATUS
                )
                indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
                indicator.set_menu(self._build_tray_menu(keep_refs=True))

                self.tray_icon = indicator
                self._tray_backend = "appindicator"
                logger.info(f"Tray icon initialized ({_INDICATOR_BACKEND} backend).")
                return
            except Exception as e:
                logger.warning(f"AppIndicator tray icon failed: {e}")

        try:
            icon_spec = _app_icon_spec()
            if icon_spec.startswith("/"):
                icon = Gtk.StatusIcon.new_from_file(icon_spec)
            else:
                icon = Gtk.StatusIcon.new_from_icon_name(icon_spec)
            icon.set_tooltip_text(APP_NAME)
            icon.connect("activate", lambda *_: self._toggle_window_visibility())
            icon.connect("popup-menu", self._on_tray_popup_menu)
            icon.set_visible(True)
            self.tray_icon = icon
            self._tray_backend = "statusicon"
            logger.info("Tray icon initialized (Gtk.StatusIcon fallback).")
        except Exception as e:
            logger.warning(f"Tray icon unavailable on this system: {e}")
            self.tray_icon = None
            self._tray_backend = None
            GLib.idle_add(self._show_tray_unavailable_warning)

    def _show_tray_unavailable_warning(self):
        self._show_info_dialog(self._("tray_unavailable"), message_type=Gtk.MessageType.WARNING)
        return False

    def _teardown_tray_icon(self):
        if self.tray_icon is not None:
            try:
                if self._tray_backend == "appindicator" and AppIndicator3 is not None:
                    self.tray_icon.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
                else:
                    self.tray_icon.set_visible(False)
            except Exception as e:
                logger.debug(f"Couldn't hide tray icon on teardown: {e}")
        self.tray_icon = None
        self._tray_backend = None
        self._tray_menu = None
        self._tray_show_label = None
        self._tray_games_item = None
        self._tray_quit_item = None

    def _toggle_window_visibility(self):
        if self.get_visible():
            self.hide()
        else:
            self._restore_main_window()

    def _on_tray_popup_menu(self, icon, button, time_):
        menu = self._build_tray_menu()
        menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, time_)

    # how long the window stays in the tray before its library is let go
    HIDDEN_TRIM_S = 30

    def _on_window_hidden(self):
        if self._hidden_trim_id is None:
            self._hidden_trim_id = GLib.timeout_add_seconds(self.HIDDEN_TRIM_S, self._drop_hidden_library)

    def _on_window_shown(self):
        if self._hidden_trim_id is not None:
            GLib.source_remove(self._hidden_trim_id)
            self._hidden_trim_id = None
        if self._library_dropped:
            self._library_dropped = False
            self._render_launchers()

    def _drop_hidden_library(self) -> bool:
        """In the tray nothing of the library shows: its cards go - their
        covers, the art drawn on them, the heroes and the animations - and
        it's built again when the window comes back. A library of a few
        hundred games held a few hundred MB of pictures for nobody."""
        self._hidden_trim_id = None
        if self.get_visible() or self._library_dropped:
            return False
        self._library_dropped = True
        self._anim_player.stop_all()
        from plunderhub.ui import animated_hero
        animated_hero.let_go_paused(self)
        # destroyed, not just removed: a card's own signal handlers hold on
        # to it, and only destroying it lets them - and its pictures - go
        for child in list(self.content.get_children()):
            child.destroy()
        self.cover_widgets.clear()
        self.game_icon_widgets.clear()
        self.selection_widgets.clear()
        self.running_time_widgets.clear()
        self._nav_groups.clear()
        getattr(self, "_home_new_tags", {}).clear()
        self._home_hero_play = None
        self._home_hero_area = None
        self._home_hero_anim = None
        self._home_hero_wanted = self._home_hero_last = None
        for name in ("_home_hero_cache", "_list_hero_cache", "_list_hero_surfaces"):
            cache = getattr(self, name, None)
            if cache:
                cache.clear()
        self._home_hero_surface = None
        if hasattr(self, "console_view") and not self.console_mode_active:
            self.console_view._art.clear()
            self.console_view._posters.clear()
        import gc
        gc.collect()
        memory.release_soon(500)
        return False

    def _tray_shown(self) -> bool:
        """Whether the tray icon is really on screen: making one never fails,
        but with no tray (plain GNOME, a bar without one) a hidden window
        could never be brought back."""
        if self.tray_icon is None:
            return False
        if self._tray_backend == "statusicon":
            return bool(self.tray_icon.is_embedded())
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "NameHasOwner", GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                                  GLib.VariantType("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
            return bool(reply.unpack()[0])
        except Exception as e:
            logger.debug(f"Couldn't ask for a tray host: {e}")
            return True     # can't tell: as before

    def _close_to_tray_or_quit(self):
        if self.use_tray and self._tray_shown():
            self.hide()
        else:
            self.request_quit()

    # how long Quit now waits at most for the work it stopped to get out
    _QUIT_WAIT_SECONDS = 300.0

    def add_quit_blocker(self, blocker):
        """Work Quit asks about while it runs - a Shipwright install, the
        update: quit_line() says what it does, quit_stop() stops it (or
        does nothing where it can't be), quit_done() once it's over."""
        self._quit_blockers.append(blocker)

    def remove_quit_blocker(self, blocker):
        if blocker in self._quit_blockers:
            self._quit_blockers.remove(blocker)

    def _running_work(self) -> List[Tuple[Optional[str], Optional[Callable[[], None]], Callable[[], bool]]]:
        """What still runs: (what to say of it - None for a quick one not
        worth asking about, how to stop it, whether it's done)."""
        from plunderhub.ui import proton_jobs
        work = []
        jobs = proton_jobs.jobs(self)
        job = jobs.job
        if job is not None:
            line = None
            if job.kind == "install":
                what = "pm_quit_unpacking" if job.stage in ("check", "unpack") else "pm_quit_downloading"
                line = self._(what).format(name=job.name, pct=job.percent())
            work.append((line, jobs.cancel, lambda: not jobs.busy()))
        for uid, mode in list(self._cloud_syncs.items()):
            if mode.startswith("done:"):
                continue
            game = self.game_index.get(uid)
            title = game.title if game is not None else uid
            # a sync or a restore is never cut short
            work.append((f"{title}: {self.launch_status(uid) or ''}".rstrip(": "), None,
                         lambda u=uid: self._cloud_syncs.get(u, "done:").startswith("done:")))
        for blocker in list(self._quit_blockers):
            if not blocker.quit_done():
                work.append((blocker.quit_line(), blocker.quit_stop, blocker.quit_done))
        return work

    def request_quit(self):
        """Quits - asked first while something is being downloaded or
        installed (a Proton build, a Shipwright install, the update) or
        saves are synced: it can be waited for (PlunderHub quits once
        it's all done), or stopped - what can be stopped is (a download
        dropped, an install undone), the rest waited for, so nothing is
        left half done."""
        work = self._running_work()
        if not work:
            self.destroy()
            return
        lines = [line for line, _stop, _done in work if line]
        if lines:
            proton_only = len(work) == 1 and work[0][1] is not None and self._proton_jobs_busy()
            response = self._card_dialog(
                self._("pm_quit_title" if proton_only else "quit_busy_title"),
                "\n".join(lines) + "\n\n" + self._("pm_quit_text" if proton_only else "quit_busy_text"),
                [(self._("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost"),
                 (self._("pm_quit_now"), Gtk.ResponseType.REJECT, "ghost"),
                 (self._("pm_quit_after"), Gtk.ResponseType.ACCEPT, "accent")],
                tone="question", default=Gtk.ResponseType.ACCEPT)
            if response == Gtk.ResponseType.ACCEPT:
                logger.info("Quitting once the work still running is done")
                self._quit_when_done(stopping=False)
                return
            if response != Gtk.ResponseType.REJECT:
                return
        logger.info("Quitting: stopping the work still running")
        for _line, stop, _done in work:
            if stop is not None:
                try:
                    stop()
                except Exception as e:
                    logger.warning(f"Couldn't stop some work before quitting: {e}")
        self._quit_when_done(stopping=True)

    def _proton_jobs_busy(self) -> bool:
        from plunderhub.ui import proton_jobs
        return proton_jobs.jobs(self).busy()

    def _quit_when_done(self, stopping: bool):
        """The main window out of sight (an install's own window stays)
        till the work is done, then quits. Waiting for it to finish is
        called off when the window is brought back from the tray."""
        self._quit_after_work = True
        self.hide()
        deadline = time.monotonic() + self._QUIT_WAIT_SECONDS

        def check() -> bool:
            if not self._quit_after_work:
                return False
            if any(not done() for _line, _stop, done in self._running_work()):
                if not stopping or time.monotonic() < deadline:
                    return True
                logger.warning("Quitting with work still running - it didn't stop in time")
            self.destroy()
            return False
        GLib.timeout_add(250, check)

    def on_delete_event(self, *_args):
        self._close_to_tray_or_quit()
        return True

    def _setup_file_watchers(self) -> bool:
        """Watches each launcher's library file / Steam steamapps folder
        that exists now and isn't watched yet; True when one was added.
        Run at startup and again when the window gets the focus back
        (_refresh_file_watchers): a launcher installed - or its first game,
        or a new Steam library - after PlunderHub started has nothing to
        watch at startup, and its games would wait for F5."""
        added = False
        single_file_watches: List[Path] = [
            p for p in (faugus_games_path(), lutris_db_path(), bottles_library_path()) if p is not None
        ]
        single_file_watches.extend(heroic_roots())
        for p in single_file_watches:
            added |= self._watch_path(p, self._on_watched_file_changed)

        for root in steam_roots():
            steamapps = root / "steamapps"
            if steamapps.exists():
                added |= self._watch_path(steamapps, self._on_watched_steamapps_changed)
                for lib in steam_library_paths(root):
                    lib_steamapps = lib / "steamapps"
                    if lib_steamapps.exists():
                        added |= self._watch_path(lib_steamapps, self._on_watched_steamapps_changed)
        return added

    def _watch_path(self, path: Path, handler) -> bool:
        """One monitor per real path (Steam's ~/.steam links lead to the
        same folder); True when a new one was set up."""
        try:
            if not path.exists():
                return False
            key = str(path.resolve())
            if key in self._watched_paths:
                return False
            monitor = Gio.File.new_for_path(str(path)).monitor(Gio.FileMonitorFlags.NONE, None)
            monitor.connect("changed", handler)
            self._file_monitors.append(monitor)
            self._watched_paths.add(key)
            return True
        except Exception as e:
            logger.warning(f"Couldn't watch: {path} ({e})")
            return False

    def _refresh_file_watchers(self):
        """On focus-in, at most every few seconds: a library that appeared
        since the last look gets watched and the games reloaded once."""
        now = time.monotonic()
        if now - self._watch_refresh_at < 3.0:
            return
        self._watch_refresh_at = now
        try:
            if self._setup_file_watchers():
                logger.info("A launcher library appeared - watching it, reloading the games")
                self._schedule_debounced_reload()
        except Exception as e:
            logger.warning(f"Refreshing the file watchers failed: {e}")

    def _on_watched_file_changed(self, monitor, file, other_file, event_type):
        self._schedule_debounced_reload()

    def _on_watched_steamapps_changed(self, monitor, file, other_file, event_type):
        if event_type not in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.DELETED):
            return
        name = file.get_basename() if file is not None else ""
        if not _APPMANIFEST_RE.match(name or ""):
            return
        self._schedule_debounced_reload()

    def _schedule_debounced_reload(self):
        if self._watch_debounce_id is not None:
            GLib.source_remove(self._watch_debounce_id)

        def _do_reload():
            self._watch_debounce_id = None
            self.reload()
            return False

        self._watch_debounce_id = GLib.timeout_add(2000, _do_reload)

class PlunderHubApplication(Gtk.Application):

    def __init__(self, self_test: bool = False, start_minimized: bool = False, steam_start: str = ""):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.window: Optional["App"] = None
        self._self_test = self_test
        self._start_minimized = start_minimized
        # started by Steam (core/steam_shortcut.py): "console" in Big Picture /
        # Game Mode, "ask" on the desktop; "" otherwise
        self._steam_start = steam_start
        action = Gio.SimpleAction.new("steam-start", GLib.VariantType.new("s"))
        action.connect("activate", lambda _a, value: self._on_steam_start(value.get_string()))
        self.add_action(action)

    def _on_steam_start(self, mode: str):
        """Steam started PlunderHub while it ran already: this one is told."""
        if self.window is None:
            self._steam_start = mode
            return
        self.window._restore_main_window()
        GLib.idle_add(self.window._steam_start, mode)

    def do_activate(self):
        if self.window is None:
            self.window = App()
            self.window.connect("destroy", lambda *_: self._on_window_destroyed())
            self.add_window(self.window)
            # --minimized (only ever passed by our own autostart .desktop
            # file - see core/autostart.py) skips showing the window at
            # all, as long as the tray icon actually came up: with no
            # tray icon there'd be no way left to ever bring the window
            # back, so this falls back to a normal, visible startup
            # rather than risk the app becoming permanently unreachable.
            if self._start_minimized and self.window.use_tray and self.window._tray_shown():
                # show_all() first, THEN hide() - GTK never auto-shows
                # a widget's children just because the window becomes
                # visible later; each child's own "visible" flag has
                # to be set at least once. Skipping straight to
                # hide() left every child widget (header bar, search
                # entry, the whole game grid) permanently invisible,
                # since bringing the window back later only goes
                # through _restore_main_window()'s plain show()/
                # present() - that maps the TOPLEVEL window, but
                # never touches children - so the window reappeared
                # as a flat, empty background with nothing in it at
                # all (reported directly, with a screenshot, after a
                # reboot into a --minimized autostart launch).
                self.window.show_all()
                self.window.hide()
            elif self._steam_start == "ask":
                # Steam started it on the desktop: only the question first,
                # PlunderHub opens once it's answered (App._steam_start)
                GLib.idle_add(self.window._steam_start, "ask")
            else:
                self.window.show_all()
                self.window.present()
                self.window.deiconify()
                if self._steam_start:
                    GLib.idle_add(self.window._steam_start, self._steam_start)
                elif not self._self_test:
                    # modal - a self-test on a fresh profile would wait on it
                    self.window._maybe_show_first_run_tip()
            if self._self_test:
                from plunderhub import selftest
                selftest.run(self.window)
        else:
            self.window._restore_main_window()
            self.window._maybe_show_first_run_tip()
        GLib.idle_add(lambda: self.window.set_focus(None))

    def _on_window_destroyed(self):
        # Every other window goes with the main one - Settings, a dialog,
        # the game page... Quitting from the tray used to leave them open
        # (each runs its own loop), the app half-gone behind them.
        main = self.window
        for window in Gtk.Window.list_toplevels():
            if window is not main and window.get_window_type() == Gtk.WindowType.TOPLEVEL:
                try:
                    if isinstance(window, Gtk.Dialog):
                        window.response(Gtk.ResponseType.DELETE_EVENT)
                    window.destroy()
                except Exception as e:
                    logger.debug(f"Couldn't close {window}: {e}")
        detector = getattr(self.window, "_running_detector", None) if self.window else None
        if detector is not None:
            detector.stop()
        probe = getattr(self.window, "_process_probe", None) if self.window else None
        if probe is not None:
            probe.stop()
        joystick_manager = getattr(self.window, "_joystick_manager", None) if self.window else None
        if joystick_manager is not None:
            joystick_manager.stop()
        # Every one of these below is a GLib.timeout_add id stored on
        # the window that, left pending, would fire AFTER the window
        # is gone and try to touch its (by-then-destroyed) widgets -
        # search-changed and file-watcher reload debouncing, the
        # undo-toast auto-dismiss, and Console Mode's cursor-auto-hide
        # timer all follow this same pattern, so they're all cleaned
        # up here the same way.
        for attr_name in ("_search_debounce_id", "_watch_debounce_id",
                           "_undo_toast_timeout_id", "_console_cursor_hide_id"):
            timeout_id = getattr(self.window, attr_name, None) if self.window else None
            if timeout_id is not None:
                GLib.source_remove(timeout_id)
        self.window = None
        self.quit()

def _set_wm_class():
    try:
        GLib.set_prgname(APP_NAME)
    except Exception as e:
        logger.debug(f"Couldn't set prgname to {APP_NAME!r}: {e}")
    try:
        Gdk.set_program_class(APP_NAME)
    except Exception as e:
        logger.debug(f"Couldn't set program class to {APP_NAME!r}: {e}")


def main():
    if "--version" in sys.argv or "-v" in sys.argv:
        print(f"{APP_NAME} {APP_VERSION}")
        print(f"Author: {APP_AUTHOR}")
        return
    install_exception_logging()
    memory.configure()

    from plunderhub.core import steam_shortcut
    argv = [arg for arg in sys.argv if arg not in ("--diagnose", "--self-test", "--minimized",
                                                   steam_shortcut.FROM_STEAM, "--console")]
    # started by Steam: Big Picture / Game Mode always in Console Mode, the
    # desktop asks; --console asks for Console Mode outright
    steam_start = ""
    if "--console" in sys.argv or (steam_shortcut.started_from_steam(sys.argv) and steam_shortcut.in_gamepad_ui()):
        steam_start = "console"
    elif steam_shortcut.started_from_steam(sys.argv):
        steam_start = "ask"
    if steam_shortcut.started_from_steam(sys.argv):
        logger.info(f"Started from Steam ({steam_start}): SteamGamepadUI={os.environ.get('SteamGamepadUI')!r}, "
                    f"XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP')!r}, "
                    f"SteamDeck={os.environ.get('SteamDeck')!r}, SteamGameId={os.environ.get('SteamGameId')!r}")
    if "--diagnose" in sys.argv:
        diagnostics.enable()
    self_testing = "--self-test" in sys.argv
    start_minimized = "--minimized" in sys.argv

    migrate_cache_dirs()
    _set_wm_class()
    app = PlunderHubApplication(self_test=self_testing, start_minimized=start_minimized, steam_start=steam_start)
    if steam_start:
        try:
            app.register(None)
            if app.get_is_remote():
                # PlunderHub runs already: it opens as Steam asked, this one ends
                app.activate_action("steam-start", GLib.Variant.new_string(steam_start))
                return
        except GLib.Error as e:
            logger.warning(f"Couldn't reach a running PlunderHub: {e.message}")
    app.run(argv)
    if self_testing:
        from plunderhub import selftest
        selftest.finish_process()
