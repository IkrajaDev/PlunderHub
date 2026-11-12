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

"""The per-game "Launch options" dialog (context menu, top entry).

Values are read from the launcher that owns the game and written back to
it (see core/launch_options.py), so they're the same options the
launcher itself shows and uses."""

import copy
import os
import signal
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from plunderhub.core.launch_options import (
    QUALITY_PRESETS, GamescopeOptions, LaunchOptions, LaunchOptionsBackend, SteamBackend, _process_running,
    backend_for,
    detect_preset, drm_connected_modes, launcher_pids, parse_command_line, preset_game_resolution,
    steam_shutdown_invocations,
)
from plunderhub.core.models import GameItem
from plunderhub.core.persistence import save_settings
from plunderhub.core.scan_paths import LAUNCHER_FLATPAK_IDS, resolve_launcher_invocation
from plunderhub.gi_setup import Gdk, GLib, Gtk
from plunderhub.logging_setup import logger

_STEAM_SHUTDOWN_TIMEOUT_S = 30


class LaunchOptionsMixin:

    def _launch_options_supported(self, game: GameItem) -> bool:
        return backend_for(game) is not None

    # --- pages -------------------------------------------------------------------

    def _lo_monitor_resolution(self) -> Optional[Tuple[int, int]]:
        """The monitor's real resolution: the kernel's mode when there's a
        single display (exact even under fractional scaling), otherwise the
        monitor showing PlunderHub, from GTK."""
        modes = drm_connected_modes()
        if len(modes) == 1:
            return modes[0]
        try:
            display = Gdk.Display.get_default()
            window = self.get_window() if hasattr(self, "get_window") else None
            monitor = display.get_monitor_at_window(window) if window is not None else None
            if monitor is None:
                monitor = display.get_primary_monitor() or display.get_monitor(0)
            geometry, scale = monitor.get_geometry(), monitor.get_scale_factor()
            return geometry.width * scale, geometry.height * scale
        except Exception as e:
            logger.debug(f"Couldn't detect the monitor resolution: {e}")
            return modes[0] if modes else None

    def _lo_monitor_refresh(self) -> int:
        """The refresh rate (Hz) of the monitor showing PlunderHub - 0
        when GTK doesn't say."""
        try:
            display = Gdk.Display.get_default()
            window = self.get_window() if hasattr(self, "get_window") else None
            monitor = display.get_monitor_at_window(window) if window is not None else None
            if monitor is None:
                monitor = display.get_primary_monitor() or display.get_monitor(0)
            return int(round(monitor.get_refresh_rate() / 1000))
        except Exception as e:
            logger.debug(f"Couldn't read the monitor's refresh rate: {e}")
            return 0

    def _lo_context(self):
        from plunderhub.core import launch_defaults
        if getattr(self, "_lo_ctx", None) is None:
            self._lo_ctx = launch_defaults.context(self._lo_monitor_refresh())
        return self._lo_ctx

    def _ask_about_new_games(self, games: List[GameItem]) -> None:
        """A game new to PlunderHub: asks once whether it should get the
        recommended launch options and Proton-GE Latest (see
        core/launch_defaults); nothing is written without the answer.
        Never in a self-test, which mustn't write to the launchers."""
        import sys
        import threading
        from plunderhub.core import launch_defaults
        from plunderhub.ui.proton_page import latest_choice
        if "--self-test" in sys.argv or getattr(self, "_lo_defaults_running", False):
            return
        self._lo_defaults_running = True
        known = self.settings.get(launch_defaults.SETTINGS_KEY)

        def work():
            try:
                after, ask = launch_defaults.new_games(games, known)
                if self.settings.get(launch_defaults.AUTO_KEY, False):
                    # switched on in Settings: no question. A game whose launcher
                    # can't be written now stays unknown, for a later scan.
                    ctx = self._lo_context()
                    for game in ask:
                        if launch_defaults.apply_answer(game, ctx, True, None):
                            after["games"] = sorted(set(after["games"]) | {game.uid})
                    ask = []
                # a store launcher's game shares its launcher's prefix, and so its Proton
                targets = {g.uid: "" if g.store_platform == "launcher-game" else latest_choice(g) for g in ask}
            except Exception as e:
                logger.warning(f"Looking for new games: {e}")
                after, ask, targets = known, [], {}
            GLib.idle_add(done, after, ask, targets)

        def done(after, ask, targets) -> bool:
            if after != known and after is not None:
                self.settings[launch_defaults.SETTINGS_KEY] = after
                save_settings(self.settings)
            # Steam running only for the Treasure Map's install: asked when that's
            # over and Steam is closed again, so it needs no extra restart
            view = getattr(self, "_tm_view", None)
            if view is not None and view.steam_held():
                ask = [g for g in ask if g.launcher != "Steam"]
            # hidden in the tray or in Console Mode: asked at a later scan
            if ask and self.get_visible() and not self.console_box.get_mapped():
                self._new_games_dialog(ask, targets)
                now = self.settings[launch_defaults.SETTINGS_KEY]
                now["games"] = sorted(set(now["games"]) | {g.uid for g in ask})
                save_settings(self.settings)
            self._lo_defaults_running = False
            return False
        threading.Thread(target=work, daemon=True, name="plunderhub-launch-defaults").start()

    def _new_game_tile(self, icon: str, title: str, sub: str) -> Gtk.ToggleButton:
        """A pick of the new game dialog: on by default, the check shows it."""
        from plunderhub.icons import get_icon_pixbuf
        from plunderhub.ui.home_mixin import _accent_rgb
        hex_accent = "#%02x%02x%02x" % tuple(round(c * 255) for c in _accent_rgb(self))
        tile = Gtk.ToggleButton()
        tile.set_active(True)
        tile.get_style_context().add_class("newgame-tile")
        row = Gtk.Box(spacing=14)
        row.pack_start(Gtk.Image.new_from_pixbuf(get_icon_pixbuf(icon, 26, hex_accent)), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        head = Gtk.Label(label=title, xalign=0)
        head.get_style_context().add_class("newgame-tile-title")
        line = Gtk.Label(label=sub, xalign=0)
        line.set_line_wrap(True)
        line.set_max_width_chars(40)
        line.get_style_context().add_class("dim-label")
        texts.pack_start(head, False, False, 0)
        texts.pack_start(line, False, False, 0)
        row.pack_start(texts, True, True, 0)
        check = Gtk.Image.new_from_pixbuf(get_icon_pixbuf("object-select-symbolic", 20, hex_accent))
        row.pack_end(check, False, False, 0)
        tile.connect("toggled", lambda t: check.set_opacity(1.0 if t.get_active() else 0.0))
        tile.add(row)
        return tile

    def _new_game_art(self, game: GameItem) -> Gtk.DrawingArea:
        """The game's cover with a soft glow of the accent colour."""
        from plunderhub.ui.home_mixin import _accent_rgb, _rounded
        w, h, room = 165, 220, 16
        # _resolve_cover_pixbuf makes the screen's own pixels itself (marked
        # HiDPI): asked for w x h logical, drawn at logical_size
        cached = self._find_cached_artwork(self.covers_dir, game.uid)
        pixbuf = self._resolve_cover_pixbuf(w, h, cached, game_title=game.title)
        area = Gtk.DrawingArea()
        area.set_size_request(w + 2 * room, h + 2 * room)
        area.set_valign(Gtk.Align.CENTER)
        gone = []
        area.connect("destroy", lambda *_a: gone.append(True))

        def art_arrived() -> bool:
            # the question can come a moment before the game's art does
            nonlocal pixbuf
            if gone:
                return False
            found = self._find_cached_artwork(self.covers_dir, game.uid)
            if found is None:
                return True
            pixbuf = self._resolve_cover_pixbuf(w, h, found, game_title=game.title)
            area.queue_draw()
            return False
        if cached is None:
            GLib.timeout_add(400, art_arrived)

        def draw(widget, cr):
            rgb = _accent_rgb(widget)
            for grow in range(room, 0, -2):
                _rounded(cr, room - grow, room - grow, w + 2 * grow, h + 2 * grow, 12 + grow)
                cr.set_source_rgba(*rgb, 0.04)
                cr.fill()
            _rounded(cr, room, room, w, h, 12)
            cr.clip()
            cr.translate(room, room)
            from plunderhub.gi_setup import logical_size
            pw, ph = logical_size(pixbuf)
            cr.scale(w / pw, h / ph)
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
            cr.paint()
            return False
        area.connect("draw", draw)
        area.show()
        return area

    def _new_games_dialog(self, ask: List[GameItem], targets: Dict[str, str]) -> None:
        import threading
        from plunderhub.core import launch_defaults
        _ = self._
        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        extra.set_margin_top(8)
        recommended = self._new_game_tile("rocket-symbolic", _("lo_new_recommended"), _("lo_new_recommended_tip"))
        extra.pack_start(recommended, False, False, 0)
        proton = None
        if any(targets.values()):
            proton = self._new_game_tile("wine-symbolic", _("lo_new_proton"), _("lo_new_proton_sub"))
            extra.pack_start(proton, False, False, 0)
        names = ", ".join(g.title for g in ask[:5]) + ("…" if len(ask) > 5 else "")
        heading = _("lo_new_title_one").format(title=ask[0].title) if len(ask) == 1 \
            else _("lo_new_title_many").format(n=len(ask))
        text = _("lo_new_text") if len(ask) == 1 else f"{names}\n{_('lo_new_text')}"
        running = sorted({g.launcher for g in ask if self._launcher_is_running(g.launcher)})
        if running:
            note = Gtk.Label(label=_("lo_new_restart_note").format(launcher=", ".join(running)), xalign=0)
            note.set_line_wrap(True)
            note.set_max_width_chars(46)
            note.get_style_context().add_class("newgame-restart")
            extra.pack_start(note, False, False, 0)
        extra.show_all()
        answer = self._card_dialog(heading, text, [(_("lo_new_skip"), Gtk.ResponseType.CANCEL, "ghost"),
                                                   (_("lo_new_apply_restart" if running else "lo_new_apply"),
                                                    Gtk.ResponseType.OK, "accent")],
                                   extra=extra, art=self._new_game_art(ask[0]), guard_ms=1200)
        use_recommended = recommended.get_active()
        use_proton = proton is not None and proton.get_active()
        if answer != Gtk.ResponseType.OK or not (use_recommended or use_proton):
            return
        ctx = self._lo_context()
        # closed first (the launchers keep these files in memory), started again after
        closed = [launcher for launcher in running if self.close_launcher_asking(launcher, self)]
        left_running = set(running) - set(closed)
        commands = [list(s["cmd"]) for s in getattr(self, "_cached_launchers", []) or []
                    if s.get("name") in closed and s.get("cmd")]

        def work():
            for game in ask:
                if game.launcher not in left_running:
                    launch_defaults.apply_answer(game, ctx, use_recommended,
                                                 targets.get(game.uid) if use_proton else None)
            for command in commands:
                try:
                    subprocess.Popen(command, start_new_session=True, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                except OSError as e:
                    logger.warning(f"Couldn't start {command[0]} again: {e}")
        threading.Thread(target=work, daemon=True, name="plunderhub-launch-defaults").start()

    def _lo_wire_resolution_presets(self, ui: Dict, monitor: Optional[Tuple[int, int]]) -> None:
        """Turning gamescope on fills both resolutions from the monitor;
        picking a quality preset fills them for that preset; typing a
        resolution by hand switches the preset to "Custom"."""
        size_keys = ("gs_game_w", "gs_game_h", "gs_out_w", "gs_out_h")
        filling = {"active": False}

        def set_fields(values) -> None:
            filling["active"] = True
            try:
                for key, value in zip(size_keys, values):
                    ui[key].set_text(str(value))
            finally:
                filling["active"] = False

        def output_size() -> Optional[Tuple[int, int]]:
            if monitor:
                return monitor
            try:
                return int(ui["gs_out_w"].get_text()), int(ui["gs_out_h"].get_text())
            except ValueError:
                return None

        def on_preset_changed(combo) -> None:
            preset = combo.get_active_id()
            if filling["active"] or preset in (None, "custom"):
                return
            out = output_size()
            if out is None:
                return
            factor = dict(QUALITY_PRESETS)[preset]
            set_fields((*preset_game_resolution(*out, factor), *out))
            if preset != "native" and not ui["gs_upscale"].get_active_id():
                ui["gs_upscale"].set_active_id("fsr")      # a no-op where FSR isn't offered

        def on_size_edited(_entry) -> None:
            if filling["active"]:
                return
            detected = detect_preset(*(ui[k].get_text().strip() for k in size_keys))
            out = output_size()
            if detected and out and ui["gs_out_w"].get_text().strip() != str(out[0]):
                detected = None     # a preset of some other output size
            filling["active"] = True
            try:
                ui["gs_preset"].set_active_id(detected or "custom")
            finally:
                filling["active"] = False

        def on_enabled(check) -> None:
            ui["gs_grid"].set_sensitive(check.get_active())
            if check.get_active() and monitor and not any(ui[k].get_text().strip() for k in size_keys):
                ui["gs_preset"].set_active_id("native")

        ui["gs_preset"].connect("changed", on_preset_changed)
        for key in size_keys:
            ui[key].connect("changed", on_size_edited)
        ui["gs_enabled"].connect("toggled", on_enabled)
        ui["gs_preset"].set_active_id("custom")

    # --- model <-> widgets -------------------------------------------------------------

    @staticmethod
    def _lo_apply_native(ui: Dict, native: bool) -> None:
        for key in ("proton_row", "compat_grid", "anticheat_grid"):
            ui[key].set_sensitive(not native)

    def _lo_write_widgets(self, ui: Dict, opts: LaunchOptions) -> None:
        if "native" in ui:
            ui["native"].set_active(opts.native)
            self._lo_apply_native(ui, opts.native)
        ui["proton"].set_active_id(opts.proton or "")
        if ui["proton"].get_active_id() is None:
            ui["proton"].set_active(0)
        for key in ("gamemode", "mangohud", "vkbasalt", "esync", "fsync", "wayland", "hdr", "fsr", "nvapi",
                    "proton_log", "battleye", "eac"):
            if key in ui:
                ui[key].set_active(getattr(opts, key))
        ui["fps_check"].set_active(bool(opts.fps_limit))
        ui["fps_entry"].set_text(opts.fps_limit)
        ui["fps_entry"].set_sensitive(bool(opts.fps_limit))
        ui["args"].set_text(opts.args)
        gs = opts.gamescope
        ui["gs_enabled"].set_active(gs.enabled)
        for key, value in (("gs_game_w", gs.game_width), ("gs_game_h", gs.game_height),
                           ("gs_out_w", gs.output_width), ("gs_out_h", gs.output_height),
                           ("gs_fps", gs.fps_limit), ("gs_fps_unfocused", gs.fps_limit_unfocused),
                           ("gs_additional", gs.additional_options)):
            ui[key].set_text(value)
        ui["gs_upscale"].set_active_id(gs.upscale_method)
        ui["gs_window"].set_active_id(gs.window_type)
        ui["gs_grab"].set_active(gs.force_grab_cursor)
        ui["gs_grid"].set_sensitive(gs.enabled)
        store = ui["env_store"]
        store.clear()
        for key, value in opts.env.items():
            store.append([key, value])

    def _lo_read_widgets(self, ui: Dict, previous: LaunchOptions) -> LaunchOptions:
        opts = LaunchOptions(wrappers=list(previous.wrappers))
        opts.native = ui["native"].get_active() if "native" in ui else previous.native
        opts.proton = ui["proton"].get_active_id() or None
        for key in ("gamemode", "mangohud", "vkbasalt", "esync", "fsync", "wayland", "hdr", "fsr", "nvapi",
                    "proton_log", "battleye", "eac"):
            if key in ui:
                setattr(opts, key, ui[key].get_active())
            else:
                setattr(opts, key, getattr(previous, key))
        fps = ui["fps_entry"].get_text().strip()
        opts.fps_limit = fps if ui["fps_check"].get_active() and fps else ""
        opts.args = ui["args"].get_text().strip()
        opts.gamescope = GamescopeOptions(
            enabled=ui["gs_enabled"].get_active(),
            game_width=ui["gs_game_w"].get_text().strip(),
            game_height=ui["gs_game_h"].get_text().strip(),
            output_width=ui["gs_out_w"].get_text().strip(),
            output_height=ui["gs_out_h"].get_text().strip(),
            upscale_method=ui["gs_upscale"].get_active_id() or "",
            window_type=ui["gs_window"].get_active_id() or "",
            fps_limit=ui["gs_fps"].get_text().strip(),
            fps_limit_unfocused=ui["gs_fps_unfocused"].get_text().strip(),
            force_grab_cursor=ui["gs_grab"].get_active(),
            additional_options=ui["gs_additional"].get_text().strip(),
        )
        for row in ui["env_store"]:
            key, value = row[0].strip(), row[1]
            if key:
                opts.env[key] = value
        return opts

    def _lo_text_to_options(self, ui: Dict, previous: LaunchOptions) -> LaunchOptions:
        buf = ui["text"].get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        opts = parse_command_line(text, base=previous)
        if not opts.gamescope.enabled:
            # Keep the gamescope fields typed in so far when it's merely off.
            opts.gamescope = copy.deepcopy(previous.gamescope)
            opts.gamescope.enabled = False
        return opts

    # --- Proton by default (Steam) ---------------------------------------------------

    def _apply_steam_proton_default(self, appids) -> None:
        """Steam games that also have a Linux version get Steam Play's default
        Proton, once each, while Steam is closed (see apply_default_proton)."""
        try:
            SteamBackend().apply_default_proton(appids)
        except Exception as e:
            logger.warning(f"Couldn't set the default Proton for Steam games: {e}")

    # --- saving -------------------------------------------------------------------

    def _lo_save(self, game: GameItem, backend: LaunchOptionsBackend, opts: LaunchOptions,
                 parent: Gtk.Window) -> bool:
        """True once saved (the dialog may close); False to keep it open."""
        if backend.blocking_process_running():
            if not self._confirm_yes_no(self._("lo_steam_close_question"), transient_for=parent,
                                        message_type=Gtk.MessageType.WARNING,
                                        title=self._("lo_steam_close_and_save")):
                return False
            if not self._lo_close_steam(parent, backend):
                self._show_info_dialog(self._("lo_steam_still_running"), message_type=Gtk.MessageType.ERROR)
                return False
        try:
            backend.save(game, opts)
        except Exception as e:
            logger.error(f"Couldn't save launch options of {game.title!r}: {e}")
            self._show_info_dialog(f"{self._('lo_save_failed')}: {e}", message_type=Gtk.MessageType.ERROR)
            return False
        logger.info(f"Saved launch options of {game.title!r} to {backend.where(game)}")
        # the game's Proton may have changed: its hint with it (ui/proton_hints.py)
        hints = getattr(self, "proton_hints", None)
        if hints is not None:
            hints.refresh()
        return True

    def close_launcher_asking(self, launcher: str, parent: Gtk.Window) -> bool:
        """Closes the launcher - asked first when one of its games runs,
        as it may go with it. True once it's closed."""
        playing = any(game is not None and game.launcher == launcher
                      for game in (self.game_index.get(uid) for uid in
                                   set(self.running_procs) | set(self.detected_running)))
        if playing and not self._confirm_yes_no(
                self._("lo_close_launcher_game_running").format(launcher=launcher), transient_for=parent,
                title=self._("lo_close_launcher").format(launcher=launcher), destructive=True):
            return False
        if not self._close_launcher(parent, launcher):
            self._show_info_dialog(self._("lo_launcher_still_running").format(launcher=launcher),
                                   message_type=Gtk.MessageType.ERROR, transient_for=parent)
            return False
        return True

    def _lo_close_steam(self, parent: Gtk.Window, backend: LaunchOptionsBackend) -> bool:
        return self._close_launcher(parent, "Steam")

    def _close_launcher(self, parent: Gtk.Window, launcher: str) -> bool:
        """Asks the launcher to quit and waits for it, under a card with
        the loading bar; True once it's gone. Steam by its own -shutdown,
        each running one by its own command (a Flatpak Steam doesn't hear
        the native one, which would start a native Steam besides); the
        rest have no such command and get the SIGTERM a desktop's quit
        sends."""
        if launcher == "Steam":
            invocations = steam_shutdown_invocations()
            if not invocations:
                invocation = resolve_launcher_invocation("steam", LAUNCHER_FLATPAK_IDS["Steam"])
                invocations = [invocation] if invocation else []
            if not invocations and launcher_pids("Steam"):
                return False
            for invocation in invocations:
                try:
                    subprocess.Popen([*invocation, "-shutdown"], start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError as e:
                    logger.error(f"Couldn't ask Steam ({invocation[0]}) to shut down: {e}")
        else:
            for pid in launcher_pids(launcher):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError as e:
                    logger.warning(f"Couldn't ask {launcher} ({pid}) to quit: {e}")
        def still_running():
            # Steam's save also waits for its web helpers to end
            return launcher_pids(launcher) or (launcher == "Steam" and _process_running(("steamwebhelper",)))

        if not still_running():
            return True
        # the app's own card dialog with the bar the loading dialogs
        # bounce under its text; the dialog is the bar's toplevel
        bar = Gtk.ProgressBar()
        bar.set_pulse_step(0.04)
        bar.get_style_context().add_class("accent-progress")
        pulse_id = GLib.timeout_add(30, lambda: (bar.pulse(), True)[1])
        started = time.monotonic()
        polling = {"source": None}

        def poll():
            if not still_running():
                result = Gtk.ResponseType.OK
            elif time.monotonic() - started > _STEAM_SHUTDOWN_TIMEOUT_S:
                result = Gtk.ResponseType.REJECT
            else:
                return True
            polling["source"] = None
            dialog = bar.get_toplevel()
            if isinstance(dialog, Gtk.Dialog):
                dialog.response(result)
            return False

        polling["source"] = GLib.timeout_add(500, poll)
        response = self._card_dialog(
            self._("lo_waiting_launcher").format(launcher=launcher),
            self._("lo_waiting_launcher_sub").format(launcher=launcher),
            [(self._("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost")],
            icon_name="clock-symbolic", transient_for=parent, extra=bar,
            other_responses=(Gtk.ResponseType.OK, Gtk.ResponseType.REJECT))
        GLib.source_remove(pulse_id)
        if polling["source"] is not None:     # closed by the person, not by poll()
            GLib.source_remove(polling["source"])
        return response == Gtk.ResponseType.OK
