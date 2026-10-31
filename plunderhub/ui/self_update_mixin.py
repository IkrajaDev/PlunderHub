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
"""The progress window for the "Auto" update - see core/self_update.py
for what actually happens in each step."""

import math
import threading
import time
import webbrowser
from typing import Optional, Tuple

from plunderhub.constants import APP_ICON_NAME, APP_VERSION
from plunderhub.core import self_update
from plunderhub.core.update_check import release_page_url
from plunderhub.gi_setup import GdkPixbuf, GLib, Gtk
from plunderhub.logging_setup import logger
from plunderhub.theme import _app_icon_file
from plunderhub.ui.progress_card import (
    RING_ICON, LogPanel, ProgressRing, Stepper, card_dialog, pill_buttons, show_final, status_label,
)

_STEP_LABEL_KEYS = {
    self_update.STEP_DOWNLOAD: "update_step_download",
    self_update.STEP_PULL: "update_step_pull",
    self_update.STEP_BUILD: "update_step_build",
    self_update.STEP_INSTALL: "update_step_install",
}
_RESTART_ROW = "restart"

# Steps that can't report how far along they are (makepkg between its
# milestones, pacman, git) still move the bar: it creeps towards the
# end of the step, fast at first and slowing down, so it never visibly
# stalls but also never claims to be done before the step really is.
_CREEP_SECONDS = {
    self_update.STEP_DOWNLOAD: 3.0,
    self_update.STEP_PULL: 4.0,
    self_update.STEP_BUILD: 10.0,
    self_update.STEP_INSTALL: 8.0,
}
_CREEP_CEILING = 0.92
_ANIMATION_INTERVAL_MS = 40
RESTART_COUNTDOWN_S = 3   # seconds before a finished update restarts by itself



class SelfUpdateMixin:

    def _auto_update_availability(self) -> Tuple[str, Optional[str]]:
        """(install method, why Auto can't be used - or None if it can)."""
        method = self_update.detect_install_method()
        if method == self_update.METHOD_UNSUPPORTED:
            return method, self._("update_auto_unavailable_install")
        missing = self_update.missing_tools(method)
        if missing:
            return method, self._("update_auto_missing_tools").format(tools=", ".join(missing))
        return method, None

    def _start_auto_update(self, tag: str, method: str):
        if getattr(self, "_auto_update_running", False):
            return
        self._auto_update_running = True
        ui = _UpdateWindow(self, tag, method)
        ui.start()


