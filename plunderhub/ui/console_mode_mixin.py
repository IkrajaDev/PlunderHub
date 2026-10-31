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


from typing import Optional, Tuple

from plunderhub.constants import CONSOLE_UI_SCALE_CSS_PERCENT
from plunderhub.core.persistence import save_settings
from plunderhub.gi_setup import Gdk, GLib, Gtk
from plunderhub.logging_setup import logger
from plunderhub.ui.console_view import ConsoleView


class ConsoleModeMixin:
    def _console_glyph_dimensions(self, base_diameter: int, shoulder: bool) -> Tuple[int, int, int]:
        percent = CONSOLE_UI_SCALE_CSS_PERCENT.get(self.console_ui_scale, 100)
        factor = percent / 100
        if shoulder:
            w = int(base_diameter * 2.2 * factor)
            h = int(base_diameter * 0.6 * factor)
            font_px = max(7, round(base_diameter * 0.375 * factor))
        else:
            w = h = int(base_diameter * factor)
            font_px = max(7, round(base_diameter * 0.4 * factor))
        return w, h, font_px

    def _console_size_glyph(self, glyph: Gtk.Label, size_provider: Gtk.CssProvider,
                             base_diameter: int, shoulder: bool):
        w, h, font_px = self._console_glyph_dimensions(base_diameter, shoulder)
        glyph.set_size_request(w, h)
        try:
            size_provider.load_from_data(f"* {{ font-size: {font_px}px; }}".encode())
        except Exception as e:
            logger.warning(f"Failed to size controller-hint glyph: {e}")

    def _console_controller_glyph(self, text: str, shoulder: bool = False, diameter: int = 48,
                                   persistent: bool = False, role: Optional[str] = None) -> Gtk.Label:
        glyph = Gtk.Label(label=text)
        glyph.get_style_context().add_class("controller-glyph")
        glyph.set_halign(Gtk.Align.CENTER)
        glyph.set_valign(Gtk.Align.CENTER)
        if shoulder:
            glyph.get_style_context().add_class("controller-glyph-shoulder")
        size_provider = Gtk.CssProvider()
        glyph.get_style_context().add_provider(size_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._console_size_glyph(glyph, size_provider, diameter, shoulder)
        if persistent:
            self._console_topbar_glyphs.append((glyph, size_provider, diameter, shoulder))
        if role is not None:
            self._console_family_glyphs.append((glyph, role))
        return glyph

    def _build_console_mode_view(self) -> Gtk.Box:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_no_show_all(True)
        outer.hide()
        self.console_view = ConsoleView(self)
        console_overlay = Gtk.Overlay()
        console_overlay.add(self.console_view)
        self.console_animations_paused_bar, self.console_animations_paused_label = \
            self._build_animations_paused_bar()
        self.console_animations_paused_bar.set_halign(Gtk.Align.CENTER)
        self.console_animations_paused_bar.set_valign(Gtk.Align.START)
        self.console_animations_paused_bar.set_margin_top(0)    # the pill's own margin holds its shadow
        console_overlay.add_overlay(self.console_animations_paused_bar)
        console_overlay.set_overlay_pass_through(self.console_animations_paused_bar, True)
        outer.pack_start(console_overlay, True, True, 0)
        return outer

    def _toggle_console_mode(self):
        if not self.get_visible():
            return
        if self.console_mode_active:
            self._exit_console_mode()
        else:
            self._enter_console_mode()

    def _console_stash_windows(self):
        """Console Mode comes over PlunderHub's other windows (Settings, a
        game's page, an install's progress...) without closing them: only
        their native window is hidden. GTK still has them open - a dialog's
        run() goes on, so does the work in it - and leaving Console Mode
        shows them again as they were. Hiding the GtkWindow itself would
        end a dialog's run(), and a dialog left showing stays above its
        fullscreen parent on most desktops. A modal one lets go of the
        input meanwhile (Console Mode's keys and clicks)."""
        stashed = []
        for win in Gtk.Window.list_toplevels():
            if win is self or not win.get_visible() or win.get_window_type() != Gtk.WindowType.TOPLEVEL:
                continue
            gdk_win = win.get_window()
            if gdk_win is None or not gdk_win.is_visible():
                continue
            stashed.append((win, win.get_modal()))
            win.set_modal(False)
            gdk_win.hide()
        self._console_stashed = stashed

    def _console_unstash_windows(self):
        stashed, self._console_stashed = getattr(self, "_console_stashed", []), []
        for win, modal in stashed:
            gdk_win = win.get_window()
            if gdk_win is None or not win.get_visible():
                continue        # closed meanwhile (an install that finished, say)
            gdk_win.show()
            win.set_modal(modal)
            win.present()

    def _enter_console_mode(self):
        if self.console_mode_active:
            return
        self._console_stash_windows()
        self.console_mode_active = True
        self.home_root.hide()
        self.fullscreen()
        self.console_box.set_no_show_all(False)
        self.console_box.show_all()
        self._console_reset_cursor_hide_timer()
        self._populate_console_mode()
        self.console_view.grab_focus()

    def _exit_console_mode(self):
        if not self.console_mode_active:
            return
        self.console_mode_active = False
        self._console_lost_fullscreen = False
        self.console_box.hide()
        self.unfullscreen()
        self.home_root.show()
        self._console_cancel_cursor_hide_timer()
        self._console_show_cursor()
        self._resync_animated_cover_sizes()
        self._console_unstash_windows()

    def _on_console_motion_notify(self, _widget, _event):
        if self.console_mode_active:
            self._console_reset_cursor_hide_timer()
        return False

    def _on_window_focus_in(self, _widget, _event):
        if self.console_mode_active and self._console_hidden_for_game:
            self._console_hidden_for_game = False
            self._console_reset_cursor_hide_timer()
        self._console_take_fullscreen_back()
        return False

    def _console_take_fullscreen_back(self):
        """Back from a game whose fullscreen took Console Mode's - after a
        moment, and only if PlunderHub still has the focus then: a game's
        splash window closing hands it over for an instant, and taking
        the fullscreen then would take it from the game."""
        if not (self.console_mode_active and self._console_lost_fullscreen):
            return

        def take() -> bool:
            if self.console_mode_active and self._console_lost_fullscreen and self.is_active():
                self._console_lost_fullscreen = False
                self.fullscreen()
            return False
        GLib.timeout_add(500, take)

    def _console_reset_cursor_hide_timer(self):
        self._console_cancel_cursor_hide_timer()
        self._console_show_cursor()
        if self.console_mode_active and not self._console_hidden_for_game:
            self._console_cursor_hide_id = GLib.timeout_add(1000, self._console_on_cursor_hide_timeout)

    def _console_cancel_cursor_hide_timer(self):
        if self._console_cursor_hide_id is not None:
            GLib.source_remove(self._console_cursor_hide_id)
            self._console_cursor_hide_id = None

    def _console_on_cursor_hide_timeout(self) -> bool:
        self._console_cursor_hide_id = None
        self._console_hide_cursor()
        return False

    def _console_hide_cursor(self):
        if self._console_cursor_hidden:
            return
        win = self.get_window()
        if win is None:
            return
        if self._console_blank_cursor is None:
            self._console_blank_cursor = Gdk.Cursor.new_for_display(self.get_display(), Gdk.CursorType.BLANK_CURSOR)
        win.set_cursor(self._console_blank_cursor)
        self._console_cursor_hidden = True

    def _console_show_cursor(self):
        if not self._console_cursor_hidden:
            return
        win = self.get_window()
        if win is not None:
            win.set_cursor(None)
        self._console_cursor_hidden = False

    def _populate_console_mode(self, steal_focus: bool = True):
        """The library changed (a scan, a launch, a favourite, a new
        cover) - rebuild the shelves, keeping the selected game."""
        self.console_view.refresh()
        if steal_focus and self.console_mode_active:
            self.console_view.grab_focus()

    # --- controller / keyboard actions -----------------------------------------

    def _console_move_focus(self, direction: str):
        self.console_view.move(direction)

    def _console_shift_launcher_filter(self, direction: int):
        self.console_view.shift_tab(direction)

    def _console_activate(self):
        game = self.console_view.selected()
        if game is None:
            return
        self._clear_new_game(game.uid)
        if self._is_game_running(game.uid):
            self._on_stop_game_clicked(None, game)
        elif game.uid not in self.launching_uids:
            self._on_game_card_clicked(None, game)

    def _console_toggle_favorite(self):
        game = self.console_view.selected()
        if game is None:
            return
        adding = game.uid not in self.favorites
        self._toggle_favorite(game)
        self.console_view.toast(("★  " if adding else "☆  ") + game.title)

    # the main window's Sort by menu, in its order (Custom = Edit order's own)
    CONSOLE_SORT_CYCLE = ["custom", "alpha", "newest", "recent", "playtime"]

    def _console_sort_label(self) -> str:
        keys = {"alpha": "sort_alpha", "newest": "sort_newest", "recent": "sort_recent",
                "playtime": "sort_playtime", "custom": "sort_custom"}
        return self._(keys.get(self.sort_mode, "sort_alpha"))

    def _console_cycle_sort(self):
        try:
            idx = self.CONSOLE_SORT_CYCLE.index(self.sort_mode)
        except ValueError:
            idx = -1
        self.sort_mode = self.CONSOLE_SORT_CYCLE[(idx + 1) % len(self.CONSOLE_SORT_CYCLE)]
        self.settings["sort_mode"] = self.sort_mode
        save_settings(self.settings)
        self._render_launchers()
        self.console_view.toast(f"{self._('sort_by')}: {self._console_sort_label()}")
