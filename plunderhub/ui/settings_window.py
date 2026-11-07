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
"""The Settings window: a page list on the left, cards of setting rows
on the right, and every change applied as soon as it's made.

Keyboard and controller share one model (see handle()):
  arrows / d-pad   move between rows (up/down) and within a row (left/right);
                   left from a row's first control goes back to the page list
  Enter / A        sliders, number and text fields enter edit mode,
                   drop-downs open, switches flip, buttons press
  Esc / B          leave edit mode; from a page back to the page list
  Ctrl+Tab, LB/RB  previous / next page          Ctrl+F  search
"""

import math
import threading
import webbrowser
from typing import Callable, Dict, List, Optional, Tuple

from plunderhub.constants import (
    APP_AUTHOR, APP_CHANNEL, APP_VERSION, CONFIG_DIR, KOFI_URL,
    FONT_SCALE_LABEL_KEYS, FONT_SCALE_ORDER, GAME_ICON_SIZE_LABEL_KEYS,
)
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango, pixbuf_at_size
from plunderhub.i18n import LANGUAGE_NATIVE_NAMES, LANGUAGE_ORDER
from plunderhub.icons import get_icon_pixbuf
from plunderhub.launchers.registry import LAUNCHER_REGISTRY
from plunderhub.logging_setup import logger
from plunderhub.theme import DANGER, THEME_ORDER, THEME_PALETTES, _app_icon_file, mark_logo, system_swatch_colors
from plunderhub.perf_guard import allow_hover_relayout
from plunderhub.ui import launcher_tasks

# Controls that only take the arrows after Enter / A.
_EDITABLE = (Gtk.Scale, Gtk.SpinButton, Gtk.Entry)
# Face buttons in their own colours, as printed on the pads.
_FACE_COLORS = {"A": "#5dbb63", "B": "#e5534b", "X": "#408cf2", "Y": "#e3b341",
                "✕": "#6e9cf0", "○": "#e5534b", "□": "#d980d9", "△": "#4fc3a1"}
_APPLY_DELAY_MS = 350

# page id, icon, title key, subtitle key
_PAGES = [
    ("general", "preferences-other-symbolic", "tab_general", "settings_sub_general"),
    ("appearance", "applications-graphics-symbolic", "tab_appearance", "settings_sub_appearance"),
    ("library", "applications-games-symbolic", "settings_page_library", "settings_sub_library"),
    ("covers", "image-x-generic-symbolic", "tab_covers", "settings_sub_covers"),
    ("console", "input-gaming-symbolic", "settings_page_console", "settings_sub_console"),
    ("system", "drive-harddisk-symbolic", "settings_page_system", "settings_sub_system"),
]
# Old tab names (and the Updates banner) still open the right page.
_TAB_ALIASES = {"sources": "library", "hidden": "library", "data": "system", "updates": "system",
                "about": "system", "shortcuts": "console"}