class _UpdateWindow:
    """One update run's dialog and the worker thread behind it. Kept
    separate from the App so its many widgets and the animation state
    don't end up as attributes on the main window."""

    def __init__(self, app, tag: str, method: str):
        self.app = app
        self._ = app._
        self.tag = tag
        self.method = method
        self.steps = self_update.steps_for(method)
        self.updater = self_update.Updater(
            method, tag,
            on_step=lambda step: GLib.idle_add(self._on_step, step),
            on_progress=lambda step, frac, status: GLib.idle_add(self._on_progress, step, frac, status),
            on_log=lambda line: GLib.idle_add(self._append_log, line),
        )
        self.current_step: Optional[str] = None
        self.step_started = 0.0
        self.step_fraction = 0.0
        self.shown_fraction = 0.0
        self.finished = False
        self.awaiting_restart = False
        self._animation_id: Optional[int] = None
        self._build()
        app.add_quit_blocker(self)

    # --- for App.request_quit --------------------------------------------

    def quit_line(self) -> str:
        return f"{self.title_label.get_text()} ({int(self.shown_fraction * 100)}%)"

    def quit_stop(self):
        """Its Cancel - once it's installing, that can't be stopped, and
        is waited for."""
        self._cancel()

    def quit_done(self) -> bool:
        return self.finished

    # --- layout --------------------------------------------------------

    def _build(self):
        """A card (see progress_card): the app's icon inside a progress
        ring, the title and versions, the steps in a row, a status line,
        the log behind a Details toggle, and pill buttons."""
        dlg, column = card_dialog(self.app._dialog_transient_parent(), self._("update_progress_title"))
        dlg.connect("delete-event", self._on_delete_event)
        self.dialog = dlg

        self.ring = ProgressRing(self._logo_pixbuf(RING_ICON))
        column.pack_start(self.ring, False, False, 0)
        self.title_label = Gtk.Label(label=self._("update_progress_title"))
        self.title_label.get_style_context().add_class("home-dialog-title")
        column.pack_start(self.title_label, False, False, 0)
        new_version = self.tag if self.tag.startswith("v") else f"v{self.tag}"
        versions = Gtk.Label(label=f"v{APP_VERSION}  →  {new_version}")
        versions.get_style_context().add_class("home-update-versions")
        column.pack_start(versions, False, False, 0)

        self.stepper = Stepper([(step, self._(_STEP_LABEL_KEYS.get(step, "update_step_restart")))
                                for step in self.steps + [_RESTART_ROW]])
        column.pack_start(self.stepper, False, False, 0)

        self.status_label = status_label(self._("update_status_preparing"))
        column.pack_start(self.status_label, False, False, 0)

        self.log = LogPanel(self._("update_details"))
        column.pack_start(self.log.revealer, False, False, 0)
        buttons = Gtk.Box(spacing=8)
        buttons.set_margin_top(16)
        buttons.pack_start(self.log.toggle, False, False, 0)
        self.release_btn, self.cancel_btn, self.close_btn, self.restart_btn = pill_buttons(self.app, (
            (self._("update_open_release_btn"), "ghost"), (self._("dialog_cancel"), "ghost"),
            (self._("dialog_ok"), "accent"), (self._("update_restart_now_btn"), "accent")))
        self.release_btn.connect("clicked", lambda *_: webbrowser.open(release_page_url(self.tag)))
        self.cancel_btn.connect("clicked", lambda *_: self._cancel())
        self.close_btn.connect("clicked", lambda *_: self._close())
        self.restart_btn.connect("clicked", lambda *_: self._restart())
        for btn in (self.restart_btn, self.close_btn, self.cancel_btn, self.release_btn):
            buttons.pack_end(btn, False, False, 0)
        column.pack_start(buttons, False, False, 0)
        self.cancel_btn.show()

    def _logo_pixbuf(self, size: int) -> Optional[GdkPixbuf.Pixbuf]:
        path = _app_icon_file()
        try:
            if path is not None:
                return GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), size, size)
            return Gtk.IconTheme.get_default().load_icon(APP_ICON_NAME, size, Gtk.IconLookupFlags.FORCE_SIZE)
        except Exception as e:
            logger.debug(f"Update dialog: couldn't load the app icon: {e}")
            return None

    def _set_step_state(self, step: str, state: str):
        self.stepper.set_state(step, state)

    # --- running -------------------------------------------------------

    def start(self):
        self.app._enter_dimmed_dialog()
        self.dialog.show_all()
        self.app._install_controller_focus_rings(self.dialog)
        self.app._enable_keyboard_focus_ring_detection(self.dialog)
        self._animation_id = GLib.timeout_add(_ANIMATION_INTERVAL_MS, self._animate)
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            self.updater.run()
        except self_update.UpdateCancelled:
            GLib.idle_add(self._on_cancelled)
            return
        except self_update.UpdateError as e:
            GLib.idle_add(self._on_failed, e.key, e.detail)
            return
        except Exception as e:
            logger.exception("Auto update failed unexpectedly")
            GLib.idle_add(self._on_failed, "update_failed_text", str(e))
            return
        GLib.idle_add(self._on_succeeded)

    def _on_step(self, step: str) -> bool:
        if self.current_step is not None:
            self._set_step_state(self.current_step, "done")
        self.current_step = step
        self.step_started = time.monotonic()
        self.step_fraction = 0.0
        self._set_step_state(step, "active")
        if step == self_update.STEP_INSTALL:
            # From here on the update finishes rather than being left
            # half-installed - see Updater.cancel().
            self.cancel_btn.hide()
            self.status_label.set_text(self._("update_status_auth"))
        return False

    def _on_progress(self, step: str, fraction: Optional[float], status: str) -> bool:
        if step != self.current_step:
            return False
        if fraction is not None:
            self.step_fraction = max(self.step_fraction, fraction)
        if step == self_update.STEP_DOWNLOAD and status:
            status = self._("update_status_downloading").format(size=status)
        if status:
            self.status_label.set_text(status)
        return False

    def _append_log(self, line: str) -> bool:
        self.log.append(line)
        return False

    def _target_fraction(self) -> float:
        if self.finished or self.current_step is None:
            return self.shown_fraction
        elapsed = time.monotonic() - self.step_started
        creep = _CREEP_CEILING * (1.0 - math.exp(-elapsed / _CREEP_SECONDS[self.current_step]))
        within = max(self.step_fraction, creep) if self.step_fraction < 1.0 else 1.0
        return self_update.overall_fraction(self.steps, self.current_step, within)

    def _animate(self) -> bool:
        target = self._target_fraction()
        # Eased rather than set outright, so jumps (a makepkg milestone,
        # a finished step) glide instead of snapping.
        self.shown_fraction += (target - self.shown_fraction) * 0.18
        if abs(target - self.shown_fraction) < 0.001:
            self.shown_fraction = target
        self.ring.tick(self.shown_fraction)
        self.stepper.tick()
        return True

    # --- outcomes ------------------------------------------------------

    def _stop_animation(self):
        if self._animation_id is not None:
            GLib.source_remove(self._animation_id)
            self._animation_id = None

    def _on_succeeded(self) -> bool:
        self.finished = True
        self._stop_animation()
        if self.current_step is not None:
            self._set_step_state(self.current_step, "done")
        self._set_step_state(_RESTART_ROW, "active")
        self.shown_fraction = 1.0
        self.ring.finish("done")
        self.title_label.set_text(self._("update_done_title"))
        # Restarts by itself after a short countdown - or at once, with
        # the button.
        self.restart_in = RESTART_COUNTDOWN_S
        self.status_label.set_text(self._("update_restart_countdown").format(seconds=self.restart_in))
        self._countdown_id = GLib.timeout_add(1000, self._countdown)
        # The new version is already installed, so the only way out is
        # the restart button - closing would leave the old code running.
        self.awaiting_restart = True
        self.dialog.set_deletable(False)
        self.cancel_btn.hide()
        self.restart_btn.show()
        self.restart_btn.grab_focus()
        return False

    def _countdown(self) -> bool:
        self.restart_in -= 1
        if self.restart_in > 0:
            self.status_label.set_text(self._("update_restart_countdown").format(seconds=self.restart_in))
            return True
        self._countdown_id = None
        self._restart()
        return False

    def _restart(self):
        if getattr(self, "_countdown_id", None) is not None:
            GLib.source_remove(self._countdown_id)
            self._countdown_id = None
        if not self_update.spawn_restart(self.method):
            self._set_step_state(_RESTART_ROW, "failed")
            self.status_label.set_text(self._("update_restart_failed_text"))
            self.awaiting_restart = False
            self.dialog.set_deletable(True)
            self.restart_btn.hide()
            self.close_btn.show()
            return
        self._set_step_state(_RESTART_ROW, "done")
        self._teardown()
        # destroy(), not the window's close handler: that one may only
        # hide to the tray, and the old process has to actually exit
        # before the new one can start - see spawn_restart().
        self.app.destroy()

    def _on_failed(self, key: str, detail: str) -> bool:
        self.finished = True
        self._stop_animation()
        if self.current_step is not None:
            self._set_step_state(self.current_step, "failed")
        if detail:
            self._append_log(detail)
        self.title_label.set_text(self._("update_failed_title"))
        show_final(self.status_label, self._(key), error=True)
        self.ring.finish("failed")
        self.log.set_open(True)        # the log says what went wrong
        self.cancel_btn.hide()
        self.release_btn.show()
        self.close_btn.show()
        self.close_btn.grab_focus()
        self.app._auto_update_running = False
        return False

    def _on_cancelled(self) -> bool:
        self.finished = True
        self._stop_animation()
        self.app._auto_update_running = False
        self._close()
        return False

    def _cancel(self):
        if not self.updater.cancellable:
            return
        self.cancel_btn.set_sensitive(False)
        self.status_label.set_text(self._("update_status_cancelling"))
        self.updater.cancel()

    def _on_delete_event(self, *_args) -> bool:
        # Also reached via Escape, which Gtk.Dialog turns into a delete.
        if self.awaiting_restart:
            return True
        if self.finished:
            self._close()
        else:
            self._cancel()
        return True

    def _teardown(self):
        self.app.remove_quit_blocker(self)
        self._stop_animation()
        self.app._exit_dimmed_dialog()
        self.dialog.destroy()

    def _close(self):
        self._teardown()
        self.app._auto_update_running = False

