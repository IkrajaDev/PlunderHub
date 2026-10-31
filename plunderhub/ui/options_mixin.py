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


import json
import shutil
import threading
import urllib.error
import urllib.request
from typing import Optional

from plunderhub.constants import (
    CACHE_DIR, CONFIG_DIR, CONSOLE_UI_SIZE_LABEL_KEYS,
    COVER_SIZE_LABEL_KEYS, FONT_SCALE_LABEL_KEYS, FONT_SCALE_ORDER, GAME_ICON_SIZE_LABEL_KEYS,
)
from plunderhub.core import self_update
from plunderhub.core.autostart import disable_autostart
from plunderhub.core.persistence import save_settings
from plunderhub.core.secret_store import clear_api_key
from plunderhub.core.utils import HEADERS
from plunderhub.gi_setup import Gdk, GLib, Gtk
from plunderhub.logging_setup import logger
from plunderhub.theme import THEME_ORDER
from plunderhub.ui.settings_window import SettingsWindow


class OptionsMixin:

    def _flash_highlight_widget(self, widget: Optional[Gtk.Widget]):
        if widget is None:
            return
        ctx = widget.get_style_context()
        ctx.add_class("settings-search-highlight")

        def _remove():
            ctx.remove_class("settings-search-highlight")
            return False
        GLib.timeout_add(1600, _remove)

    def _settings_heading(self, text: str, top_margin: int = 4, icon_name: Optional[str] = None) -> Gtk.Box:
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        if top_margin > 0:
            rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            rule.get_style_context().add_class("settings-section-rule")
            rule.set_margin_top(top_margin + 4)
            wrapper.pack_start(rule, False, False, 0)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top((top_margin if top_margin > 0 else 0) + 4)
        row.set_margin_bottom(2)
        if icon_name is not None:
            icon = self._icon_image(icon_name, Gtk.IconSize.BUTTON)
            icon.get_style_context().add_class("settings-heading")
            row.pack_start(icon, False, False, 0)
        lbl = Gtk.Label(label=text)
        lbl.set_xalign(0)
        lbl.get_style_context().add_class("settings-heading")
        row.pack_start(lbl, False, False, 0)
        wrapper.pack_start(row, False, False, 0)
        wrapper.heading_label = lbl
        return wrapper

    def _check_with_icon(self, label_text: str, icon_name: str) -> Gtk.CheckButton:
        """A Gtk.CheckButton with an icon-then-label row as its content,
        instead of set_image()/set_always_show_image(). Needed because
        GtkCheckButton's own "check" indicator sits right up against
        whatever comes after it with no meaningful CSS handle to add
        breathing room there (margin/padding on the "check" node,
        -GtkButton-image-spacing, -GtkCheckButton-indicator-spacing -
        all confirmed, live, to have zero visible effect against this
        widget in this GTK version) - a custom Gtk.Box with its own
        spacing is the only reliable way to actually control the gap
        between the icon and the label (reported directly: Settings
        felt cramped, everything blending together). Safe to do
        instead of set_image() because nothing here ever calls
        set_label()/get_label() on these widgets to update them later
        - a language change closes and fully rebuilds the whole
        Settings dialog from scratch (see on_save_settings) rather
        than patching labels in place.

        set_margin_start on the CONTENT box (not the "check" node -
        that part's still fixed) is a second, separate fix: even with
        the icon-label gap solved, the indicator-to-icon gap was still
        the GtkCheckButton's own tight built-in spacing (reported
        directly, with a screenshot, as still-cramped even on rows
        already converted to this content-Box approach). Since this
        margin lives on OUR OWN widget rather than GTK's internal
        indicator, set_margin_start actually works here, unlike every
        CSS attempt against the indicator itself - matched to the same
        10px as the icon-label spacing so both gaps read as equal."""
        cb = Gtk.CheckButton()
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        content.set_margin_start(10)
        content.pack_start(self._icon_image(icon_name, Gtk.IconSize.BUTTON), False, False, 0)
        content.pack_start(Gtk.Label(label=label_text), False, False, 0)
        cb.add(content)
        return cb

    def _radio_with_icon(self, group: Optional[Gtk.RadioButton], label_text: str, icon_name: str) -> Gtk.RadioButton:
        """See _check_with_icon's own docstring - same reasoning,
        same fix (including the set_margin_start indicator-to-icon
        gap fix), for Gtk.RadioButton instead."""
        r = Gtk.RadioButton.new_from_widget(group)
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        content.set_margin_start(10)
        content.pack_start(self._icon_image(icon_name, Gtk.IconSize.BUTTON), False, False, 0)
        content.pack_start(Gtk.Label(label=label_text), False, False, 0)
        r.add(content)
        return r

    def _disable_scale_scroll(self, scale: Gtk.Scale):
        def on_scroll(widget, event):
            parent = widget.get_parent()
            while parent is not None and not isinstance(parent, Gtk.ScrolledWindow):
                parent = parent.get_parent()
            if parent is not None:
                vadj = parent.get_vadjustment()
                if vadj is not None:
                    success, _dx, dy = event.get_scroll_deltas()
                    if not success:
                        if event.direction == Gdk.ScrollDirection.UP:
                            dy = -1.0
                        elif event.direction == Gdk.ScrollDirection.DOWN:
                            dy = 1.0
                        else:
                            dy = 0.0
                    step = vadj.get_step_increment() or 20
                    new_value = vadj.get_value() + dy * step * 3
                    upper = max(vadj.get_lower(), vadj.get_upper() - vadj.get_page_size())
                    vadj.set_value(max(vadj.get_lower(), min(new_value, upper)))
            return True
        scale.connect("scroll-event", on_scroll)

    def _make_info_icon(self, tooltip_text: str) -> Gtk.Image:
        icon = self._icon_image("dialog-information-symbolic", Gtk.IconSize.BUTTON)
        icon.set_tooltip_text(tooltip_text)
        icon.get_accessible().set_name(tooltip_text)
        return icon

    def show_options(self, initial_tab: Optional[str] = None):
        if self._options_dialog is not None:
            self._options_dialog.present()
            if self._options_dialog_goto_tab is not None:
                self._options_dialog_goto_tab(initial_tab)
            return
        SettingsWindow(self, initial_tab or self._settings_last_tab).run()

    def _delete_full_config(self, options_dialog: Gtk.Dialog):
        # Not under work that's still writing there (a saves restore, a
        # Proton unpack, covers coming in): it'd be cut short, or bring
        # the old config back into the fresh one.
        busy = [line or "" for line, _stop, done in self._running_work() if not done()]
        if self._download_thread is not None and self._download_thread.is_alive():
            busy.append(self._("download_running"))
        if busy:
            self._show_info_dialog(self._("sink_wait_work") + "\n\n" + "\n".join(b for b in busy if b),
                                   message_type=Gtk.MessageType.WARNING)
            return
        # Keep / Back up first / Sink it - a backup made from here comes
        # back to the same question, now saying it's safe.
        text = self._("confirm_delete_config_text")
        while True:
            response = self._card_dialog(
                self._("confirm_delete_config_heading"), text,
                [(self._("dialog_keep_btn"), Gtk.ResponseType.CANCEL, "ghost"),
                 (self._("sink_backup_first"), Gtk.ResponseType.APPLY, "ghost"),
                 (self._("dialog_sink_btn"), Gtk.ResponseType.OK, "danger")],
                tone="danger", default=Gtk.ResponseType.CANCEL, transient_for=options_dialog)
            if response == Gtk.ResponseType.APPLY:
                if self._export_settings(options_dialog):
                    text = self._("sink_backed_up") + "\n\n" + self._("confirm_delete_config_text")
                continue
            if response != Gtk.ResponseType.OK:
                return
            break
        # "Run on startup" lives outside the config folder - left in
        # place, a sunk PlunderHub would still start at every login.
        disable_autostart()
        # The API key can live in the OS keyring (libsecret) rather
        # than settings.json, since that's the preferred storage path
        # whenever it's available - shutil.rmtree(CONFIG_DIR) below
        # has no way to reach into the keyring, so without this a
        # "full delete" would silently leave the key behind, only for
        # it to reappear the next time the app starts and migrates/
        # reads it back in. Already safe to call unconditionally -
        # it's a no-op if there's no keyring service or no stored key.
        clear_api_key()
        try:
            shutil.rmtree(CONFIG_DIR)
        except Exception as e:
            logger.error(f"Failed to delete config folder: {e}")
            self._show_info_dialog(f"{self._('delete_config_failed')}: {e}", message_type=Gtk.MessageType.ERROR)
            return
        # Covers/icons now live under CONFIG_DIR, so the line above already
        # sank them too - this is just a leftover-cleanup pass for anything
        # still sitting in the old ~/.cache/plunderhub/ location (e.g. from
        # before this build's cache->config move finished migrating it).
        try:
            if CACHE_DIR.exists():
                shutil.rmtree(CACHE_DIR)
        except Exception as e:
            logger.error(f"Failed to delete leftover cache folder: {e}")
        # Then straight back up, fresh - not just gone.
        self._card_dialog(self._("delete_config_done"), self._("delete_config_restart_text"),
                          [(self._("restart_btn"), Gtk.ResponseType.OK, "accent")], tone="success",
                          transient_for=options_dialog)
        options_dialog.response(Gtk.ResponseType.CLOSE)
        self_update.spawn_relaunch()
        self.destroy()

    def _open_config_folder(self):
        self._show_folder(CONFIG_DIR, transient_for=getattr(self, "_options_dialog", None))

    def _cover_size_label_text(self, size: int) -> str:
        key = COVER_SIZE_LABEL_KEYS.get(size)
        return self._(key) if key else str(size)

    def _console_ui_size_label_text(self, size: int) -> str:
        key = CONSOLE_UI_SIZE_LABEL_KEYS.get(size)
        return self._(key) if key else str(size)

    def _game_icon_size_label_text(self, size: int) -> str:
        key = GAME_ICON_SIZE_LABEL_KEYS.get(size)
        return self._(key) if key else str(size)

    @staticmethod
    def _snap_scale_value(value: float, step: int, min_v: int, max_v: int) -> int:
        snapped = round(value / step) * step
        return max(min_v, min(max_v, snapped))

    def _on_scale_change_value(self, scale, scroll_type, value, step: int, min_v: int, max_v: int) -> bool:
        scale.set_value(self._snap_scale_value(value, step, min_v, max_v))
        return True

    def _snap_scale_and_block(self, scale, handler_func, step: int, min_v: int, max_v: int) -> int:
        val = scale.get_value()
        snapped = self._snap_scale_value(val, step, min_v, max_v)
        if int(val) != snapped:
            scale.handler_block_by_func(handler_func)
            scale.set_value(snapped)
            scale.handler_unblock_by_func(handler_func)
        return snapped

    def _on_cover_scale_change_value(self, scale, scroll_type, value) -> bool:
        return self._on_scale_change_value(scale, scroll_type, value, 50, 200, 500)

    def _on_cover_scale_value_changed(self, scale):
        snapped = self._snap_scale_and_block(scale, self._on_cover_scale_value_changed, 50, 200, 500)
        self.cover_size_label.set_text(self._cover_size_label_text(snapped))

    def _on_console_cover_scale_change_value(self, scale, scroll_type, value) -> bool:
        return self._on_scale_change_value(scale, scroll_type, value, 50, 200, 500)

    def _on_console_cover_scale_value_changed(self, scale):
        snapped = self._snap_scale_and_block(scale, self._on_console_cover_scale_value_changed, 50, 200, 500)
        self.console_cover_size_label.set_text(self._cover_size_label_text(snapped))

    def _on_console_ui_scale_change_value(self, scale, scroll_type, value) -> bool:
        return self._on_scale_change_value(scale, scroll_type, value, 50, 100, 400)

    def _on_console_ui_scale_value_changed(self, scale):
        snapped = self._snap_scale_and_block(scale, self._on_console_ui_scale_value_changed, 50, 100, 400)
        self.console_ui_scale_label.set_text(self._console_ui_size_label_text(snapped))
        self._apply_console_ui_scale(snapped)

    def _on_game_icon_scale_change_value(self, scale, scroll_type, value) -> bool:
        return self._on_scale_change_value(scale, scroll_type, value, 1, 0, len(GAME_ICON_SIZE_LABEL_KEYS) - 1)

    def _on_game_icon_scale_value_changed(self, scale):
        game_icon_size_order = sorted(GAME_ICON_SIZE_LABEL_KEYS)
        snapped = self._snap_scale_and_block(
            scale, self._on_game_icon_scale_value_changed, 1, 0, len(game_icon_size_order) - 1)
        actual_size = game_icon_size_order[snapped]
        self.game_icon_size_label.set_text(self._game_icon_size_label_text(actual_size))

    def _on_font_scale_change_value(self, scale, scroll_type, value) -> bool:
        return self._on_scale_change_value(scale, scroll_type, value, 1, 0, len(FONT_SCALE_ORDER) - 1)

    def _on_font_scale_value_changed(self, scale):
        snapped = self._snap_scale_and_block(scale, self._on_font_scale_value_changed, 1, 0, len(FONT_SCALE_ORDER) - 1)
        key = FONT_SCALE_ORDER[snapped]
        self.font_scale_label.set_text(self._(FONT_SCALE_LABEL_KEYS[key]))
        self._apply_font_scale(key)

    def on_save_settings(self):
        """Reads every control in the Settings window and applies it -
        called (debounced) after each change, there is no Save button.
        The API key is the exception: it's only stored by its own Save
        button (on_save_api_key)."""
        old_enabled = set(self.enabled_launchers)

        new_show = self.show_covers_check.get_active()
        new_auto_close = self.auto_close_check.get_active()
        new_auto_minimize = self.auto_minimize_check.get_active()
        next_workspace_check = getattr(self, "next_workspace_check", None)
        new_next_workspace = (next_workspace_check.get_active() if next_workspace_check is not None
                              else self.launch_on_next_workspace)
        new_launch_mode = "confirm" if self.launch_mode_combo.get_active() == 1 else "instant"
        new_size = int(self.cover_size_scale.get_value())
        new_console_size = int(self.console_cover_size_scale.get_value())
        lang_idx = self.lang_combo.get_active()
        try:
            new_lang = self.lang_combo_codes[lang_idx]
        except (IndexError, TypeError):
            new_lang = "en"
        font_idx = max(0, min(len(FONT_SCALE_ORDER) - 1, round(self.font_scale_scale.get_value())))
        new_font_scale = FONT_SCALE_ORDER[font_idx]
        new_console_ui_scale = int(max(100, min(400, round(self.console_ui_scale_scale.get_value() / 50) * 50)))
        _game_icon_size_order = sorted(GAME_ICON_SIZE_LABEL_KEYS)
        _game_icon_idx = max(0, min(len(_game_icon_size_order) - 1, round(self.game_icon_size_scale.get_value())))
        new_game_icon_size = _game_icon_size_order[_game_icon_idx]
        new_show_bubbles = self.show_bubbles_check.get_active()
        new_pause_animations_when_idle = self.pause_animations_check.get_active()
        new_dim_background_for_dialogs = self.dim_dialogs_check.get_active()
        new_use_tray = self.tray_check.get_active() or new_auto_minimize
        new_run_on_startup = self.run_on_startup_check.get_active()
        new_scroll_top_button_enabled = self.scroll_top_btn_check.get_active()
        new_update_check_enabled = self.update_check_enabled_check.get_active()
        new_show_playtime = self.show_playtime_check.get_active()
        new_show_game_titles = self.show_game_titles_check.get_active()
        new_show_card_menu = self.show_card_menu_check.get_active()
        new_theme = self._settings_theme_choice if self._settings_theme_choice in THEME_ORDER else "plunderhub"
        new_enabled = {name for name, check in self.source_toggles.items() if check.get_active()}

        self.settings["show_covers"] = new_show
        self.settings["auto_close"] = new_auto_close
        self.settings["minimize_on_launch"] = new_auto_minimize
        self.settings["launch_on_next_workspace"] = new_next_workspace
        self.settings["launch_mode"] = new_launch_mode
        self.settings["cover_size"] = new_size
        self.settings["console_cover_size"] = new_console_size
        self.settings["console_ui_scale"] = new_console_ui_scale
        self.settings["game_icon_size"] = new_game_icon_size
        self.settings["lang"] = new_lang
        self.settings["font_scale"] = new_font_scale
        self.settings["show_bubbles"] = new_show_bubbles
        self.settings["pause_animations_when_idle"] = new_pause_animations_when_idle
        self.settings["dim_background_for_dialogs"] = new_dim_background_for_dialogs
        # the frame-rate modes the switch replaced
        self.settings.pop("animated_cover_mode", None)
        self.settings.pop("animated_cover_custom_fps", None)
        self.settings["enabled_launchers"] = list(new_enabled)
        self.settings["launcher_order"] = self.launcher_order
        self.settings["game_order"] = self.game_order
        self.settings["all_game_order"] = self.all_game_order
        self.settings["use_tray"] = new_use_tray
        self.settings["run_on_startup"] = new_run_on_startup
        self.settings["scroll_top_button_enabled"] = new_scroll_top_button_enabled
        self.settings["update_check_enabled"] = new_update_check_enabled
        self.settings["show_playtime"] = new_show_playtime
        self.settings["show_game_titles"] = new_show_game_titles
        self.settings["show_card_menu_button"] = new_show_card_menu
        self.settings["theme"] = new_theme
        save_settings(self.settings)

        tray_changed = new_use_tray != self.use_tray
        run_on_startup_changed = new_run_on_startup != self.run_on_startup

        self.lang = new_lang
        self.auto_close = new_auto_close
        self.minimize_on_launch = new_auto_minimize
        self.launch_on_next_workspace = new_next_workspace
        self.launch_mode = new_launch_mode
        self.show_covers = new_show
        self.cover_size = new_size
        self.console_cover_size = new_console_size
        self.console_ui_scale = new_console_ui_scale
        self.game_icon_size = new_game_icon_size
        self.font_scale = new_font_scale
        self.show_bubbles = new_show_bubbles
        self.pause_animations_when_idle = new_pause_animations_when_idle
        self._update_decorative_animation_pause_state()
        self.dim_background_for_dialogs = new_dim_background_for_dialogs
        if getattr(self, "_dimming_dialog_open", False) and hasattr(self, "dialog_dim_scrim"):
            if self.dim_background_for_dialogs:
                if self.dialog_dim_scrim.get_parent() is None:
                    self.main_overlay.add_overlay(self.dialog_dim_scrim)
                    self.main_overlay.set_overlay_pass_through(self.dialog_dim_scrim, True)
                self.dialog_dim_scrim.show()
            elif self.dialog_dim_scrim.get_parent() is not None:
                self.main_overlay.remove(self.dialog_dim_scrim)
        self.enabled_launchers = new_enabled
        self.use_tray = new_use_tray
        self.run_on_startup = new_run_on_startup
        self.scroll_top_button_enabled = new_scroll_top_button_enabled
        if not self.scroll_top_button_enabled:
            self._set_scroll_top_visible(False)
        else:
            self._on_main_scroll_value_changed(self.scroller.get_vadjustment())
        self.update_check_enabled = new_update_check_enabled
        self.show_playtime = new_show_playtime
        self.show_game_titles = new_show_game_titles
        self.show_card_menu_button = new_show_card_menu
        self.theme = new_theme
        self._init_strings()
        self._refresh_static_ui_language()
        self._apply_theme(self.theme)
        self._apply_font_scale(self.font_scale)
        self._apply_console_ui_scale(self.console_ui_scale)
        if hasattr(self, "bubble_canvas"):
            self._redraw_all_bubbles()

        if tray_changed:
            if self.use_tray:
                self._setup_tray_icon()
            else:
                self._teardown_tray_icon()

        if run_on_startup_changed:
            from plunderhub.core.autostart import disable_autostart, enable_autostart
            if self.run_on_startup:
                if not enable_autostart():
                    # Couldn't write the autostart file (read-only
                    # home dir, unusual XDG setup, etc.) - fall back
                    # to "off" rather than leave the setting claiming
                    # something that didn't actually happen. The
                    # checkbox itself picks this up correctly next
                    # time Settings is reopened, since it's always
                    # initialized from self.run_on_startup.
                    self.run_on_startup = False
                    self.settings["run_on_startup"] = False
                    save_settings(self.settings)
            else:
                disable_autostart()

        if new_enabled != old_enabled:
            self.reload()
        else:
            self._render_launchers()
        self._pending_reload = False

    def _on_api_key_visibility_toggle(self, entry, icon_pos, event):
        visible = entry.get_visibility()
        entry.set_visibility(not visible)
        if visible:
            entry.set_icon_from_pixbuf(Gtk.EntryIconPosition.SECONDARY, self._icon_pixbuf("view-conceal-symbolic", 16))
            entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, self._("api_key_show_tooltip"))
        else:
            entry.set_icon_from_pixbuf(Gtk.EntryIconPosition.SECONDARY, self._icon_pixbuf("view-reveal-symbolic", 16))
            entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, self._("api_key_hide_tooltip"))

    def on_test_api_key(self, button):
        key = self.api_entry.get_text().strip()
        if not key:
            self._show_info_dialog(self._("no_api_key"), message_type=Gtk.MessageType.WARNING)
            return
        # The request can take up to its 10s timeout, so it runs off the
        # UI thread - on it, the whole window froze until it returned.
        button.set_sensitive(False)
        threading.Thread(target=self._test_api_key_worker, args=(key, button), daemon=True).start()

    def _test_api_key_worker(self, key: str, button):
        url = "https://www.steamgriddb.com/api/v2/search/autocomplete/witcher"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", **HEADERS})
        error_detail = None
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
            success = isinstance(data, dict) and bool(data.get("success", False))
            if not success:
                errors = data.get("errors") if isinstance(data, dict) else None
                error_detail = "; ".join(map(str, errors)) if isinstance(errors, list) and errors \
                    else "unexpected response"
        except urllib.error.HTTPError as e:
            success = False
            error_detail = f"HTTP {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            success = False
            error_detail = f"Network error: {e.reason}"
        except Exception as e:
            success = False
            error_detail = f"Unexpected error: {e}"
        GLib.idle_add(self._show_api_key_test_result, success, error_detail, button)

    def _show_api_key_test_result(self, success: bool, error_detail: Optional[str], button) -> bool:
        button.set_sensitive(True)
        if success:
            self._show_info_dialog(self._("test_success"), icon_name="emblem-ok-symbolic",
                                   heading=self._("test_success_heading"))
        else:
            self._show_info_dialog(self._("test_fail").format(detail=error_detail),
                                   message_type=Gtk.MessageType.ERROR, heading=self._("test_fail_heading"))
        return False

    def on_save_api_key(self, button):
        new_key = self.api_entry.get_text().strip()
        self._save_api_key(new_key)
        save_settings(self.settings)
        self._update_cover_hint_bar_visibility()
        if new_key:
            self._show_info_dialog(self._("api_key_saved"), icon_name="emblem-ok-symbolic",
                                   heading=self._("api_key_saved_heading"))
        else:
            self._show_info_dialog(self._("api_key_removed"), heading=self._("api_key_removed_heading"))
        if new_key and self.show_covers:
            # what the launchers had no art for: asked, never fetched unasked
            self._offer_missing_art_download(getattr(self, "_options_dialog", None))