class SettingsWindow:

    def __init__(self, app, initial_tab=None):
        self.app = app
        self._ = app._
        self.rows: Dict[str, List[Tuple[Gtk.Widget, List]]] = {}   # page -> [(row, [controls])]
        self.owner: Dict[int, Gtk.Widget] = {}                     # id(focusable) -> its control
        self.search_index: List[Tuple[str, str, Gtk.Widget]] = []
        self.editing: Optional[Gtk.Widget] = None
        self.keynav = False
        self.focused_row: Optional[Gtk.Widget] = None
        self.theme_flow: Optional[Gtk.FlowBox] = None
        self._apply_id: Optional[int] = None
        self._building = True
        self._reopen_on: Optional[str] = None
        self._page_id = "general"

        app._settings_theme_choice = app.theme
        dlg = Gtk.Dialog(title=self._("options"), transient_for=app, flags=0)
        dlg.set_modal(True)
        dlg.set_default_size(*self._default_size())
        dlg.connect("key-press-event", self._on_key)
        dlg.connect("set-focus", lambda _w, widget: (self._update_hint(widget),
                                                     GLib.idle_add(self._mark_focused_row),
                                                     GLib.idle_add(self._keep_in_view, widget)))
        dlg.connect("button-press-event", lambda *_: self._set_keynav(False))
        # The app routes controller events for the active dialog here.
        dlg.plunderhub_nav = self.handle
        dlg.plunderhub_scroll = self.scroll_by
        self.dialog = dlg
        action_area = dlg.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()

        area = dlg.get_content_area()
        area.set_spacing(0)
        area.set_border_width(0)
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        area.pack_start(outer, True, True, 0)
        outer.pack_start(self._build_sidebar(), False, False, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        right.pack_start(self.stack, True, True, 0)
        right.pack_start(self._build_footer(), False, False, 0)
        outer.pack_start(right, True, True, 0)

        for page_id, _icon, title_key, sub_key in _PAGES:
            builder = getattr(self, f"_page_{page_id}")
            self.stack.add_named(self._page(page_id, self._(title_key), self._(sub_key), builder), page_id)

        self.goto(initial_tab)
        self._building = False

    # --- running -------------------------------------------------------------

    def run(self):
        app = self.app
        app._options_dialog = self.dialog
        app._options_dialog_goto_tab = self.goto
        self.dialog.show_all()
        self.search_results.hide()
        self._refresh_launcher_rows()
        self._update_hint()
        GLib.timeout_add(1000, self._tick)
        self.dialog.set_focus(None)
        app._enter_dimmed_dialog()
        self.dialog.run()
        app._settings_last_tab = self._page_id
        app._exit_dimmed_dialog()
        self._flush_apply()
        self.dialog.destroy()
        app._options_dialog = None
        app._options_dialog_goto_tab = None
        if self._reopen_on is not None:
            # A new language: the whole window is rebuilt in it.
            GLib.idle_add(app.show_options, self._reopen_on)
            return
        app._apply_theme(app.theme)
        app._apply_font_scale(app.font_scale)
        app._apply_console_ui_scale(app.console_ui_scale)
        if app._pending_reload:
            app._pending_reload = False
            app.reload()

    def goto(self, tab):
        tab = _TAB_ALIASES.get(tab, tab)
        if tab not in {p[0] for p in _PAGES}:
            tab = "general"
        page = self.stack.get_child_by_name(tab)
        if page is not None:
            # A stack ignores a hidden child as its visible one - and before
            # the first show_all() every page is still hidden.
            page.show_all()
        for row in self.nav.get_children():
            if row.page_id == tab:
                self.nav.select_row(row)
        self._page_id = tab
        self.stack.set_visible_child_name(tab)

    def _default_size(self) -> Tuple[int, int]:
        width, height = 1060, 720
        try:
            display = self.app.get_display()
            window = self.app.get_window()
            monitor = display.get_monitor_at_window(window) if window else display.get_monitor(0)
            if monitor is not None:
                work = monitor.get_workarea()
                width = min(width, work.width - 60)
                height = min(height, int(work.height * 0.85))
        except Exception as e:
            logger.debug(f"Settings: couldn't read the monitor size: {e}")
        return width, height

    def _tick(self) -> bool:
        if self.dialog.get_window() is None:
            return False
        self._update_hint()     # follows a controller being plugged in / out
        return True

    # --- applying changes -------------------------------------------------------

    def changed(self, *_args):
        """Every control calls this. Saving waits for a short pause, so a
        dragged slider applies once rather than on every pixel."""
        if self._building:
            return
        if self._apply_id is not None:
            GLib.source_remove(self._apply_id)
        self._apply_id = GLib.timeout_add(_APPLY_DELAY_MS, self._apply)

    def _apply(self) -> bool:
        self._apply_id = None
        lang_before = self.app.lang
        try:
            self.app.on_save_settings()
        except Exception:
            logger.exception("Applying settings failed")
        if self.app.lang != lang_before:
            self._reopen_on = self._page_id
            self.dialog.response(Gtk.ResponseType.CLOSE)
        return False

    def _flush_apply(self):
        if self._apply_id is not None:
            GLib.source_remove(self._apply_id)
            self._apply()

    # --- sidebar and footer -----------------------------------------------------

    def _build_sidebar(self) -> Gtk.Box:
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        side.get_style_context().add_class("settings-sidebar")
        side.set_size_request(260, -1)

        brand = Gtk.Box(spacing=10)
        brand.set_margin_top(18)
        brand.set_margin_start(18)
        logo = Gtk.Image()
        mark_logo(logo, 36)
        brand.pack_start(logo, False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title = Gtk.Label(label=self._("options"), xalign=0)
        title.get_style_context().add_class("settings-app-title")
        version = Gtk.Label(label=f"PlunderHub {self._version_text()}", xalign=0)
        version.get_style_context().add_class("settings-app-sub")
        titles.pack_start(title, False, False, 0)
        titles.pack_start(version, False, False, 0)
        brand.pack_start(titles, False, False, 0)
        side.pack_start(brand, False, False, 0)

        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(self._("settings_search_placeholder"))
        self.search.set_margin_start(12)
        self.search.set_margin_end(12)
        self.search.connect("search-changed", self._on_search)
        side.pack_start(self.search, False, False, 0)

        self.nav = Gtk.ListBox()
        self.nav.get_style_context().add_class("settings-nav")
        self.nav.set_selection_mode(Gtk.SelectionMode.BROWSE)
        for page_id, icon, title_key, _sub in _PAGES:
            row = Gtk.ListBoxRow()
            row.page_id = page_id
            box = Gtk.Box(spacing=12)
            box.pack_start(self.app._icon_image(icon, Gtk.IconSize.BUTTON), False, False, 0)
            box.pack_start(Gtk.Label(label=self._(title_key), xalign=0), True, True, 0)
            row.add(box)
            self.nav.add(row)
        self.nav.connect("row-selected", self._on_page_selected)
        side.pack_start(self.nav, False, False, 0)

        self.search_results = Gtk.ListBox()
        self.search_results.get_style_context().add_class("settings-nav")
        self.search_results.connect("row-activated", self._on_search_hit)
        side.pack_start(self.search_results, True, True, 0)
        return side

    def _on_page_selected(self, _list, row):
        if row is None:
            return
        self.stop_edit()
        self._page_id = row.page_id
        self.stack.set_visible_child_name(row.page_id)

    def _build_footer(self) -> Gtk.Box:
        footer = Gtk.Box(spacing=8)
        footer.set_border_width(10)
        footer.set_margin_start(14)
        footer.set_margin_end(14)
        # Left: what A / Enter does right here. Centre: the standing promise
        # that nothing needs saving - hidden where it isn't true (the API
        # key, which has its own Save button).
        self.hint_label = Gtk.Label(xalign=0)
        self.hint_label.get_style_context().add_class("settings-hint")
        footer.pack_start(self.hint_label, False, False, 0)
        self.applied_label = Gtk.Label(label=self._("settings_applied"))
        self.applied_label.get_style_context().add_class("settings-footer")
        self.applied_label.set_no_show_all(True)
        self.applied_label.show()
        footer.set_center_widget(self.applied_label)
        self.close_btn = Gtk.Button(label=self._("close_btn"))
        self.close_btn.connect("clicked", lambda *_: self.dialog.response(Gtk.ResponseType.CLOSE))
        footer.pack_end(self.close_btn, False, False, 0)
        return footer

    # --- page scaffolding --------------------------------------------------------

    def _page(self, page_id: str, title: str, subtitle: str, builder: Callable) -> Gtk.ScrolledWindow:
        self._current_page = page_id
        self.rows[page_id] = []
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # A solid, always-visible bar in the accent colour instead of the
        # thin overlay one that only appears while scrolling - so a page
        # that continues below the window says so at a glance.
        scroller.set_overlay_scrolling(False)
        scroller.get_style_context().add_class("settings-scroll")
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        column.set_border_width(28)
        # A readable column in the middle, rather than rows stretched
        # across a wide window with each control far from its name.
        column.set_size_request(760, -1)
        column.set_halign(Gtk.Align.CENTER)
        head = Gtk.Label(label=title, xalign=0)
        head.get_style_context().add_class("settings-page-title")
        sub = Gtk.Label(label=subtitle, xalign=0)
        sub.get_style_context().add_class("settings-page-sub")
        column.pack_start(head, False, False, 0)
        column.pack_start(sub, False, False, 6)
        builder(column)
        # the focus is kept in view by _keep_in_view - GTK's own scrolling
        # to it showed the row's card, and fought it
        scroller.add(column)
        # the footer's hint comes and goes with the row stood on, and the
        # page's height with it: the row is kept in view again then
        scroller.connect("size-allocate", lambda *_: self.keynav and GLib.idle_add(
            self._keep_in_view, self.dialog.get_focus()))
        return scroller

    def group(self, column: Gtk.Box, title: str, danger: bool = False, icon: Optional[str] = None) -> Gtk.Box:
        heading_row = Gtk.Box(spacing=8)
        heading_row.set_margin_top(14)
        if icon:
            heading_row.pack_start(self.app._icon_image(icon, Gtk.IconSize.MENU), False, False, 0)
        heading = Gtk.Label(label=title.upper(), xalign=0)
        heading.get_style_context().add_class("settings-group-title")
        if danger:
            heading.get_style_context().add_class("settings-danger-title")
        heading_row.pack_start(heading, False, False, 0)
        column.pack_start(heading_row, False, False, 0)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.get_style_context().add_class("settings-card")
        if danger:
            card.get_style_context().add_class("settings-danger")
        column.pack_start(card, False, False, 0)
        return card

    def _separator(self, card: Gtk.Box):
        if card.get_children():
            sep = Gtk.Box()
            sep.get_style_context().add_class("settings-row-sep")
            sep.set_margin_start(16)
            sep.set_margin_end(16)
            card.pack_start(sep, False, False, 0)

    def _version_text(self) -> str:
        """1.0.5 - with "beta" after it while this is a beta."""
        return f"{APP_VERSION} {self._('beta_badge').lower()}" if APP_CHANNEL else APP_VERSION

    def row(self, card: Gtk.Box, title: str, subtitle=None, controls=(), icon=None,
            extra_lines=(), pill: Optional[str] = None, strong_pill: bool = False) -> Gtk.EventBox:
        """One setting: icon, name (+ explanation) on the left, its
        controls lined up on the right. subtitle may be a Gtk.Label the
        caller keeps updating; pill is a badge beside the name
        ("Recommended"), filled in the accent colour with strong_pill."""
        self._separator(card)
        row = Gtk.EventBox()
        box = Gtk.Box(spacing=14)
        box.get_style_context().add_class("settings-row")
        if icon:
            # an icon name - or a ready image (a launcher's logo)
            image = icon if isinstance(icon, Gtk.Widget) else self.app._icon_image(icon, Gtk.IconSize.LARGE_TOOLBAR)
            image.set_valign(Gtk.Align.CENTER)
            box.pack_start(image, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.set_valign(Gtk.Align.CENTER)
        name = Gtk.Label(label=title, xalign=0)
        name.set_line_wrap(True)
        name.get_style_context().add_class("settings-row-title")
        if pill:
            # on a line of its own above the name - beside it, it squeezed
            # the name into wrapping
            badge = Gtk.Label(label=pill)
            badge.get_style_context().add_class("settings-pill")
            if strong_pill:
                badge.get_style_context().add_class("settings-pill-strong")
            badge.set_halign(Gtk.Align.START)
            badge.set_margin_bottom(2)
            texts.pack_start(badge, False, False, 0)
        texts.pack_start(name, False, False, 0)
        for line in extra_lines:
            texts.pack_start(line, False, False, 0)
        row.subtitle = None
        if subtitle is not None:
            if isinstance(subtitle, Gtk.Label):
                sub = subtitle
                sub.set_xalign(0)
            else:
                sub = Gtk.Label(label=subtitle, xalign=0)
            sub.set_line_wrap(True)
            sub.set_max_width_chars(60)
            sub.get_style_context().add_class("settings-row-sub")
            texts.pack_start(sub, False, False, 0)
            row.subtitle = sub
        box.pack_start(texts, True, True, 0)
        right = Gtk.Box(spacing=8)
        right.set_valign(Gtk.Align.CENTER)
        for control in controls:
            control.set_valign(Gtk.Align.CENTER)
            right.pack_start(control, False, False, 0)
        box.pack_end(right, False, False, 0)
        row.add(box)
        card.pack_start(row, False, False, 0)
        self.search_index.append((title, self._current_page, row))
        self._register(row, controls)
        return row

    def _register(self, row: Gtk.Widget, controls):
        targets = []
        for control in controls:
            target = getattr(control, "settings_target", control)
            if not isinstance(target, (Gtk.Button, Gtk.Switch, Gtk.Scale, Gtk.Entry, Gtk.ComboBox)):
                continue
            targets.append(target)
            self.owner[id(target)] = target
            if isinstance(target, Gtk.ComboBox):
                for child in _descendants(target):
                    self.owner[id(child)] = target
            if isinstance(target, Gtk.Entry):
                target.connect("button-press-event", lambda w, _e: self.start_edit(w) or False)
            if isinstance(target, Gtk.Switch):
                target.connect("notify::active", lambda *_: self._update_hint())
        if targets:
            self.rows[self._current_page].append((row, targets))

    # --- controls -------------------------------------------------------------------

    def switch(self, active: bool) -> Gtk.Switch:
        sw = Gtk.Switch()
        sw.set_active(active)
        sw.connect("notify::active", self.changed)
        return sw

    def combo(self, items: List[str], active: int, title: str) -> Gtk.ComboBoxText:
        cb = Gtk.ComboBoxText()
        for item in items:
            cb.append_text(item)
        cb.set_active(active)
        cb.connect("changed", self.changed)
        self.app._combo_picker_titles[cb] = title
        return cb

    def button(self, label: str, icon: Optional[str] = None, suggested: bool = False,
               destructive: bool = False, group: Optional[Gtk.SizeGroup] = None) -> Gtk.Button:
        btn = Gtk.Button()
        inner = Gtk.Box(spacing=7)
        inner.set_halign(Gtk.Align.CENTER)
        if icon:
            image = self.app._icon_image(icon, Gtk.IconSize.BUTTON)
            inner.pack_start(image, False, False, 0)
        inner.pack_start(Gtk.Label(label=label), False, False, 0)
        btn.add(inner)
        if suggested:
            self.app._mark_suggested_action(btn)
        if destructive:
            btn.get_style_context().add_class("destructive-action")
            allow_hover_relayout(btn, "turns red with light text under the pointer")
        if icon:
            # On the accent pill in its text colour; on a red one, red; on
            # a plain one, in the accent.
            if suggested:
                colour = self.app._icon_tint_for(image)
            elif destructive:
                colour = DANGER
            else:
                colour = self.app._home_accent_hex()
            pixbuf = get_icon_pixbuf(icon, 16, colour)
            if pixbuf is not None:
                image.set_from_pixbuf(pixbuf)
        if group is not None:
            group.add_widget(btn)
        return btn

    def slider(self, scale: Gtk.Scale, value_label: Gtk.Label) -> Gtk.Box:
        box = Gtk.Box(spacing=8)
        scale.set_draw_value(False)
        scale.set_size_request(180, -1)
        self.app._disable_scale_scroll(scale)
        value_label.set_width_chars(8)
        value_label.set_xalign(1)
        box.pack_start(scale, False, False, 0)
        box.pack_start(value_label, False, False, 0)
        box.settings_target = scale
        scale.connect("value-changed", self.changed)
        return box

    # --- pages --------------------------------------------------------------------------

    def _page_general(self, col: Gtk.Box):
        app = self.app
        card = self.group(col, self._("section_launch_behavior"))
        app.launch_mode_combo = self.combo(
            [self._("launch_mode_instant"), self._("launch_mode_confirm")],
            1 if app.launch_mode == "confirm" else 0, self._("section_launch_behavior"))
        self.row(card, self._("settings_launch_click"), None, [app.launch_mode_combo], icon="rocket-symbolic")
        app.auto_close_check = self.switch(app.auto_close)
        self.row(card, self._("auto_close_check"), None, [app.auto_close_check], icon="window-close-symbolic")
        app.auto_minimize_check = self.switch(app.minimize_on_launch)
        self.row(card, self._("auto_minimize_check"), None, [app.auto_minimize_check],
                 icon="window-minimize-symbolic")
        app.next_workspace_check = None
        if app.workspace_mover() is not None:
            compositor = app.workspace_mover().compositor
            sub = self._("next_workspace_sub").format(wm=compositor.name)
            if compositor.xwayland_only:
                sub += "\n" + self._("wm_xwayland_note").format(wm=compositor.name)
            app.next_workspace_check = self.switch(app.launch_on_next_workspace)
            self.row(card, self._("next_workspace_check"), sub,
                     [app.next_workspace_check], icon="go-next-symbolic")

        card = self.group(col, self._("section_window_behavior"))
        app.tray_check = self.switch(app.use_tray)
        tray_sub = Gtk.Label(label=self._("settings_tray_sub"))
        self.row(card, self._("tray_check"), tray_sub, [app.tray_check], icon="picture-in-picture-symbolic")
        app.run_on_startup_check = self.switch(app.run_on_startup)
        self.row(card, self._("run_on_startup_check"), self._("settings_startup_sub"),
                 [app.run_on_startup_check], icon="power-symbolic")

        def on_minimize(*_):
            # Minimising after launch needs somewhere to come back from.
            locked = app.auto_minimize_check.get_active()
            if locked:
                app.tray_check.set_active(True)
            app.tray_check.set_sensitive(not locked)
            tray_sub.set_text(self._("settings_tray_locked_sub") if locked else self._("settings_tray_sub"))
        app.auto_minimize_check.connect("notify::active", on_minimize)
        on_minimize()

        card = self.group(col, self._("section_language"))
        app.lang_combo_codes = list(LANGUAGE_ORDER)
        try:
            lang_idx = app.lang_combo_codes.index(app.lang)
        except ValueError:
            lang_idx = app.lang_combo_codes.index("en")
        app.lang_combo = self.combo([LANGUAGE_NATIVE_NAMES[c] for c in LANGUAGE_ORDER], lang_idx,
                                    self._("section_language"))
        self.row(card, _strip_colon(self._("language_label")), self._("ai_translation_note"),
                 [app.lang_combo], icon="languages-symbolic")

        card = self.group(col, self._("section_ui_extras"))
        app.scroll_top_btn_check = self.switch(app.scroll_top_button_enabled)
        self.row(card, self._("scroll_top_btn_check"), None, [app.scroll_top_btn_check],
                 icon="arrow-up-to-line-symbolic")

    def _page_appearance(self, col: Gtk.Box):
        app = self.app
        heading = Gtk.Label(label=self._("section_theme").upper(), xalign=0)
        heading.get_style_context().add_class("settings-group-title")
        heading.set_margin_top(14)
        col.pack_start(heading, False, False, 0)
        flow = Gtk.FlowBox()
        flow.get_style_context().add_class("settings-themes")
        flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
        flow.set_activate_on_single_click(True)
        flow.set_homogeneous(True)
        flow.set_halign(Gtk.Align.START)
        flow.set_max_children_per_line(4)
        flow.set_min_children_per_line(4)
        flow.set_column_spacing(10)
        flow.set_row_spacing(10)
        for name in THEME_ORDER:
            flow.add(self._swatch(name))
        flow.connect("child-activated", lambda _f, child: self._choose_theme(child.theme_name))
        col.pack_start(flow, False, False, 0)
        self.theme_flow = flow
        self.rows[self._current_page].append((flow, ["grid"]))
        self.search_index.append((self._("section_theme"), self._current_page, flow))
        self._mark_current_theme()

        card = self.group(col, self._("section_font_size"))
        app.font_scale_adj = Gtk.Adjustment(value=FONT_SCALE_ORDER.index(app.font_scale), lower=0,
                                            upper=len(FONT_SCALE_ORDER) - 1, step_increment=1,
                                            page_increment=1, page_size=0)
        app.font_scale_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=app.font_scale_adj)
        app.font_scale_scale.set_digits(0)
        app.font_scale_label = Gtk.Label(label=self._(FONT_SCALE_LABEL_KEYS[app.font_scale]))
        app.font_scale_scale.connect("change-value", app._on_font_scale_change_value)
        app.font_scale_scale.connect("value-changed", app._on_font_scale_value_changed)
        self.row(card, self._("settings_text_size"), None,
                 [self.slider(app.font_scale_scale, app.font_scale_label)], icon="type-symbolic")

        card = self.group(col, self._("settings_group_cards"))
        app.cover_size_adj = Gtk.Adjustment(value=app.cover_size, lower=200, upper=500, step_increment=50,
                                            page_increment=50, page_size=0)
        app.cover_size_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=app.cover_size_adj)
        app.cover_size_scale.set_digits(0)
        app.cover_size_label = Gtk.Label(label=app._cover_size_label_text(app.cover_size))
        app.cover_size_scale.connect("change-value", app._on_cover_scale_change_value)
        app.cover_size_scale.connect("value-changed", app._on_cover_scale_value_changed)
        self.row(card, self._("settings_cover_size"), self._("settings_cover_size_sub"),
                 [self.slider(app.cover_size_scale, app.cover_size_label)], icon="scaling-symbolic")

        icon_order = sorted(GAME_ICON_SIZE_LABEL_KEYS)
        app.game_icon_size_adj = Gtk.Adjustment(value=icon_order.index(app.game_icon_size), lower=0,
                                                upper=len(icon_order) - 1, step_increment=1,
                                                page_increment=1, page_size=0)
        app.game_icon_size_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                                             adjustment=app.game_icon_size_adj)
        app.game_icon_size_scale.set_digits(0)
        app.game_icon_size_label = Gtk.Label(label=app._game_icon_size_label_text(app.game_icon_size))
        app.game_icon_size_scale.connect("change-value", app._on_game_icon_scale_change_value)
        app.game_icon_size_scale.connect("value-changed", app._on_game_icon_scale_value_changed)
        self.row(card, self._("section_icon_size"), self._("icon_size_info"),
                 [self.slider(app.game_icon_size_scale, app.game_icon_size_label)], icon="view-list-symbolic")

        app.show_playtime_check = self.switch(app.show_playtime)
        self.row(card, self._("show_playtime_check"), None, [app.show_playtime_check], icon="clock-symbolic")
        app.show_game_titles_check = self.switch(app.show_game_titles)
        self.row(card, self._("show_game_titles_check"), None, [app.show_game_titles_check],
                 icon="case-sensitive-symbolic")
        app.show_card_menu_check = self.switch(app.show_card_menu_button)
        self.row(card, self._("show_card_menu_check"), self._("show_card_menu_sub"), [app.show_card_menu_check],
                 icon="view-more-symbolic")

        card = self.group(col, self._("settings_console_group"))
        app.console_cover_size_adj = Gtk.Adjustment(value=app.console_cover_size, lower=200, upper=500,
                                                    step_increment=50, page_increment=50, page_size=0)
        app.console_cover_size_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                                                 adjustment=app.console_cover_size_adj)
        app.console_cover_size_scale.set_digits(0)
        app.console_cover_size_label = Gtk.Label(label=app._cover_size_label_text(app.console_cover_size))
        app.console_cover_size_scale.connect("change-value", app._on_console_cover_scale_change_value)
        app.console_cover_size_scale.connect("value-changed", app._on_console_cover_scale_value_changed)
        self.row(card, self._("settings_cover_size"), self._("settings_console_cover_sub"),
                 [self.slider(app.console_cover_size_scale, app.console_cover_size_label)],
                 icon="scaling-symbolic")
        app.console_ui_scale_adj = Gtk.Adjustment(value=app.console_ui_scale, lower=100, upper=400,
                                                  step_increment=50, page_increment=50, page_size=0)
        app.console_ui_scale_scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL,
                                               adjustment=app.console_ui_scale_adj)
        app.console_ui_scale_scale.set_digits(0)
        app.console_ui_scale_label = Gtk.Label(label=app._console_ui_size_label_text(app.console_ui_scale))
        app.console_ui_scale_scale.connect("change-value", app._on_console_ui_scale_change_value)
        app.console_ui_scale_scale.connect("value-changed", app._on_console_ui_scale_value_changed)
        self.row(card, self._("settings_console_ui"), self._("settings_console_ui_sub"),
                 [self.slider(app.console_ui_scale_scale, app.console_ui_scale_label)],
                 icon="layout-dashboard-symbolic")

        card = self.group(col, self._("section_background"))
        app.show_bubbles_check = self.switch(app.show_bubbles)
        self.row(card, self._("show_bubbles_check"), None, [app.show_bubbles_check], icon="droplets-symbolic")
        app.pause_animations_check = self.switch(app.pause_animations_when_idle)
        self.row(card, self._("pause_animations_check"), self._("pause_animations_info"),
                 [app.pause_animations_check], icon="pause-symbolic")
        app.dim_dialogs_check = self.switch(app.dim_background_for_dialogs)
        self.row(card, self._("dim_dialogs_check"), self._("dim_dialogs_info"), [app.dim_dialogs_check],
                 icon="moon-symbolic")

    def _swatch(self, name: str) -> Gtk.FlowBoxChild:
        child = Gtk.FlowBoxChild()
        child.theme_name = name
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        area = Gtk.DrawingArea()
        area.set_size_request(118, 64)
        area.set_halign(Gtk.Align.CENTER)
        area.connect("draw", _draw_swatch, THEME_PALETTES.get(name))
        box.pack_start(area, False, False, 0)
        label = Gtk.Label(label=self._(f"theme_{name}"))
        label.get_style_context().add_class("settings-swatch-name")
        box.pack_start(label, False, False, 0)
        child.name_label = label
        child.add(box)
        return child

    def _choose_theme(self, name: str):
        if name == self.app._settings_theme_choice:
            return
        self.app._settings_theme_choice = name
        self.app._apply_theme(name)
        self._mark_current_theme()
        self.changed()

    def _mark_current_theme(self):
        current = self.app._settings_theme_choice
        for child in self.theme_flow.get_children():
            is_current = child.theme_name == current
            child.name_label.set_text(("✓ " if is_current else "") + self._(f"theme_{child.theme_name}"))
            ctx = child.name_label.get_style_context()
            (ctx.add_class if is_current else ctx.remove_class)("settings-swatch-current")
            if is_current:
                self.theme_flow.select_child(child)

    def _page_library(self, col: Gtk.Box):
        # first on the page: everyone comes here to switch on their launchers
        from plunderhub.core.steam_shortcut import steam_users
        if steam_users():
            from plunderhub.ui import steam_shortcut_ui
            steam_shortcut_ui.build(self, self.group(col, self._("ss_group")))
        self.launcher_card = self.group(col, self._("sources_header_launchers"))
        self.app.source_toggles = {}

        card = self.group(col, self._("sw_settings_group"))
        sw_btn = self.button(self._("sw_open_btn"), "hammer-symbolic")
        sw_btn.connect("clicked", lambda *_: (self.stop_edit(), self.app.show_shipwright(parent=self.dialog)))
        self.row(card, self._("sw_title"), self._("sw_settings_sub"), [sw_btn], icon="hammer-symbolic")
        from plunderhub.ui.proton_page import S as proton_texts
        proton_btn = self.button(proton_texts["manage"], "wine-symbolic")
        proton_btn.connect("clicked", lambda *_: (self.stop_edit(), self.app.show_proton_page(parent=self.dialog)))
        proton_auto = self.switch(self.app.settings.get("proton_auto_update", True))

        def proton_auto_toggled(switch, _pspec):
            from plunderhub.core.persistence import save_settings
            self.app.settings["proton_auto_update"] = switch.get_active()
            save_settings(self.app.settings)
        proton_auto.connect("notify::active", proton_auto_toggled)
        self.row(card, proton_texts["auto_title"], proton_texts["auto_sub"], [proton_btn, proton_auto],
                 icon="wine-symbolic")
        protondb = self.switch(self.app.settings.get("protondb_enabled", True))

        def protondb_toggled(switch, _pspec):
            from plunderhub.core.persistence import save_settings
            self.app.settings["protondb_enabled"] = switch.get_active()
            save_settings(self.app.settings)
        protondb.connect("notify::active", protondb_toggled)
        self.row(card, self._("pdb_setting_title"), self._("pdb_setting_sub"), [protondb],
                 icon="starred-symbolic")

        card = self.group(col, self._("tab_hidden"))
        hidden_any = False
        for launcher, ids in self.app.scanner.hidden.items():
            for uid in list(ids):
                hidden_any = True
                game = self.app.game_index.get(uid)
                title = game.title if game is not None else uid
                btn = self.button(self._("unhide_label"), "view-reveal-symbolic")
                row = self.row(card, title, launcher, [btn], icon="view-conceal-symbolic")
                btn.connect("clicked", self._unhide, launcher, uid, row, card)
        if not hidden_any:
            empty = Gtk.Label(label=self._("no_hidden"), xalign=0)
            empty.get_style_context().add_class("settings-row-sub")
            empty.get_style_context().add_class("settings-row")
            card.pack_start(empty, False, False, 0)

    def _unhide(self, _btn, launcher: str, uid: str, row: Gtk.Widget, card: Gtk.Box):
        self.app.scanner.unhide(launcher, uid)
        self.app._render_launchers()
        page_rows = self.rows["library"]
        self.rows["library"] = [r for r in page_rows if r[0] is not row]
        self.search_index = [e for e in self.search_index if e[2] is not row]
        row.destroy()
        if not [c for c in card.get_children() if isinstance(c, Gtk.EventBox)]:
            for child in card.get_children():
                child.destroy()
            empty = Gtk.Label(label=self._("no_hidden"), xalign=0)
            empty.get_style_context().add_class("settings-row-sub")
            empty.get_style_context().add_class("settings-row")
            card.pack_start(empty, False, False, 0)
            empty.show()
        self._to_sidebar()

    def _refresh_launcher_rows(self, states: Optional[Dict] = None):
        """(Re)builds the launcher list - also after an install/removal.
        Drawn from the states known last (asking the package manager takes
        from half a second to seconds); fresh ones are asked for off the GTK
        thread and the list is built again if they differ."""
        fresh = states is not None
        if states is None:
            states = getattr(self.app, "_launcher_state_cache", None)
        if states is None:
            # never asked yet this run: once, here
            states = {l.name: launcher_tasks.launcher_state(l.name) for l in LAUNCHER_REGISTRY}
            self.app._launcher_state_cache = states
            fresh = True
        self._build_launcher_rows(states)
        if not fresh:
            self._ask_launcher_states()

    def _ask_launcher_states(self):
        card = self.launcher_card

        def work():
            try:
                states = {l.name: launcher_tasks.launcher_state(l.name) for l in LAUNCHER_REGISTRY}
            except Exception as e:
                logger.warning(f"Couldn't read the launchers' state: {e}")
                return
            GLib.idle_add(done, states)

        def done(states):
            changed = states != self.app._launcher_state_cache
            self.app._launcher_state_cache = states
            if changed and card.get_toplevel().get_visible():
                self._refresh_launcher_rows(states)
            return False
        threading.Thread(target=work, daemon=True, name="plunderhub-launcher-state").start()

    def _build_launcher_rows(self, states: Dict):
        card = self.launcher_card
        old_rows = {id(child) for child in card.get_children()}
        self.rows["library"] = [r for r in self.rows["library"] if id(r[0]) not in old_rows]
        self.search_index = [e for e in self.search_index if id(e[2]) not in old_rows]
        for child in card.get_children():
            child.destroy()
        enabled = {name: toggle.get_active() for name, toggle in self.app.source_toggles.items()}
        self.app.source_toggles = {}
        buttons = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        toggles = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self._current_page = "library"
        new_rows = []
        for ldef in LAUNCHER_REGISTRY:
            name = ldef.name
            state = states.get(name) or launcher_tasks.launcher_state(name)
            toggle = Gtk.Switch()
            toggle.set_active(enabled.get(name, name in self.app.enabled_launchers))
            toggle.connect("notify::active", self.changed)
            self.app.source_toggles[name] = toggle
            controls = []
            if state.installed:
                btn = self.button(self._("uninstall_btn"), "user-trash-symbolic", group=buttons)
                if state.locked_by:
                    btn.set_sensitive(False)
                else:
                    btn.connect("clicked", lambda _b, n=name: self._start_task(n, "remove"))
                controls = [btn, toggle]
                toggles.add_widget(toggle)
            else:
                btn = self.button(self._("install_btn"), "system-software-install-symbolic", group=buttons)
                btn.connect("clicked", lambda _b, n=name: self._start_task(n, "install"))
                spacer = Gtk.Box()
                toggles.add_widget(spacer)
                controls = [btn, spacer]
            status = Gtk.Label(xalign=0)
            if state.installed:
                status.set_text("● " + self._("source_installed") + (f" · {state.source_label}" if state.source_label else ""))
                status.get_style_context().add_class("settings-status-on")
            else:
                status.set_text("○ " + self._("source_not_installed"))
                status.get_style_context().add_class("settings-status-off")
            lines = [status]
            if state.locked_by:
                lock = Gtk.Label(label=self._("settings_bundle_lock").format(
                    bundle=", ".join(state.locked_by), name=name), xalign=0)
                lock.set_line_wrap(True)
                lock.set_max_width_chars(60)
                lock.get_style_context().add_class("settings-row-sub")
                lines.append(lock)
            row = self.row(card, launcher_tasks.display_name(name), None, controls,
                           icon=launcher_tasks.launcher_icon(self.app, name, 28), extra_lines=lines)
            new_rows.append(row)
        # The launcher rows come before the hidden-games rows.
        first = [r for r in self.rows["library"] if getattr(r[0], "plunderhub_first", False)]
        launcher_entries = [r for r in self.rows["library"] if r[0] in new_rows]
        others = [r for r in self.rows["library"] if r[0] not in new_rows and r not in first]
        self.rows["library"] = first + launcher_entries + others
        card.show_all()

    def _start_task(self, name: str, action: str):
        self.stop_edit()
        launcher_tasks.start(self.app, name, action, self.dialog, on_finished=self._after_task)

    def _after_task(self):
        self._refresh_launcher_rows()
        self.changed()

    def _page_covers(self, col: Gtk.Box):
        app = self.app
        card = self.group(col, self._("tab_covers"))
        app.show_covers_check = self.switch(app.show_covers)
        self.row(card, self._("show_covers_check"), self._("show_covers_desc"), [app.show_covers_check],
                 icon="image-x-generic-symbolic", pill=self._("settings_highly_recommended"), strong_pill=True)
        fill_btn = self.button(self._("art_fill_btn"), "folder-download-symbolic")
        fill_btn.connect("clicked", app._fill_missing_art_clicked)
        app._art_fill_btn = fill_btn
        self.row(card, self._("art_fill_row"), self._("art_fill_row_sub"), [fill_btn], icon="image-down-symbolic")

        card = self.group(col, self._("section_api"))
        app.api_entry = Gtk.Entry()
        app.api_entry.set_text(app.api_key or "")
        app.api_entry.set_visibility(False)
        # a slim dot: the hidden key takes about the room the shown one does
        app.api_entry.set_invisible_char("•")
        # hidden, a modest field - the dots scroll; shown, it grows to the key
        app.api_entry.set_width_chars(self._KEY_FIELD_CHARS)
        app.api_entry.set_icon_from_pixbuf(Gtk.EntryIconPosition.SECONDARY,
                                           app._icon_pixbuf("view-conceal-symbolic", 16))
        app.api_entry.set_icon_activatable(Gtk.EntryIconPosition.SECONDARY, True)
        app.api_entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, self._("api_key_show_tooltip"))
        app.api_entry.connect("icon-press", app._on_api_key_visibility_toggle)
        # After the toggle above: the field grows to show the whole key,
        # and shrinks back when it's hidden again.
        app.api_entry.connect("icon-press", self._animate_key_field)
        app.api_entry.manual_save = True
        save_btn = self.button(self._("save_btn"), "document-save-symbolic", suggested=True)
        save_btn.manual_save = True
        save_btn.connect("clicked", app.on_save_api_key)
        self.row(card, _strip_colon(self._("api_label")), self._("api_key_storage_info"),
                 [app.api_entry, save_btn], icon="key-round-symbolic",
                 pill=self._("lo_recommended"))
        gen_btn = self.button(self._("gen_api_btn"), "web-browser-symbolic")
        gen_btn.connect("clicked", lambda *_: webbrowser.open("https://www.steamgriddb.com/profile/preferences/api"))
        test_btn = self.button(self._("test_btn"), "circle-dashed-check-symbolic")
        test_btn.connect("clicked", app.on_test_api_key)
        self.row(card, self._("settings_api_tools"), self._("settings_api_tools_sub"), [gen_btn, test_btn],
                 icon="emblem-ok-symbolic")

    _KEY_FIELD_CHARS = 16
    _KEY_FIELD_ANIM_MS = 220

    def _animate_key_field(self, entry: Gtk.Entry, *_args):
        start = entry.get_width_chars()
        hidden = self._KEY_FIELD_CHARS
        target = max(hidden, len(entry.get_text()) + 3) if entry.get_visibility() else hidden
        if getattr(self, "_key_anim_id", None):
            GLib.source_remove(self._key_anim_id)
        began = GLib.get_monotonic_time()

        def step():
            t = min(1.0, (GLib.get_monotonic_time() - began) / (self._KEY_FIELD_ANIM_MS * 1000))
            eased = 1 - (1 - t) ** 3
            entry.set_width_chars(round(start + (target - start) * eased))
            if t >= 1.0:
                self._key_anim_id = None
                return False
            return True
        self._key_anim_id = GLib.timeout_add(15, step)

    def _page_console(self, col: Gtk.Box):
        """Every shortcut, in the groups of the shortcuts window
        (ui/shortcut_list): the keyboard's, then a controller's."""
        from plunderhub.ui.shortcut_list import CONTROLLER, KEYBOARD, OWN_KEYS_COLUMN
        keys_col = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        keyboard = self._("shortcuts_keyboard_heading")
        for title_key, icon, rows in KEYBOARD:
            card = self.group(col, f"{keyboard} · {self._(title_key)}", icon=icon)
            for keys, key in rows:
                self._shortcut_row(card, [self._keycaps(keys, keys_col)], self._(key))

        controller = self._("shortcuts_controller_heading")
        xbox_col = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        ps_col = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for title_key, icon, rows in CONTROLLER:
            card = self.group(col, f"{controller} · {self._(title_key)}", icon=icon)
            if title_key in OWN_KEYS_COLUMN:
                xbox_col = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
                ps_col = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
            # which column is which pad, over every card's buttons
            header = Gtk.Box(spacing=18)
            header.get_style_context().add_class("settings-row")
            for text, size_group in (("Xbox", xbox_col), ("PlayStation", ps_col)):
                lbl = Gtk.Label(label=text, xalign=0)
                lbl.get_style_context().add_class("settings-shortcut-head")
                size_group.add_widget(lbl)
                header.pack_start(lbl, False, False, 0)
            card.pack_start(header, False, False, 0)
            for (xbox, ps), key in rows:
                xbox = [self._("shortcut_start") if k == "Start" else k for k in xbox.split("+")]
                ps = ps.split("+")
                self._shortcut_row(card, [self._keycaps(xbox, xbox_col, _FACE_COLORS.get(xbox[0])),
                                          self._keycaps(ps, ps_col, _FACE_COLORS.get(ps[0]))], self._(key))

    def _keycaps(self, keys, size_group: Gtk.SizeGroup, color: Optional[str] = None) -> Gtk.Box:
        box = Gtk.Box(spacing=4)
        for i, k in enumerate(keys):
            if i and (keys[0] == "Ctrl" or keys[0] in ("View", "Share")):
                plus = Gtk.Label(label="+")
                plus.get_style_context().add_class("settings-row-sub")
                box.pack_start(plus, False, False, 0)
            cap = Gtk.Label()
            if color:
                cap.set_markup(f"<span foreground='{color}'>{GLib.markup_escape_text(k)}</span>")
            else:
                cap.set_text(k)
            cap.get_style_context().add_class("settings-key")
            box.pack_start(cap, False, False, 0)
        size_group.add_widget(box)
        return box

    def _shortcut_row(self, card: Gtk.Box, key_boxes, text: str, tag: Optional[str] = None):
        """Keys first, what they do right next to them - a cheat sheet,
        not a setting, so the arrows skip it."""
        if not hasattr(card, "desc_col"):
            card.desc_col = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self._separator(card)
        box = Gtk.Box(spacing=18)
        box.get_style_context().add_class("settings-row")
        box.get_style_context().add_class("settings-shortcut")
        for kb in key_boxes:
            box.pack_start(kb, False, False, 0)
        desc = Gtk.Label(label=text, xalign=0)
        card.desc_col.add_widget(desc)
        box.pack_start(desc, False, False, 0)
        if tag:
            pill = Gtk.Label(label=tag)
            pill.get_style_context().add_class("settings-tag")
            pill.set_valign(Gtk.Align.CENTER)
            box.pack_start(pill, False, False, 0)
        card.pack_start(box, False, False, 0)

    def _page_system(self, col: Gtk.Box):
        app = self.app
        card = self.group(col, self._("section_updates"))
        app.update_check_enabled_check = self.switch(app.update_check_enabled)
        self.row(card, self._("update_check_enabled_check"), self._("update_check_enabled_info"),
                 [app.update_check_enabled_check], icon="view-refresh-symbolic")
        app.update_check_now_btn = Gtk.Button(label=self._("update_check_now_btn"))
        app.update_check_now_btn.connect("clicked", lambda *_: app._on_check_now_or_install_clicked())
        app.update_check_status_label = Gtk.Label(label="")
        self.row(card, self._("about_version").format(version=APP_VERSION), app.update_check_status_label,
                 [app.update_check_now_btn], icon="system-software-install-symbolic")
        app._refresh_update_check_status_label()
        app._refresh_update_check_button()

        card = self.group(col, self._("section_backup"))
        export_btn = self.button(self._("export_settings_btn"), "document-save-as-symbolic")
        export_btn.connect("clicked", lambda *_: app._export_settings(self.dialog))
        import_btn = self.button(self._("import_settings_btn"), "document-open-symbolic")
        import_btn.connect("clicked", lambda *_: app._import_settings(self.dialog))
        # when the last backup was made, in the accent, above what it holds
        app.backup_status_label = Gtk.Label(label=app._backup_status_text(), xalign=0)
        app.backup_status_label.get_style_context().add_class("settings-status-on")
        self.row(card, self._("settings_backup_title"), self._("settings_backup_sub"),
                 [export_btn, import_btn], icon="drive-multidisk-symbolic", extra_lines=[app.backup_status_label])
        config_btn = self.button(self._("config_btn"), "folder-symbolic")
        config_btn.connect("clicked", lambda *_: app._open_config_folder())
        self.row(card, self._("section_config_folder"), str(CONFIG_DIR).replace(str(GLib.get_home_dir()), "~"),
                 [config_btn], icon="folder-symbolic")

        card = self.group(col, self._("tab_about"))
        github_btn = self.button("GitHub", "github-symbolic")
        github_btn.connect("clicked", lambda *_: webbrowser.open("https://github.com/IkrajaDev/PlunderHub"))
        kofi_btn = self.button("Ko-fi", "starred-symbolic")
        kofi_btn.connect("clicked", lambda *_: webbrowser.open(KOFI_URL))
        license_btn = self.button("GPL-3.0", "scroll-text-symbolic")
        license_btn.connect("clicked", lambda *_: webbrowser.open(
            "https://github.com/IkrajaDev/PlunderHub/blob/main/LICENSE"))
        bug_btn = self.button(self._("about_report_bug"), "bug-symbolic")
        bug_btn.connect("clicked", lambda b: app._show_bug_report(b))
        self.row(card, f"PlunderHub {self._version_text()}", self._("about_author").format(author=APP_AUTHOR),
                 [github_btn, kofi_btn, license_btn, bug_btn], icon="help-about-symbolic")
        log_btn = self.button(self._("log_open_btn"), "scroll-text-symbolic")
        log_btn.connect("clicked", lambda *_: self._open_captains_log())
        self.row(card, self._("log_title"), self._("log_row_sub").format(version=APP_VERSION),
                 [log_btn], icon="scroll-text-symbolic")

        card = self.group(col, self._("section_danger_zone"), danger=True)
        delete_btn = self.button(self._("delete_config_btn"), "user-trash-symbolic", destructive=True)
        delete_btn.connect("clicked", lambda *_: app._delete_full_config(self.dialog))
        self.row(card, self._("delete_config_btn"), self._("settings_delete_sub"), [delete_btn],
                 icon="triangle-alert-symbolic")

    def _open_captains_log(self):
        from plunderhub.ui import captains_log
        captains_log.show(self.app, self.dialog)

    # --- keyboard / controller ---------------------------------------------------

    def _visible_rows(self):
        return [(w, t) for w, t in self.rows[self._page_id] if w.get_mapped()]

    def _control_for(self, widget: Optional[Gtk.Widget]) -> Optional[Gtk.Widget]:
        while widget is not None:
            control = self.owner.get(id(widget))
            if control is not None:
                return control
            widget = widget.get_parent()
        return None

    def _locate(self, focus):
        """(row index, column) of the focused control on this page; the
        column is "grid" inside the theme picker."""
        if focus is None:
            return None
        rows = self._visible_rows()
        if isinstance(focus, Gtk.FlowBoxChild):
            for i, (_w, targets) in enumerate(rows):
                if targets == ["grid"]:
                    return i, "grid"
            return None
        control = self._control_for(focus)
        if control is None:
            return None
        for i, (_w, targets) in enumerate(rows):
            if control in targets:
                return i, targets.index(control)
        return None

    def _focusable(self, targets):
        return [t for t in targets if t == "grid" or t.get_sensitive()]

    def _keep_in_view(self, widget: Optional[Gtk.Widget]) -> bool:
        """The page scrolls so the whole row stood on shows, with room
        round it - GTK's own focus scrolling brought the row's card into
        view, and a last row was left half under the bottom edge."""
        if widget is None or self.dialog.get_window() is None:
            return False
        scroller = widget.get_ancestor(Gtk.ScrolledWindow)
        if scroller is None or scroller is widget:
            return False
        where = self._locate(widget)
        target = self._visible_rows()[where[0]][0] if where is not None and where[1] != "grid" else widget
        content = scroller.get_child()
        if isinstance(content, Gtk.Viewport):
            content = content.get_child()
        at = target.translate_coordinates(content, 0, 0) if content is not None else None
        if not at:
            return False
        adj = scroller.get_vadjustment()
        top, h, view, margin = at[-1], target.get_allocated_height(), adj.get_page_size(), 28
        if top + h + margin > adj.get_value() + view:
            adj.set_value(min(adj.get_upper() - view, top + h + margin - view))
        if top - margin < adj.get_value():
            adj.set_value(max(adj.get_lower(), top - margin))
        return False

    def scroll_by(self, delta: float):
        """The right stick: the page moves, and the row stood on with it -
        once it would leave the view, the nearest row still wholly in it
        is stood on (_keep_in_view pulled the page back to the old one)."""
        page = self.stack.get_visible_child()
        adj = page.get_vadjustment() if isinstance(page, Gtk.ScrolledWindow) else None
        if adj is None:
            return
        adj.set_value(max(adj.get_lower(), min(adj.get_upper() - adj.get_page_size(), adj.get_value() + delta)))
        where = self._locate(self.dialog.get_focus())
        if where is None:
            return
        content = page.get_child()
        if isinstance(content, Gtk.Viewport):
            content = content.get_child()
        top, bottom, margin = adj.get_value(), adj.get_value() + adj.get_page_size(), 28

        def span(widget):
            at = widget.translate_coordinates(content, 0, 0)
            return (at[1], at[1] + widget.get_allocated_height()) if at else None

        rows = self._visible_rows()
        here = span(rows[where[0]][0])
        if here is None or (here[0] - margin >= top and here[1] + margin <= bottom):
            return
        order = range(len(rows)) if delta > 0 else range(len(rows) - 1, -1, -1)
        for i in order:
            at = span(rows[i][0])
            if (at is not None and at[0] - margin >= top and at[1] + margin <= bottom
                    and self._focusable(rows[i][1])):
                self._focus_row(i, from_below=delta < 0)
                return

    def _focus_row(self, index: int, from_below: bool = False):
        rows = self._visible_rows()
        step = -1 if from_below else 1
        while 0 <= index < len(rows):
            targets = self._focusable(rows[index][1])
            if targets:
                if targets == ["grid"]:
                    children = self.theme_flow.get_children()
                    cols = self.theme_flow.get_max_children_per_line()
                    (children[(len(children) - 1) // cols * cols] if from_below else children[0]).grab_focus()
                else:
                    targets[0].grab_focus()
                return True
            index += step
        return False

    def _to_sidebar(self):
        row = self.nav.get_selected_row()
        if row is not None:
            row.grab_focus()

    def _switch_page(self, delta: int):
        self.stop_edit()
        rows = self.nav.get_children()
        current = self.nav.get_selected_row()
        index = rows.index(current) if current in rows else 0
        target = rows[(index + delta) % len(rows)]
        self.nav.select_row(target)
        target.grab_focus()

    def start_edit(self, widget: Gtk.Widget):
        if self.editing is widget:
            return
        self.stop_edit()
        self.editing = widget
        widget.get_style_context().add_class("settings-editing")
        if isinstance(widget, Gtk.Entry) and not isinstance(widget, Gtk.SpinButton):
            widget.grab_focus_without_selecting()
            widget.set_position(-1)
        self._update_hint()

    def stop_edit(self):
        if self.editing is not None:
            self.editing.get_style_context().remove_class("settings-editing")
            if isinstance(self.editing, Gtk.SpinButton):
                self.editing.update()
            self.editing = None
            self._update_hint()

    def _adjust(self, widget: Gtk.Widget, direction: int):
        if isinstance(widget, Gtk.SpinButton):
            widget.spin(Gtk.SpinType.STEP_FORWARD if direction > 0 else Gtk.SpinType.STEP_BACKWARD, 1)
        elif isinstance(widget, Gtk.Scale):
            adj = widget.get_adjustment()
            adj.set_value(max(adj.get_lower(), min(adj.get_upper(),
                                                   adj.get_value() + direction * adj.get_step_increment())))

    def _set_keynav(self, on: bool) -> bool:
        self.keynav = on
        GLib.idle_add(self._mark_focused_row)
        return False

    def _mark_focused_row(self) -> bool:
        row = None
        focus = self.dialog.get_focus()
        if self.keynav:
            where = self._locate(focus)
            if where is not None and where[1] != "grid":
                row = self._visible_rows()[where[0]][0]
        # the footer's Close, stood on: the app's keyboard ring (theme.py .kb-focus)
        ctx = self.close_btn.get_style_context()
        (ctx.add_class if self.keynav and focus is self.close_btn else ctx.remove_class)("kb-focus")
        if self.theme_flow is not None:
            for child in self.theme_flow.get_children():
                ctx = child.get_style_context()
                (ctx.add_class if self.keynav and child is focus else ctx.remove_class)("settings-theme-focused")
        if row is not self.focused_row:
            if self.focused_row is not None:
                self.focused_row.get_style_context().remove_class("settings-row-focused")
            if row is not None:
                row.get_style_context().add_class("settings-row-focused")
            self.focused_row = row
        return False

    def handle(self, action: str, from_pad: bool = True) -> bool:
        """One navigation step, shared by the keyboard and the controller."""
        if action == "release":
            return True
        if not self.keynav:
            self._set_keynav(True)
        from plunderhub.ui import keynav
        if action in ("up", "down", "left", "right") and keynav.restore(self.dialog):
            return True     # back where it was before the pointer moved
        focus = self.dialog.get_focus()

        if self.editing is not None:
            if action in ("back", "activate"):
                self.stop_edit()
            elif action in ("left", "right", "up", "down") and not (
                    isinstance(self.editing, Gtk.Entry) and not isinstance(self.editing, Gtk.SpinButton)):
                self._adjust(self.editing, 1 if action in ("right", "up") else -1)
            return True

        if action == "prev_tab":
            self._switch_page(-1)
            return True
        if action == "next_tab":
            self._switch_page(1)
            return True
        if action == "settings":
            self.dialog.response(Gtk.ResponseType.CLOSE)
            return True

        if focus is self.close_btn:
            if action == "up":
                self._focus_row(len(self._visible_rows()) - 1, from_below=True)
            elif action in ("left", "back"):
                self._to_sidebar()
            elif action == "activate":
                self.close_btn.clicked()
            return True

        if focus is not None and focus.get_parent() is self.search_results:
            hits = [r for r in self.search_results.get_children() if hasattr(r, "target")]
            i = hits.index(focus) if focus in hits else 0
            if action == "up":
                (self.search if i == 0 else hits[i - 1]).grab_focus()
            elif action == "down" and i + 1 < len(hits):
                hits[i + 1].grab_focus()
            elif action in ("activate", "right"):
                self._on_search_hit(self.search_results, focus)
            elif action == "back":
                self.search.grab_focus()
            return True

        if focus is not None and focus.get_parent() is self.nav or focus is None:
            rows = self.nav.get_children()
            if focus is None:
                # nothing stood on yet (just opened): B closes at once, the
                # rest lands on the sidebar
                if action == "back":
                    self.dialog.response(Gtk.ResponseType.CLOSE)
                else:
                    self._to_sidebar()
                return True
            i = rows.index(focus)
            if action in ("up", "down"):
                if action == "up" and i == 0:
                    self.search.grab_focus()
                    return True
                target = rows[max(0, min(len(rows) - 1, i + (1 if action == "down" else -1)))]
                self.nav.select_row(target)
                target.grab_focus()
            elif action in ("right", "activate"):
                self._focus_row(0)
            elif action == "back":
                self.dialog.response(Gtk.ResponseType.CLOSE)
            return True

        if focus is self.search:
            hits = [r for r in self.search_results.get_children() if hasattr(r, "target")]
            if action in ("down", "activate") and self.search_results.get_visible() and hits:
                hits[0].grab_focus()
                if action == "activate":
                    self._on_search_hit(self.search_results, hits[0])
                return True
            if action == "down":
                self._to_sidebar()
                return True
            if action == "back":
                self._to_sidebar()
                return True
            return False

        where = self._locate(focus)
        if where is None:
            return False
        row_i, col = where
        rows = self._visible_rows()

        if col == "grid":
            children = self.theme_flow.get_children()
            i = children.index(focus)
            cols = self.theme_flow.get_max_children_per_line()
            if action == "left":
                if i % cols == 0:
                    self._to_sidebar()
                else:
                    children[i - 1].grab_focus()
            elif action == "right":
                if i % cols < cols - 1 and i + 1 < len(children):
                    children[i + 1].grab_focus()
            elif action == "up":
                if i >= cols:
                    children[i - cols].grab_focus()
                elif row_i > 0:
                    self._focus_row(row_i - 1, from_below=True)
            elif action == "down":
                if i + cols < len(children):
                    children[i + cols].grab_focus()
                elif i // cols < (len(children) - 1) // cols:
                    children[-1].grab_focus()
                elif not self._focus_row(row_i + 1):
                    self.close_btn.grab_focus()
            elif action == "activate":
                self._choose_theme(focus.theme_name)
            elif action == "back":
                self._to_sidebar()
            return True

        targets = self._focusable(rows[row_i][1])
        control = rows[row_i][1][col]
        col = targets.index(control) if control in targets else 0
        if action == "up":
            if row_i > 0:
                self._focus_row(row_i - 1, from_below=True)
        elif action == "down":
            if not self._focus_row(row_i + 1):
                self.close_btn.grab_focus()
        elif action == "left":
            if col > 0:
                targets[col - 1].grab_focus()
            else:
                self._to_sidebar()
        elif action == "right":
            if col + 1 < len(targets):
                targets[col + 1].grab_focus()
        elif action == "back":
            self._to_sidebar()
        elif action == "activate":
            if isinstance(control, _EDITABLE):
                self.start_edit(control)
            elif isinstance(control, Gtk.ComboBox):
                if from_pad:
                    self.app._open_combo_picker(control, self.dialog)
                else:
                    control.popup()
            elif isinstance(control, Gtk.Switch):
                control.set_active(not control.get_active())
            elif isinstance(control, Gtk.Button):
                control.clicked()
        return True

    _KEY_ACTIONS = {
        Gdk.KEY_Up: "up", Gdk.KEY_Down: "down", Gdk.KEY_Left: "left", Gdk.KEY_Right: "right",
        Gdk.KEY_Return: "activate", Gdk.KEY_KP_Enter: "activate", Gdk.KEY_space: "activate",
        Gdk.KEY_Escape: "back",
    }

    def _on_key(self, _dlg, event) -> bool:
        key = event.keyval
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and key in (Gdk.KEY_Page_Down, Gdk.KEY_Tab):
            return self.handle("next_tab", from_pad=False)
        if ctrl and key in (Gdk.KEY_Page_Up, Gdk.KEY_ISO_Left_Tab):
            return self.handle("prev_tab", from_pad=False)
        if ctrl and key in (Gdk.KEY_f, Gdk.KEY_F):
            self.stop_edit()
            self.search.grab_focus()
            return True
        focus = self.dialog.get_focus()

        # The fps field: arrows change the number, digits can be typed too.
        if isinstance(self.editing, Gtk.SpinButton):
            action = self._KEY_ACTIONS.get(key)
            if action is not None and key != Gdk.KEY_space:
                return self.handle(action, from_pad=False)
            return False
        # Typing into the API key field: only Enter / Esc are ours.
        if isinstance(self.editing, Gtk.Entry):
            if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Escape):
                self.stop_edit()
                return True
            return False
        if focus is self.search:
            if key == Gdk.KEY_Down:
                return self.handle("down", from_pad=False)
            if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
                return self.handle("activate", from_pad=False)
            if key == Gdk.KEY_Escape:
                if self.search.get_text():
                    self.search.set_text("")
                else:
                    self._to_sidebar()
                return True
            return False
        if isinstance(focus, Gtk.Entry) and event.string and event.string.isprintable():
            return True     # not in edit mode yet: typing does nothing
        if key == Gdk.KEY_Escape and (focus is None or focus.get_parent() is self.nav):
            self.dialog.response(Gtk.ResponseType.CLOSE)
            return True
        action = self._KEY_ACTIONS.get(key)
        if action is None:
            return False
        return self.handle(action, from_pad=False)

    def _pad_glyph(self, role: str) -> Optional[str]:
        """The controller's own button for a role, coloured like on the
        pad - or None with no controller plugged in, or a key pressed last."""
        try:
            if self.app._controller_family == "keyboard" or not self.app._joystick_manager.is_connected():
                return None
        except Exception:
            return None
        glyph = self.app._controller_glyph_text(role)
        color = _FACE_COLORS.get(glyph)
        glyph = GLib.markup_escape_text(glyph)
        return f"<span foreground='{color}' weight='bold'>{glyph}</span>" if color else f"<b>{glyph}</b>"

    def _update_hint(self, focus: Optional[Gtk.Widget] = None):
        if focus is None:
            focus = self.dialog.get_focus()
        target = self._control_for(focus)
        enter = "<b>Enter</b>"
        ok = self._pad_glyph("activate") or enter
        done = self._pad_glyph("back") or enter
        if self.editing is not None:
            if isinstance(self.editing, Gtk.Entry) and not isinstance(self.editing, Gtk.SpinButton):
                text = self._("hint_editing_text").format(btn=enter)
            else:
                text = self._("hint_editing_value").format(arrows="<b>←  →</b>", btn=done)
        elif isinstance(focus, Gtk.FlowBoxChild):
            text = self._("hint_apply_theme").format(btn=ok)
        elif isinstance(target, Gtk.Switch):
            text = self._("hint_turn_off" if target.get_active() else "hint_turn_on").format(btn=ok)
        elif isinstance(target, _EDITABLE):
            text = self._("hint_change").format(btn=ok)
        elif isinstance(target, Gtk.ComboBox):
            text = self._("hint_open_list").format(btn=ok)
        elif isinstance(target, Gtk.Button):
            text = self._("hint_press").format(btn=ok)
        elif focus is not None and focus.get_parent() is self.nav:
            text = self._("hint_open_page").format(btn=ok)
        elif focus is not None and focus.get_parent() is self.search_results:
            text = self._("hint_go_there").format(btn=ok)
        else:
            text = ""
        self.hint_label.set_markup(text)
        self.applied_label.set_visible(not getattr(target, "manual_save", False))

    # --- search ----------------------------------------------------------------------

    def _on_search(self, entry: Gtk.SearchEntry):
        query = entry.get_text().strip().lower()
        for child in self.search_results.get_children():
            self.search_results.remove(child)
        if not query:
            self.search_results.hide()
            self.nav.show()
            return
        titles = {p[0]: self._(p[2]) for p in _PAGES}
        hits = [(t, p, w) for t, p, w in self.search_index if query in t.lower()]
        for text, page, widget in hits[:12]:
            row = Gtk.ListBoxRow()
            row.target = (page, widget)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.get_style_context().add_class("settings-search-hit")
            name = Gtk.Label(label=text, xalign=0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            where = Gtk.Label(label=titles[page], xalign=0)
            where.get_style_context().add_class("settings-search-page")
            box.pack_start(name, False, False, 0)
            box.pack_start(where, False, False, 0)
            row.add(box)
            self.search_results.add(row)
        if not hits:
            none = Gtk.Label(label=self._("settings_search_no_results"))
            none.set_margin_top(12)
            none.get_style_context().add_class("settings-search-page")
            self.search_results.add(none)
        self.nav.hide()
        self.search_results.show_all()

    def _on_search_hit(self, _list, row):
        if not hasattr(row, "target"):
            return
        page, widget = row.target
        self.search.set_text("")
        self.goto(page)
        ctx = widget.get_style_context()
        ctx.add_class("settings-flash")

        def focus_it():
            for w, targets in self.rows[page]:
                if w is widget:
                    if targets == ["grid"]:
                        self.theme_flow.get_children()[0].grab_focus()
                    else:
                        focusable = self._focusable(targets)
                        if focusable:
                            focusable[0].grab_focus()
            return False
        GLib.timeout_add(250, focus_it)
        GLib.timeout_add(1400, lambda: (ctx.remove_class("settings-flash"), False)[1])


def _strip_colon(text: str) -> str:
    return text.rstrip(":").rstrip()


def _descendants(widget: Gtk.Widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from _descendants(child)


def _hex_rgb(value: str):
    h = value.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _draw_swatch(widget: Gtk.Widget, cr, palette: Optional[Dict]) -> bool:
    """A tiny window in the theme's colours: sidebar, two text lines and
    an accent button. The system theme gets the desktop's own colours."""
    w, h, r = 118, 64, 10
    system = system_swatch_colors() if palette is None else None
    if system is not None:
        colors = system
    elif palette is None:
        ctx = widget.get_style_context()
        # (in its own state: another one would restyle it for another frame)
        fg = ctx.get_color(ctx.get_state())
        fg_rgb = (fg.red, fg.green, fg.blue)
        colors = {"bg": (0.5, 0.5, 0.5), "bg_raised": (0.42, 0.42, 0.42), "fg": fg_rgb,
                  "fg_dim": tuple(c * 0.7 for c in fg_rgb), "accent": (0.21, 0.52, 0.89)}
    else:
        colors = {k: _hex_rgb(palette[k]) for k in ("bg", "bg_raised", "fg", "fg_dim", "accent")}
    cr.new_sub_path()
    cr.arc(w - r, r, r, -math.pi / 2, 0)
    cr.arc(w - r, h - r, r, 0, math.pi / 2)
    cr.arc(r, h - r, r, math.pi / 2, math.pi)
    cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.set_source_rgb(*colors["bg"])
    cr.fill_preserve()
    cr.clip()
    cr.set_source_rgb(*colors["bg_raised"])
    cr.rectangle(0, 0, 30, h)
    cr.fill()
    cr.set_source_rgb(*colors["fg_dim"])
    for i in range(3):
        cr.rectangle(7, 10 + i * 14, 16, 5)
        cr.fill()
    cr.set_source_rgb(*colors["fg"])
    cr.rectangle(38, 12, 56, 6)
    cr.fill()
    cr.set_source_rgb(*colors["fg_dim"])
    cr.rectangle(38, 24, 40, 5)
    cr.fill()
    cr.set_source_rgb(*colors["accent"])
    cr.rectangle(38, 42, 44, 12)
    cr.fill()
    return False
