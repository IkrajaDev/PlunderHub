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
"""A game's right-click menu, its page, and the SteamGridDB picker.

GameMenu   the right-click menu: a header, Set sail / Favorite / Hide, and
           four rows - Game settings, Open, On the web, Select · Reorder.
           Open and On the web unfold, when clicked, in a column beside
           it, inside the same popover (GTK 3 on Wayland hides a popover when a second one
           opens next to it).
GamePage   the game's own page: its hero, cover and icon (each can be
           changed), an Overview and the Launch options.
ArtPicker  choosing a cover, hero or icon from SteamGridDB.

The Launch options tab keeps the old editor's widget keys (gamemode,
gs_enabled, env_store...) so the LaunchOptionsMixin's load, save and
gamescope-preset logic drives it unchanged.
"""

import bisect
import copy
import hashlib
import math
import os
import shutil
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cairo
import gi

gi.require_version("PangoCairo", "1.0")
from gi.repository import GObject, PangoCairo  # noqa: E402

from plunderhub.core.launch_options import (  # noqa: E402
    LAUNCHER_SKIP_ARG, QUALITY_PRESETS, WRAPPER_KEY, LaunchOptions, SteamBackend, backend_for, missing_tools,
    to_command_line, window_mode_in, with_arg, with_window_mode,
)
from plunderhub.constants import CACHE_DIR  # noqa: E402
from plunderhub.core.models import GameItem  # noqa: E402
from plunderhub.core.persistence import save_settings  # noqa: E402
from plunderhub.core.heroic_cloud import SYNC_MODES, cloud_saves  # noqa: E402
from plunderhub.core.save_backup import backup_dir, list_backups  # noqa: E402
from plunderhub.core.heroic_launch import runner_of, updates_before_launch  # noqa: E402
from plunderhub.core import flatpak_layers, prefix_tools  # noqa: E402
from plunderhub.core.apng import open_animation  # noqa: E402
from plunderhub.core.tools import TOOLS_BY_ID  # noqa: E402
from plunderhub.core.utils import HEADERS, format_bytes  # noqa: E402
from plunderhub.gi_setup import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, hidpi_pixbuf, logical_size, raw_set_source_pixbuf, ui_scale  # noqa: E402
from plunderhub.icons import get_icon_pixbuf  # noqa: E402
from plunderhub.launchers.registry import LAUNCHER_SEARCH_NAMES  # noqa: E402
from plunderhub.logging_setup import logger  # noqa: E402
from plunderhub.ui import keynav  # noqa: E402
from plunderhub.ui.home_mixin import _accent_rgb, _on_accent_rgb, _rounded  # noqa: E402
from plunderhub.ui.progress_card import card_dialog  # noqa: E402

HERO_H = 300
COVER_W, COVER_H = 168, 252
ICON = 56
COVER_X, COVER_Y = 28, HERO_H - 150
TEXT_X = COVER_X + COVER_W + 24
TABS_Y = HERO_H + 96
HEADER_H = max(COVER_Y + COVER_H, TABS_Y + 42) + 18


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb)


def _colour_of(widget: Gtk.Widget, dim: bool = False) -> str:
    ctx = widget.get_style_context()
    c = ctx.get_color(ctx.get_state())
    return _hex((c.red, c.green, c.blue)) if not dim else _hex((c.red * 0.7, c.green * 0.7, c.blue * 0.7))


def _icon(name: str, size: int, colour: Optional[str] = None) -> Gtk.Image:
    """A bundled icon in the label colour of wherever it ends up - or in
    colour, when given."""
    image = Gtk.Image()

    def tint(widget, *_):
        pb = get_icon_pixbuf(name, size, colour or _colour_of(widget))
        if pb is not None:
            widget.set_from_pixbuf(pb)
    image.connect("style-updated", tint)
    image.connect("realize", tint)
    return image


def _load_scaled(path: Optional[Path], w: int, h: int, crop: bool = False) -> Optional[GdkPixbuf.Pixbuf]:
    """The first frame of path, fitted into w x h - or, with crop,
    filling it and cut to it."""
    if path is None:
        return None
    try:
        pb = GdkPixbuf.PixbufAnimation.new_from_file(str(path)).get_static_image()
    except Exception as e:
        logger.debug(f"Couldn't read {path}: {e}")
        return None
    if pb is None:
        return None
    hidpi = ui_scale()          # w x h logical, with the screen's own pixels
    w, h = w * hidpi, h * hidpi
    pw, ph = pb.get_width(), pb.get_height()
    scale = max(w / pw, h / ph) if crop else min(w / pw, h / ph)
    nw, nh = max(1, int(pw * scale)), max(1, int(ph * scale))
    pb = pb.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
    if crop:
        x, y = max(0, (nw - w) // 2), max(0, int((nh - h) * 0.3))
        pb = pb.new_subpixbuf(x, y, min(w, nw - x), min(h, nh - y)).copy()
    return hidpi_pixbuf(pb, hidpi)


class ToggleSwitch(Gtk.Switch):
    """A Gtk.Switch with a CheckButton's "toggled" signal - what the
    launch-options logic listens to."""
    __gsignals__ = {"toggled": (GObject.SignalFlags.RUN_FIRST, None, ())}

    def __init__(self):
        super().__init__()
        self.set_valign(Gtk.Align.CENTER)
        self.connect("notify::active", lambda *_: self.emit("toggled"))


# --- the right-click menu ---------------------------------------------------------------

def _close_on_shadow_click(pop: Gtk.Popover):
    """The popover's window takes in its drop shadow too: a click there,
    just below or beside the menu, counted as inside and left it open.
    Outside what's drawn is outside."""
    def press(widget, _event) -> bool:
        child = widget.get_child()
        if child is None:
            return False
        x, y = child.get_pointer()
        alloc = child.get_allocation()
        if 0 <= x < alloc.width and 0 <= y < alloc.height:
            return False
        widget.popdown()
        return True
    pop.connect("button-press-event", press)


class GameMenu(Gtk.Popover):
    def __init__(self, app, game: GameItem, relative_to: Gtk.Widget):
        super().__init__()
        self.app, self.game = app, game
        self._ = app._
        self.set_relative_to(relative_to)
        self.get_style_context().add_class("game-menu")
        _close_on_shadow_click(self)
        self.side_for: Optional[str] = None

        outer = Gtk.Box()
        outer.pack_start(self._main_column(), False, False, 0)
        self.side = Gtk.Revealer()
        self.side.set_transition_type(Gtk.RevealerTransitionType.SLIDE_RIGHT)
        self.side.set_transition_duration(160)
        self.side.connect("notify::child-revealed", self._on_side_folded)
        side_box = Gtk.Box()
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.get_style_context().add_class("gm-vsep")
        side_box.pack_start(sep, False, False, 0)
        self.side_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.side_list.set_valign(Gtk.Align.START)
        self.side_list.set_margin_top(4)
        side_box.pack_start(self.side_list, False, False, 0)
        self.side.add(side_box)
        outer.pack_start(self.side, False, False, 0)
        self.add(outer)
        outer.show_all()
        # while it's open the window's keys and the controller come here (App._open_menu)
        keynav.track(self, app)
        self.connect("show", lambda *_: setattr(app, "_open_menu", self))
        self.connect("closed", lambda *_: getattr(app, "_open_menu", None) is self and setattr(app, "_open_menu", None))
        # a new menu is made for every opening: this one goes once it's
        # faded out (kept, it and its handlers on the window piled up)
        self.connect("closed", lambda *_: GLib.timeout_add(1000, self._drop_if_closed))
        self._gone = False
        self.connect("destroy", lambda *_: setattr(self, "_gone", True))

    def _drop_if_closed(self) -> bool:
        if not self._gone and not self.get_visible():
            self.destroy()
        return False

    # keyboard and controller ------------------------------------------------

    def focus_first(self):
        """Opened from the keyboard or a controller: onto its first row."""
        focus = self.get_toplevel().get_focus() if self.get_toplevel() else None
        if focus is None or not focus.is_ancestor(self):
            self.child_focus(Gtk.DirectionType.TAB_FORWARD)
        keynav.on(self)

    def _side_buttons(self) -> List[Gtk.Widget]:
        return [b for b in self.side_list.get_children()
                if isinstance(b, Gtk.Button) and b.get_sensitive() and b.get_visible()]

    def _opener(self) -> Optional[Gtk.Widget]:
        found = []

        def walk(w):
            if getattr(w, "plunderhub_side", None) == self.side_for:
                found.append(w)
            if isinstance(w, Gtk.Container):
                for child in w.get_children():
                    walk(child)
        walk(self.get_child())
        return found[0] if found else None

    def nav(self, action: str) -> bool:
        """Arrows / d-pad move along the menu, Right opens a row's side
        list (Left and Back fold it away), A / Enter picks, Back closes."""
        keynav.on(self)
        top = self.get_toplevel()
        focus = top.get_focus() if top is not None else None
        inside = focus is not None and focus.is_ancestor(self)
        if not inside and action in ("up", "down", "left", "right"):
            self.focus_first()
            return True
        in_side = inside and focus.is_ancestor(self.side_list)
        if action == "back":
            if self.side_for is not None:
                opener = self._opener()
                self._close_side()
                if opener is not None:
                    opener.grab_focus()
            else:
                self.popdown()
            return True
        if action == "activate":
            if inside:
                focus.activate()
            return True
        if action == "right" and inside and not in_side and getattr(focus, "plunderhub_side", None):
            self._open_side(focus.plunderhub_side)
            rows = self._side_buttons()
            if rows:
                rows[0].grab_focus()
            return True
        if action == "left" and in_side:
            return self.nav("back")
        direction = {"up": Gtk.DirectionType.UP, "down": Gtk.DirectionType.DOWN,
                     "left": Gtk.DirectionType.LEFT, "right": Gtk.DirectionType.RIGHT}.get(action)
        if direction is not None:
            self.child_focus(direction)
        return True

    # rows ----------------------------------------------------------------

    def _row(self, icon_name: str, text: str, sub: Optional[str] = None, chevron: bool = False,
             action: Optional[Callable] = None, side: Optional[str] = None) -> Gtk.Button:
        btn = Gtk.Button()
        btn.get_style_context().add_class("gm-row")
        box = Gtk.Box(spacing=12)
        box.pack_start(_icon(icon_name, 18), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        texts.set_valign(Gtk.Align.CENTER)
        texts.pack_start(Gtk.Label(label=text, xalign=0), False, False, 0)
        if sub:
            s = Gtk.Label(label=sub, xalign=0)
            s.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            s.set_max_width_chars(32)
            s.get_style_context().add_class("gm-row-sub")
            texts.pack_start(s, False, False, 0)
        box.pack_start(texts, True, True, 0)
        if chevron:
            c = Gtk.Label(label="›")
            c.get_style_context().add_class("gm-chev")
            box.pack_end(c, False, False, 0)
        btn.add(box)
        if side is not None:
            btn.plunderhub_side = side
            btn.connect("clicked", lambda b: self._toggle_side(side))
        elif action is not None:
            btn.connect("clicked", lambda *_: (self.popdown(), action()))
        return btn

    def _main_column(self) -> Gtk.Box:
        app, game, _ = self.app, self.game, self._
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        head = Gtk.Box(spacing=12)
        head.set_margin_start(6)
        head.set_margin_end(6)
        head.set_margin_top(4)
        head.set_margin_bottom(10)
        icon = app._create_game_icon_image(game, 40)
        icon.set_valign(Gtk.Align.CENTER)
        head.pack_start(icon, False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        titles.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=game.title, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(26)
        title.get_style_context().add_class("gm-title")
        sub = Gtk.Label(label=f"{game.launcher}  ·  {app._home_playtime_text(game.uid)}", xalign=0)
        sub.get_style_context().add_class("gm-row-sub")
        titles.pack_start(title, False, False, 0)
        titles.pack_start(sub, False, False, 0)
        head.pack_start(titles, True, True, 0)
        page.pack_start(head, False, False, 0)

        running = app._is_game_running(game.uid)
        fav = game.uid in app.favorites
        quick = Gtk.Box(spacing=6, homogeneous=True)
        for icon_name, text, kind, action in (
                ("media-playback-stop-symbolic" if running else "media-playback-start-symbolic",
                 _("stop_game_tooltip") if running else _("launch_label"), "danger" if running else "primary",
                 (lambda: app._on_stop_game_clicked(None, game)) if running else
                 (lambda: app.request_launch(game.launch_cmd, game.title, game.env, game.uid, game.install_dir))),
                ("starred-symbolic", _("gm_unfavorite") if fav else _("gm_favorite"), "on" if fav else "",
                 lambda: app._toggle_favorite(game)),
                ("view-conceal-symbolic", _("gm_hide"), "", lambda: app._hide_game(game))):
            b = Gtk.Button()
            ctx = b.get_style_context()
            ctx.add_class("gm-quick")
            if kind:
                ctx.add_class(f"gm-{kind}")
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            v.pack_start(_icon(icon_name, 18), False, False, 0)
            v.pack_start(Gtk.Label(label=text), False, False, 0)
            b.add(v)
            b.connect("clicked", lambda *_a, f=action: (self.popdown(), f()))
            quick.pack_start(b, True, True, 0)
        page.pack_start(quick, False, False, 4)

        page.pack_start(self._separator(), False, False, 0)
        page.pack_start(self._row("preferences-system-symbolic", _("gm_settings"), _("gm_settings_sub"),
                                  action=lambda: GamePage(app, game)), False, False, 0)
        page.pack_start(self._row("folder-symbolic", _("gm_open"), _("gm_open_sub"), chevron=True, side="open"),
                        False, False, 0)
        page.pack_start(self._row("web-browser-symbolic", _("gm_web"), _("gm_web_sub"), chevron=True, side="web"),
                        False, False, 0)
        if cloud_saves(game.uid) is not None:
            page.pack_start(self._row("cloud-symbolic", _("gm_cloud"), _("gm_cloud_sub"), chevron=True,
                                      side="cloud"), False, False, 0)
        elif app.save_locations(game):
            page.pack_start(self._row("document-save-symbolic", _("gm_backup"), _("gm_backup_sub"), chevron=True,
                                      side="backup"), False, False, 0)
        page.pack_start(self._separator(), False, False, 0)

        split = Gtk.Box(homogeneous=True)
        selected = app.selection_mode and game.uid in app.selected_uids

        def select():
            if not app.selection_mode:
                app._enter_selection_mode()
            app._toggle_selection(game.uid)
        for icon_name, text, action in (
                ("object-select-symbolic", _("gm_deselect") if selected else _("gm_select"), select),
                ("view-sort-descending-symbolic", _("reorder_menu"), lambda: app._enter_reorder_mode())):
            b = Gtk.Button()
            b.get_style_context().add_class("gm-split")
            hb = Gtk.Box(spacing=8)
            hb.set_halign(Gtk.Align.CENTER)
            hb.pack_start(_icon(icon_name, 16), False, False, 0)
            hb.pack_start(Gtk.Label(label=text), False, False, 0)
            b.add(hb)
            b.connect("clicked", lambda *_a, f=action: (self.popdown(), f()))
            split.pack_start(b, True, True, 0)
        page.pack_start(split, False, False, 0)
        return page

    @staticmethod
    def _separator() -> Gtk.Separator:
        sep = Gtk.Separator()
        sep.get_style_context().add_class("gm-sep")
        return sep

    # Open / On the web ------------------------------------------------------

    def _side_rows(self, which: str):
        app, game, _ = self.app, self.game, self._
        if which == "cloud":
            saves = cloud_saves(game.uid)
            if saves is None or not saves.locations:
                return []
            icons = {"download": "cloud-download-symbolic", "upload": "cloud-upload-symbolic",
                     "force_download": "cloud-download-symbolic", "force_upload": "cloud-upload-symbolic"}
            rows = [(icons[mode], _(f"cloud_{mode}"), _(f"cloud_{mode}_sub"),
                     lambda m=mode: app.start_cloud_sync(game, m)) for mode, _flag in SYNC_MODES]
            # None: a separator
            return rows + [None] + self._backup_rows()
        if which == "backup":
            return self._backup_rows()
        if which == "open":
            rows = []
            if game.install_dir and Path(game.install_dir).exists():
                rows.append(("folder-symbolic", _("gm_game_folder"), game.install_dir,
                             lambda: app._open_game_folder(game)))
            saves = app._resolve_save_dir(game)
            if saves is not None:
                rows.append(("document-save-symbolic", _("gp_saves"), str(saves),
                             lambda: app._open_game_saves(game)))
            prefix = app._resolve_wine_prefix(game)
            if prefix is not None:
                rows.append(("drive-harddisk-symbolic", _("gp_prefix"), str(prefix),
                             lambda: app._open_wineprefix_folder(game)))
            return rows
        rows = []
        if game.store_platform != "sideload":
            # Hand-added Heroic games have no store - the fallback would
            # open an unrelated Steam search.
            rows.append(("store-symbolic", _("gm_store"), _("gm_store_sub"), lambda: app._open_store_page(game)))
        rows.append(("circle-dashed-check-symbolic", "ProtonDB", _("gm_protondb_sub"),
                     lambda: app._open_protondb(game)))
        rows.append(("scroll-text-symbolic", _("patch_notes_menu"), _("gm_patch_notes_sub"),
                     lambda: app._show_patch_notes(game)))
        return rows

    def _backup_rows(self):
        app, game, _ = self.app, self.game, self._
        backups = list_backups(game.uid)
        last = (_("saves_last_backup").format(when=app._home_ago_text(backups[0].created))
                if backups else _("saves_no_backup"))
        return [("document-save-symbolic", _("saves_backup"),
                 _("saves_backup_sub").format(path=app._home_path(backup_dir(game.uid))),
                 lambda: app.back_up_game_saves(game)),
                ("rotate-ccw-symbolic", _("saves_restore"), last,
                 (lambda: app.restore_game_saves(game)) if backups else None),
                None,
                ("folder-symbolic", _("saves_open_backups_row"), app._home_path(backup_dir(game.uid)),
                 lambda: app.open_save_backups(game))]

    def _toggle_side(self, which: str):
        """A click on Open / On the web unfolds it - or, when it's the one
        already open, folds it away."""
        if self.side_for == which:
            self._close_side()
        else:
            self._open_side(which)

    def _open_side(self, which: str):
        if self.side_for == which:
            return
        for child in self.side_list.get_children():
            child.destroy()
        # Always from the top, under its own heading - the same place
        # whichever row opened it.
        heading = Gtk.Label(label=self._side_heading(which).upper(), xalign=0)
        heading.get_style_context().add_class("home-menu-heading")
        self.side_list.pack_start(heading, False, False, 0)
        if which == "cloud":
            # Heroic says the same above its sync
            warning = Gtk.Box(spacing=10)
            warning.get_style_context().add_class("gm-warning")
            warning.pack_start(_icon("triangle-alert-symbolic", 16, "#e8a33d"), False, False, 0)
            text = Gtk.Label(label=self._("cloud_beta_warning"), xalign=0)
            text.set_line_wrap(True)
            text.set_max_width_chars(30)
            warning.pack_start(text, True, True, 0)
            self.side_list.pack_start(warning, False, False, 0)
        rows = self._side_rows(which)
        if not rows:
            none = Gtk.Label(label=self._("cloud_no_folder" if which == "cloud" else "gm_nothing_to_open"),
                             xalign=0)
            none.set_line_wrap(True)
            none.set_max_width_chars(30)
            none.get_style_context().add_class("gm-row-sub")
            none.set_margin_start(12)
            none.set_margin_top(6)
            self.side_list.pack_start(none, False, False, 0)
        for row in rows:
            if row is None:
                self.side_list.pack_start(self._separator(), False, False, 0)
                continue
            icon_name, text, sub, action = row
            b = self._row(icon_name, text, sub, action=action)
            if action is None:
                b.set_sensitive(False)
                b.set_opacity(0.5)
            self.side_list.pack_start(b, False, False, 0)
        self.side_list.show_all()
        self.side.set_reveal_child(True)
        self.side_for = which

    def _side_heading(self, which: str) -> str:
        return self._({"open": "gm_open", "cloud": "gm_cloud", "backup": "gm_backup"}.get(which, "gm_web"))

    def _close_side(self):
        self.side.set_reveal_child(False)
        self.side_for = None

    def _on_side_folded(self, revealer, _pspec):
        """Folded away it's no wider than nothing but still as tall as
        its rows (GTK's revealer keeps the height) - emptied, the menu
        shrinks back to its own column."""
        if revealer.get_child_revealed() or self.side_for is not None:
            return
        for child in self.side_list.get_children():
            child.destroy()
        self.queue_resize()


class LauncherMenu(GameMenu):
    """A launcher's right-click menu, built like a game's: what it is,
    Open (or Close) / Resupply / Shipwright, its icon and the folders
    its games are in, reordering and Settings."""

    def __init__(self, app, name: str, relative_to: Gtk.Widget):
        self.name = name
        section = next((s for s in app._cached_launchers if s["name"] == name), None) or {}
        self.cmd = section.get("cmd")
        self.games = [g for g in section.get("games", []) if not app.scanner.is_hidden(g.launcher, g.uid)]
        super().__init__(app, None, relative_to)

    def _main_column(self) -> Gtk.Box:
        app, name, _ = self.app, self.name, self._
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        head = Gtk.Box(spacing=12)
        head.set_margin_start(6)
        head.set_margin_end(6)
        head.set_margin_top(4)
        head.set_margin_bottom(10)
        icon = app._load_launcher_icon(name, 40)
        icon.set_valign(Gtk.Align.CENTER)
        head.pack_start(icon, False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        titles.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=name, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(26)
        title.get_style_context().add_class("gm-title")
        running = app._launcher_is_running(name)
        sub = Gtk.Label(label=_("lm_running") if running else _("games_count").format(count=len(self.games)),
                        xalign=0)
        sub.get_style_context().add_class("gm-row-sub")
        titles.pack_start(title, False, False, 0)
        titles.pack_start(sub, False, False, 0)
        head.pack_start(titles, True, True, 0)
        page.pack_start(head, False, False, 0)

        if running:
            first = ("media-playback-stop-symbolic", _("launcher_close"), "danger",
                     lambda: app._on_close_launcher_clicked(name))
        else:
            first = ("media-playback-start-symbolic", _("launcher_open"), "primary",
                     (lambda: app.request_launch_launcher(self.cmd, name)) if self.cmd else None)
        quick = Gtk.Box(spacing=6, homogeneous=True)
        for icon_name, text, kind, action in (
                first,
                ("view-refresh-symbolic", _("refresh"), "", lambda: app.reload(manual=True)),
                ("hammer-symbolic", _("sw_title"), "", lambda: app.show_shipwright(focus=name))):
            b = Gtk.Button()
            ctx = b.get_style_context()
            ctx.add_class("gm-quick")
            if kind:
                ctx.add_class(f"gm-{kind}")
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            v.pack_start(_icon(icon_name, 18), False, False, 0)
            v.pack_start(Gtk.Label(label=text), False, False, 0)
            b.add(v)
            if action is None:
                b.set_sensitive(False)
            else:
                b.connect("clicked", lambda *_a, f=action: (self.popdown(), f()))
            quick.pack_start(b, True, True, 0)
        page.pack_start(quick, False, False, 4)

        page.pack_start(self._separator(), False, False, 0)
        page.pack_start(self._row("image-x-generic-symbolic", _("icon_menu"), _("lm_icon_sub"), chevron=True,
                                  side="icon"), False, False, 0)
        page.pack_start(self._row("folder-symbolic", _("gm_open"), _("lm_open_sub"), chevron=True, side="open"),
                        False, False, 0)
        page.pack_start(self._separator(), False, False, 0)

        split = Gtk.Box(homogeneous=True)
        for icon_name, text, action in (
                ("view-sort-descending-symbolic", _("reorder_menu"), lambda: app._enter_reorder_mode(name)),
                ("preferences-system-symbolic", _("options"), lambda: app.show_options("library"))):
            b = Gtk.Button()
            b.get_style_context().add_class("gm-split")
            hb = Gtk.Box(spacing=8)
            hb.set_halign(Gtk.Align.CENTER)
            hb.pack_start(_icon(icon_name, 16), False, False, 0)
            hb.pack_start(Gtk.Label(label=text), False, False, 0)
            b.add(hb)
            b.connect("clicked", lambda *_a, f=action: (self.popdown(), f()))
            split.pack_start(b, True, True, 0)
        page.pack_start(split, False, False, 0)
        return page

    def _side_heading(self, which: str) -> str:
        return self._("icon_menu" if which == "icon" else "gm_open")

    def _side_rows(self, which: str):
        app, name, _ = self.app, self.name, self._
        if which == "icon":
            def from_sgdb():
                # after the menu has gone - the picker runs its own loop
                GLib.idle_add(lambda: ArtPicker(
                    app, None, "icon", app, name=LAUNCHER_SEARCH_NAMES.get(name, name), kinds=("icon",),
                    on_apply=lambda _kind, item, preview: app.apply_launcher_sgdb_icon(name, item, preview))
                    and False)
            return [("image-up-symbolic", _("gp_upload"), None, lambda: app._set_launcher_icon_from_file(name)),
                    ("image-down-symbolic", _("gp_choose_sgdb"), None, from_sgdb),
                    None,
                    ("rotate-ccw-symbolic", _("gp_reset"), None,
                     (lambda: app.reset_launcher_icon(name)) if app.has_custom_launcher_icon(name) else None)]
        # the folders its games are in (Steam's libraries, Heroic's and
        # Lutris' install folders...), most games first
        counts: Dict[str, int] = {}
        for game in self.games:
            if game.install_dir:
                parent = os.path.dirname(os.path.normpath(game.install_dir))
                counts[parent] = counts.get(parent, 0) + 1
        rows = []
        for folder in sorted(counts, key=lambda f: -counts[f])[:6]:
            if os.path.isdir(folder):
                rows.append(("folder-symbolic", _("lm_games_folder"), app._home_path(Path(folder)),
                             lambda f=folder: app._show_folder(f)))
        return rows


# --- the game page ---------------------------------------------------------------------------

class GamePage(Gtk.Dialog):
    """The game's own page. One scroll for all of it - the header goes up
    with the content, nothing gets cut off part way."""

    def __init__(self, app, game: GameItem, tab: str = "overview", show_hint: bool = False):
        super().__init__(transient_for=app._dialog_transient_parent(), modal=True)
        self.app, self.game = app, game
        self._ = app._
        self.set_title(self._("gp_title").format(name=game.title))
        self.set_decorated(False)
        _w, h = app.get_size()
        self.set_default_size(1060, max(640, min(900, h - 60)))
        self.get_style_context().add_class("gp-window")
        action_area = self.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()
        self.hero_pb = self.cover_pb = self.icon_pb = None
        self.hero_for_width = 0
        self.hover: Optional[str] = None
        self.launch: Optional["LaunchOptionsTab"] = None

        outer = Gtk.Overlay()
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # the Settings window's bar: always there, in the accent colour
        self.scroller.set_overlay_scrolling(False)
        self.scroller.get_style_context().add_class("settings-scroll")
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Overlay()
        self.banner = Gtk.DrawingArea()
        self.banner.set_size_request(-1, HEADER_H)
        self.banner.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK
                               | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.banner.connect("draw", self._draw_banner)
        self.banner.connect("motion-notify-event", lambda _w, e: self._set_hover(self._hit(e.x, e.y)))
        self.banner.connect("leave-notify-event", lambda *_: self._set_hover(None))
        self.banner.connect("button-press-event", self._on_banner_click)
        # the art's menu; it goes when the page loses the focus too - a
        # click outside PlunderHub doesn't reach a popover on Wayland
        self._art_menu: Optional[Gtk.Popover] = None
        self.connect("notify::is-active",
                     lambda w, _p: not w.is_active() and self._art_menu is not None and self._art_menu.popdown())
        # an animated hero or cover plays here too - each redraws its part
        from plunderhub.ui.animated_hero import HeroAnimation
        self.hero_anim = HeroAnimation(app, self.banner, lambda: (0, 0, self.banner.get_allocated_width(), HERO_H))
        self.cover_anim = HeroAnimation(app, self.banner, lambda: (COVER_X, COVER_Y, COVER_W, COVER_H))
        head.add(self.banner)

        tabs = Gtk.Box(spacing=2)
        tabs.get_style_context().add_class("gp-tabs")
        tabs.set_halign(Gtk.Align.START)
        tabs.set_valign(Gtk.Align.START)
        tabs.set_margin_start(TEXT_X)
        tabs.set_margin_top(TABS_Y)
        self.pages = Gtk.Stack()
        self.pages.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.pages.set_vhomogeneous(False)
        self.pages.add_named(self._overview(), "overview")
        tab_specs = [("overview", self._("gp_tab_overview"))]
        backend = backend_for(game)
        if backend is not None:
            try:
                self.launch = LaunchOptionsTab(app, game, backend, self)
                self.pages.add_named(self.launch, "launch")
                tab_specs.append(("launch", self._("gp_tab_launch")))
            except Exception as e:
                logger.error(f"Couldn't read the launch options of {game.title!r}: {e}")
        first = None
        self.tab_buttons: Dict[str, Gtk.RadioButton] = {}
        for name, text in tab_specs:
            rb = Gtk.RadioButton.new_with_label_from_widget(first, text)
            rb.set_mode(False)
            first = first or rb
            rb.connect("toggled", lambda b, n=name: b.get_active() and self._show_tab(n))
            tabs.pack_start(rb, False, False, 0)
            self.tab_buttons[name] = rb
        head.add_overlay(tabs)
        page.pack_start(head, False, False, 0)
        page.pack_start(self.pages, True, True, 0)
        self.scroller.add(page)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.pack_start(self.scroller, True, True, 0)
        if self.launch is not None:
            body.pack_start(self.launch.footer, False, False, 0)
        outer.add(body)
        close = Gtk.Button()
        close.get_style_context().add_class("gp-close")
        close.add(_icon("window-close-symbolic", 18, "#ffffff"))
        close.set_tooltip_text(self._("close_btn"))
        close.set_halign(Gtk.Align.END)
        close.set_valign(Gtk.Align.START)
        close.set_margin_top(14)
        close.set_margin_end(36)    # clear of the scrollbar
        close.connect("clicked", lambda *_: self.response(Gtk.ResponseType.CLOSE))
        outer.add_overlay(close)
        self.get_content_area().set_border_width(0)
        self.get_content_area().pack_start(outer, True, True, 0)
        self.connect("key-press-event", self._on_key)
        # arrow keys and the controller: see _nav; where they stand, shown
        app._enable_keyboard_focus_ring_detection(self)
        self.plunderhub_nav = self._nav
        keynav.track(self)
        self.connect("set-focus", lambda _w, widget: widget is not None and GLib.idle_add(self._scroll_to, widget))

        self._load_art()
        self.show_all()
        # Nothing focused at first - GTK would put it on the close button
        # and ring it, as if it was about to be pressed.
        self.set_focus(None)
        if self.launch is not None:
            self.launch.footer.hide()
        if tab == "launch":
            self.show_launch_options()
        if show_hint:
            # opened from the card's Proton badge: down to its strip, lit up
            # a moment - else it looks as if nothing happened
            GLib.timeout_add(300, self._bring_hint)
        app._game_page = self
        app._enter_dimmed_dialog()
        try:
            self.run()
        finally:
            app._game_page = None
            app._exit_dimmed_dialog()
            self.destroy()

    def _on_key(self, _w, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self._nav("back")
            return True
        if (event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter)
                and not isinstance(self.get_focus(), Gtk.Entry)):
            self._nav("activate")
            return True
        return False

    def _scroll_to(self, widget: Gtk.Widget) -> bool:
        """Keeps what the keyboard or controller stands on in view."""
        page = self.scroller.get_child()
        if isinstance(page, Gtk.Viewport):
            page = page.get_child()     # coordinates in the content, not in the view
        if page is None or not widget.is_ancestor(page):
            return False
        at = widget.translate_coordinates(page, 0, 0)
        if not at:
            return False
        adj = self.scroller.get_vadjustment()
        top, h, view = at[-1], widget.get_allocated_height(), adj.get_page_size()
        margin = 40
        if top - margin < adj.get_value():
            adj.set_value(max(adj.get_lower(), top - margin))
        elif top + h + margin > adj.get_value() + view:
            adj.set_value(min(adj.get_upper() - view, top + h + margin - view))
        return False

    def _nav(self, action: str):
        """The keyboard's arrows and the controller: LB / RB switch between
        Overview and Launch options (and the Launch options' own tabs
        with the d-pad's Left / Right on them); the rest moves the focus
        and works the controls, as in any dialog."""
        app = self.app
        keynav.on(self)
        current = self.pages.get_visible_child_name()
        if action in ("prev_tab", "next_tab"):
            names = list(self.tab_buttons)
            i = names.index(current) if current in names else 0
            target = self.tab_buttons[names[(i + (1 if action == "next_tab" else -1)) % len(names)]]
            target.set_active(True)
            target.grab_focus()
            return
        if action == "back":
            if self.plunderhub_inside_row:
                self._set_inside(False)     # out of the row first
            else:
                self.response(Gtk.ResponseType.CLOSE)
            return
        focus = self.get_focus()
        if focus is None and action in ("up", "down", "left", "right", "activate"):
            # back where the keyboard was before the pointer moved - else
            # the first press lands on the tab that's showing
            if not keynav.restore(self):
                self.tab_buttons[current].grab_focus()
            return
        line = self._setting_line(focus) if focus is not None else None
        if action == "activate" and line is not None and not self.plunderhub_inside_row:
            # a row of several controls (Proton, Proton versions): Enter
            # goes into it - its first control ringed, the arrows along it
            self._set_inside(True)
            return
        if action in ("left", "right") and line is not None and not self.plunderhub_inside_row:
            return
        if action == "activate" and isinstance(focus, Gtk.Switch):
            focus.set_active(not focus.get_active())
            return
        if action in ("left", "right"):
            # along a row of tabs: the next one shows straight away
            for row in [list(self.tab_buttons.values())] + (
                    [list(self.launch.chip_buttons.values())] if self.launch is not None else []):
                if focus in row:
                    j = row.index(focus) + (1 if action == "right" else -1)
                    if 0 <= j < len(row):
                        row[j].set_active(True)
                        row[j].grab_focus()
                    return
        sliding = isinstance(focus, (Gtk.Range, Gtk.SpinButton)) or focus in app._radio_paired_spin
        if action in ("up", "down") or (action in ("left", "right") and not sliding):
            if self._step(focus, action):
                return
        app._dialog_nav_default(self, action)

    plunderhub_inside_row = False

    def _set_inside(self, inside: bool):
        self.plunderhub_inside_row = inside
        keynav.remark(self)

    def _setting_line(self, focus: Gtk.Widget) -> Optional[List[Gtk.Widget]]:
        """The stop row focus is in, when it's a setting's row with more
        than one control."""
        for row in self._stops():
            if any(w is focus or focus.is_ancestor(w) for w in row) and len(row) > 1:
                setting = focus
                while setting is not None and not setting.get_style_context().has_class("gp-setting"):
                    setting = setting.get_parent()
                return row if setting is not None and all(w.is_ancestor(setting) for w in row) else None
        return None

    def _stops(self) -> List[List[Gtk.Widget]]:
        """What the arrows stop on, a row at a time from the top: the
        tabs, then the page's controls a line at a time (a setting's combo
        and its button together: Proton, Proton versions) - then the
        Launch options' Save / Cancel."""
        groups: List[Tuple[float, List[Gtk.Widget]]] = [(-2, list(self.tab_buttons.values()))]
        page = self.pages.get_visible_child()
        footer = self.launch.footer if self.launch is not None and self.launch.footer.get_visible() else None
        placed = []
        for widget in keynav._focusables(page, []):
            if widget in self.tab_buttons.values():
                continue
            xy = widget.translate_coordinates(page, 0, 0)
            if xy:
                placed.append((xy[-1] + widget.get_allocated_height() / 2, xy[-2], widget))
        # one stop row per line: what sits at the same height together
        for y, x, widget in sorted(placed, key=lambda p: (p[0], p[1])):
            if groups and groups[-1][0] >= 0 and abs(groups[-1][0] - y) <= 14:
                groups[-1][1].append(widget)
            else:
                groups.append((y, [widget]))
        if footer is not None:
            groups.append((1e9, keynav._focusables(footer, [])))
        return [widgets for _y, widgets in groups if widgets]

    def _step(self, focus: Gtk.Widget, action: str) -> bool:
        stops = self._stops()
        at = next(((r, row.index(w)) for r, row in enumerate(stops) for w in row
                   if w is focus or focus.is_ancestor(w)), None)
        if at is None:
            return False
        r, c = at
        if action in ("left", "right"):
            j = c + (1 if action == "right" else -1)
            if 0 <= j < len(stops[r]):
                stops[r][j].grab_focus()
            return True
        nr = r + (1 if action == "down" else -1)
        if not 0 <= nr < len(stops):
            return True
        if self.plunderhub_inside_row:
            self._set_inside(False)
        target = stops[nr]
        # onto a row of tabs: the one showing
        chips = list(self.launch.chip_buttons.values()) if self.launch is not None else []
        picked = next((w for w in target if isinstance(w, Gtk.RadioButton) and w.get_active()
                       and (w in chips or w in self.tab_buttons.values())), None)
        (picked or target[0]).grab_focus()
        return True

    def _proton_switched(self, choice: str):
        """The game was put on Proton-GE Latest from its hint: Launch
        options shows it (and won't save the old one back)."""
        if self.launch is not None:
            self.launch.proton_switched(choice)
        # the strip on the other tab says nothing anymore
        shown = self.pages.get_visible_child_name()
        for name, strip in (("overview", getattr(self, "_hint_strip", None)),
                            ("launch", getattr(self.launch, "hint_strip", None))):
            if strip is not None and name != shown:
                strip.hide()

    def _bring_hint(self) -> bool:
        """Glides the page down to the Proton hint's strip on the tab
        shown (a third of the way down the view) and lights it up."""
        shown = self.pages.get_visible_child_name()
        holder = (getattr(self.launch, "hint_strip", None) if shown == "launch"
                  else getattr(self, "_hint_strip", None))
        page = self.scroller.get_child()
        if isinstance(page, Gtk.Viewport):
            page = page.get_child()
        if holder is None or not holder.get_visible() or page is None:
            return False
        at = holder.translate_coordinates(page, 0, 0)
        if not at:
            return False
        adj = self.scroller.get_vadjustment()
        start = adj.get_value()
        goal = max(adj.get_lower(), min(adj.get_upper() - adj.get_page_size(),
                                        at[1] - adj.get_page_size() / 3))
        strip = holder.get_children()[0] if holder.get_children() else holder
        strip.get_style_context().add_class("ph-strip-lit")
        GLib.timeout_add(1600, lambda: strip.get_style_context().remove_class("ph-strip-lit") and False)
        began = {"t": None}

        def glide(_w, clock) -> bool:
            now = clock.get_frame_time()
            began["t"] = began["t"] or now
            k = min(1.0, (now - began["t"]) / 380_000)
            ease = 1 - (1 - k) ** 3
            adj.set_value(start + (goal - start) * ease)
            return k < 1.0
        if abs(goal - start) > 2:
            self.scroller.add_tick_callback(glide)
        return False

    def show_launch_options(self):
        if "launch" in self.tab_buttons:
            self.tab_buttons["launch"].set_active(True)

    def _show_tab(self, name: str):
        self.pages.set_visible_child_name(name)
        if self.launch is not None:
            self.launch.footer.set_visible(name == "launch")

    # art ----------------------------------------------------------------

    def _load_art(self):
        app, uid = self.app, self.game.uid
        cover = app._find_cached_artwork(app.covers_dir, uid)
        self.cover_pb = _load_scaled(cover, COVER_W, COVER_H, crop=True)
        self.icon_pb = _load_scaled(app._find_cached_artwork(app.game_icons_dir, uid), ICON, ICON)
        self.hero_path = app.hero_path(uid)
        self.hero_pb = None
        self.hero_for_width = 0
        # the files they move from, if they're animated
        self.hero_motion = app._art_animation(self.hero_path)
        self.cover_motion = app._art_animation(cover)
        self.banner.queue_draw()

    def art_changed(self, _kind: str):
        self._load_art()

    def _areas(self) -> Dict[str, tuple]:
        w = self.banner.get_allocated_width()
        return {"hero": (0, 0, w, HERO_H), "cover": (COVER_X, COVER_Y, COVER_W, COVER_H),
                "icon": (TEXT_X, HERO_H + 22, ICON, ICON)}

    def _hit(self, x: float, y: float) -> Optional[str]:
        for kind in ("icon", "cover", "hero"):
            ax, ay, aw, ah = self._areas()[kind]
            if ax <= x <= ax + aw and ay <= y <= ay + ah:
                return kind
        return None

    def _set_hover(self, kind: Optional[str]):
        if kind == self.hover:
            return
        self.hover = kind
        self.banner.queue_draw()
        window = self.banner.get_window()
        if window is not None:
            window.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "pointer") if kind else None)

    def _text(self, cr, text: str, x: float, y: float, size: float, bold: bool, rgba, width: float = 0,
              center: bool = False, heavy: bool = False):
        layout = PangoCairo.create_layout(cr)
        desc = self.banner.get_pango_context().get_font_description().copy()
        desc.set_size(int(size * Pango.SCALE))
        desc.set_weight(Pango.Weight.HEAVY if heavy else Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        layout.set_font_description(desc)
        if width:
            layout.set_width(int(width * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            if center:
                layout.set_alignment(Pango.Alignment.CENTER)
        layout.set_text(text, -1)
        cr.set_source_rgba(*rgba)
        cr.move_to(x, y)
        PangoCairo.show_layout(cr, layout)

    def _pill(self, cr, label: str, cx: float, cy: float):
        lw = 26 + 8.2 * len(label)
        _rounded(cr, cx - lw / 2, cy - 16, lw, 32, 16)
        cr.set_source_rgba(0, 0, 0, 0.72)
        cr.fill()
        self._text(cr, label, cx - lw / 2, cy - 9, 10, True, (1, 1, 1, 1), width=lw, center=True)

    def _draw_banner(self, widget, cr):
        w = widget.get_allocated_width()
        ctx = self.get_style_context()
        # Asked for in the state it's in: a style looked up in another one
        # is restyled for it and back, which asked for another frame - the
        # page repainted every frame, for nothing (80 % of a core).
        state = ctx.get_state()
        bg = ctx.get_property("background-color", state)
        bg_rgb = (bg.red, bg.green, bg.blue) if bg is not None and bg.alpha > 0 else (0.03, 0.1, 0.1)
        fg = ctx.get_color(state)
        accent = _accent_rgb(widget)
        cr.set_source_rgb(*bg_rgb)
        cr.paint()
        # (one that can't be read is tried again only at a new width - not
        # at every redraw of the page)
        if self.hero_pb is None and self.hero_path is not None and w > 1 and self.hero_for_width != w:
            self.hero_pb = _load_scaled(self.hero_path, w, HERO_H, crop=True)
            self.hero_for_width = w
        elif self.hero_pb is not None and self.hero_for_width != w and self.hero_path is not None:
            self.hero_pb = _load_scaled(self.hero_path, w, HERO_H, crop=True)
            self.hero_for_width = w
        self.hero_anim.set(self.hero_motion, w, HERO_H)
        cr.save()
        cr.rectangle(0, 0, w, HERO_H)
        cr.clip()
        if self.hero_anim.paint(cr, w, HERO_H):
            pass
        elif self.hero_pb is not None:
            Gdk.cairo_set_source_pixbuf(cr, self.hero_pb, 0, 0)
            cr.paint()
        else:
            # no hero yet: the cover's colours, blurred into the header
            g = cairo.LinearGradient(0, 0, w, HERO_H)
            g.add_color_stop_rgba(0, *accent, 0.28)
            g.add_color_stop_rgba(1, *bg_rgb, 1)
            cr.set_source(g)
            cr.paint()
        cr.restore()
        if self.hover == "hero":
            cr.set_source_rgba(0, 0, 0, 0.25)
            cr.rectangle(0, 0, w, HERO_H)
            cr.fill()
        fade = cairo.LinearGradient(0, HERO_H * 0.35, 0, HERO_H)
        fade.add_color_stop_rgba(0, *bg_rgb, 0)
        fade.add_color_stop_rgba(1, *bg_rgb, 1)
        cr.set_source(fade)
        cr.rectangle(0, 0, w, HERO_H)
        cr.fill()
        if self.hover == "hero":
            self._pill(cr, self._("gp_change_hero"), w / 2, HERO_H / 2)

        x, y, cw, ch = COVER_X, COVER_Y, COVER_W, COVER_H
        _rounded(cr, x + 3, y + 8, cw, ch, 12)
        cr.set_source_rgba(0, 0, 0, 0.45)
        cr.fill()
        cr.save()
        _rounded(cr, x, y, cw, ch, 12)
        cr.clip()
        self.cover_anim.set(self.cover_motion, cw, ch)
        cr.save()
        cr.translate(x, y)
        moving = self.cover_anim.paint(cr, cw, ch)
        cr.restore()
        if moving:
            pass
        elif self.cover_pb is not None:
            Gdk.cairo_set_source_pixbuf(cr, self.cover_pb, x + (cw - logical_size(self.cover_pb)[0]) / 2, y)
            cr.paint()
        else:
            cr.set_source_rgba(1, 1, 1, 0.06)
            cr.paint()
        if self.hover == "cover":
            cr.set_source_rgba(0, 0, 0, 0.4)
            cr.paint()
        cr.restore()
        if self.hover == "cover":
            cr.set_source_rgb(*accent)
            cr.set_line_width(3)
            _rounded(cr, x - 1.5, y - 1.5, cw + 3, ch + 3, 13.5)
            cr.stroke()
            self._pill(cr, self._("gp_change_cover"), x + cw / 2, y + ch / 2)

        ix, iy, iw, ih = self._areas()["icon"]
        cr.save()
        _rounded(cr, ix, iy, iw, ih, 12)
        cr.clip()
        if self.icon_pb is not None:
            Gdk.cairo_set_source_pixbuf(cr, self.icon_pb, ix + (iw - logical_size(self.icon_pb)[0]) / 2,
                                        iy + (ih - logical_size(self.icon_pb)[1]) / 2)
            cr.paint()
        else:
            cr.set_source_rgba(1, 1, 1, 0.06)
            cr.paint()
        if self.hover == "icon":
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.paint()
            self._text(cr, self._("gp_change"), ix, iy + ih / 2 - 7, 8, True, (1, 1, 1, 1), width=iw, center=True)
        cr.restore()
        if self.hover == "icon":
            cr.set_source_rgb(*accent)
            cr.set_line_width(2.5)
            _rounded(cr, ix - 1.5, iy - 1.5, iw + 3, ih + 3, 13.5)
            cr.stroke()

        app, game = self.app, self.game
        text_w = w - (ix + iw + 16) - 28
        self._text(cr, game.title, ix + iw + 16, iy, 24, True, (fg.red, fg.green, fg.blue, 1), width=text_w,
                   heavy=True)
        last = app.recent.get(game.uid)
        meta = [game.launcher, app._home_playtime_text(game.uid)]
        if last:
            meta.append(self._("home_last_played").format(when=app._home_ago_text(last)))
        self._text(cr, "  ·  ".join(meta), ix + iw + 16, iy + 36, 11, False, (fg.red, fg.green, fg.blue, 0.62),
                   width=text_w)
        return False

    def _on_banner_click(self, _w, event) -> bool:
        kind = self._hit(event.x, event.y)
        if kind is None or event.button != 1:
            return False
        if self._art_menu is not None:
            # a click on the art while its menu is open just closes it
            self._art_menu.popdown()
            return True
        pop = Gtk.Popover()
        _close_on_shadow_click(pop)
        self._art_menu = pop
        pop.connect("closed", lambda *_: setattr(self, "_art_menu", None))
        pop.get_style_context().add_class("game-menu")
        pop.set_relative_to(self.banner)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(event.x), int(event.y), 1, 1
        pop.set_pointing_to(rect)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        app, game = self.app, self.game
        for icon_name, text, action in (
                ("image-up-symbolic", self._("gp_upload"), lambda: app._set_art_from_file(kind, game)),
                ("image-down-symbolic", self._("gp_choose_sgdb"), lambda: ArtPicker(app, game, kind, self)),
                ("rotate-ccw-symbolic", self._("gp_reset"), lambda: app._reset_art(kind, game))):
            b = Gtk.Button()
            b.get_style_context().add_class("gm-row")
            hb = Gtk.Box(spacing=12)
            hb.pack_start(_icon(icon_name, 18), False, False, 0)
            hb.pack_start(Gtk.Label(label=text, xalign=0), True, True, 0)
            b.add(hb)
            b.connect("clicked", lambda *_a, f=action: (pop.popdown(), GLib.idle_add(lambda: f() and False)))
            box.pack_start(b, False, False, 0)
        pop.add(box)
        box.show_all()
        pop.popup()
        return True

    # overview ---------------------------------------------------------------

    def _card(self, label: str, value: str, action: Optional[Callable] = None):
        # everything centred; a card that opens something says so under
        # its value, where it doesn't push the label off the middle
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        lab = Gtk.Label(label=label.upper(), xalign=0.5)
        # one line, always: left free, a two-word label asked for the
        # height of two and sat higher than its neighbours
        lab.set_single_line_mode(True)
        lab.set_ellipsize(Pango.EllipsizeMode.END)
        lab.get_style_context().add_class("gp-card-label")
        val = Gtk.Label(label=value, xalign=0.5)
        val.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        val.get_style_context().add_class("gp-card-value")
        box.pack_start(lab, False, False, 0)
        box.pack_start(val, False, False, 0)
        if action is not None:
            hint = Gtk.Label(label=self._("gp_open_hint"), xalign=0.5)
            hint.get_style_context().add_class("gp-open-hint")
            box.pack_start(hint, False, False, 0)
        # the cards of a row stretch to its tallest: the text stays in the
        # middle of each, not at the top
        box.set_valign(Gtk.Align.CENTER)
        if action is None:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            card.get_style_context().add_class("gp-card")
            card.pack_start(box, True, True, 0)
            return card, val
        btn = Gtk.Button()
        btn.get_style_context().add_class("gp-card-btn")
        btn.add(box)
        btn.connect("clicked", lambda *_: action())
        return btn, val

    def _overview(self) -> Gtk.Widget:
        app, game, _ = self.app, self.game, self._
        grid = Gtk.Grid(column_spacing=14, row_spacing=14, column_homogeneous=True)
        grid.set_border_width(28)
        version_card, self.version_val = self._card(_("gp_version"), "…")
        runner_card, self.runner_val = self._card(_("gp_runs_with"), "…")
        size_card, self.size_val = self._card(_("gp_size"), _("gp_measuring"))
        grid.attach(version_card, 0, 0, 1, 1)
        grid.attach(runner_card, 1, 0, 1, 1)
        grid.attach(size_card, 2, 0, 1, 1)
        # a newer Proton release mentions this game (ui/proton_hints.py)
        from plunderhub.ui.proton_hints import hint_strip
        hint = self._hint_strip = hint_strip(app, game, lambda: self, on_switched=self._proton_switched)
        grid.insert_row(0)
        grid.attach(hint, 0, 0, 3, 1)

        # where it lives - each one opens on a click
        places = []
        if game.install_dir:
            places.append((_("gp_installed_in"), game.install_dir,
                           (lambda: app._open_game_folder(game)) if Path(game.install_dir).exists() else None))
        saves = app._resolve_save_dir(game)
        if saves is not None:
            places.append((_("gp_saves"), str(saves), lambda: app._open_game_saves(game)))
        prefix = app._resolve_wine_prefix(game)
        if prefix is not None:
            places.append((_("gp_prefix"), str(prefix), lambda: app._open_wineprefix_folder(game)))
        if places:
            inner = Gtk.Grid(column_spacing=14, column_homogeneous=True)
            span = 6 // len(places)
            for i, (label, value, action) in enumerate(places):
                card, _v = self._card(label, value, action)
                inner.attach(card, i * span, 0, span, 1)
            grid.attach(inner, 0, 1, 3, 1)

        last = app.recent.get(game.uid)
        launcher = game.launcher + (f" · {_('store_' + game.store_platform)}"
                                    if game.launcher == "Heroic" and game.store_platform else "")
        # a box, not the grid's columns: the ProtonDB card joins it as a
        # fourth once its rating is in (and only if there is one)
        self._facts_row = Gtk.Box(spacing=14, homogeneous=True)
        for label, value in ((_("gp_launcher"), launcher),
                             (_("gp_playtime"), app._home_playtime_text(game.uid)),
                             (_("gp_last_played"), app._home_ago_text(last) if last
                              else _("home_never_played_short"))):
            card, _v = self._card(label, value)
            self._facts_row.pack_start(card, True, True, 0)
        grid.attach(self._facts_row, 0, 2, 3, 1)
        if app.settings.get("protondb_enabled", True):
            threading.Thread(target=self._fetch_protondb, daemon=True).start()
        if runner_of(game.uid) is not None and not updates_before_launch(game.uid):
            note = Gtk.Label(label=_("heroic_update_auto_off_text"), xalign=0)
            note.set_line_wrap(True)
            note.get_style_context().add_class("gp-note")
            grid.attach(note, 0, 3, 3, 1)
        threading.Thread(target=self._measure, daemon=True).start()
        return grid

    def _fetch_protondb(self):
        from plunderhub.core.protondb import rating
        try:
            found = rating(self.game.uid, self.game.title)
        except Exception as e:
            logger.debug(f"No ProtonDB rating for {self.game.title!r}: {e}")
            found = None
        if found is not None:
            GLib.idle_add(self._show_protondb, found)

    def _show_protondb(self, found) -> bool:
        """The ProtonDB card: the tier in its colour, how many reports
        (and that it's the Steam version's, for a game from elsewhere);
        a click opens the game's ProtonDB page."""
        if self.get_window() is None:
            return False
        _ = self._
        value = _("pdb_pending") if found.tier == "pending" else found.tier.capitalize()
        card, val = self._card("ProtonDB", value, lambda: webbrowser.open(found.url))
        val.get_style_context().add_class(f"pdb-tier-{found.tier}")
        sub = Gtk.Label(label=_("pdb_reports_steam" if found.by_title else "pdb_reports").format(n=found.total),
                        xalign=0.5)
        sub.set_justify(Gtk.Justification.CENTER)
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        sub.get_style_context().add_class("gp-card-sub")
        box = val.get_parent()
        box.pack_start(sub, False, False, 0)
        box.reorder_child(sub, 2)
        if found.tier == "native" and found.proton:
            # a Linux version on Steam - and how the Windows one runs with
            # Proton, which is what a copy from another store may well be
            with_proton = Gtk.Label(xalign=0.5)
            with_proton.set_markup(GLib.markup_escape_text(_("pdb_with_proton")).replace(
                "{tier}", f'<span weight="bold" foreground="{TIER_COLOURS[found.proton]}">'
                          f'{found.proton.capitalize()}</span>'))
            with_proton.get_style_context().add_class("gp-card-sub")
            box.pack_start(with_proton, False, False, 0)
            box.reorder_child(with_proton, 2)
        card.set_tooltip_text(found.url)
        self._facts_row.pack_start(card, True, True, 0)
        card.show_all()
        return False

    def _measure(self):
        game, app = self.game, self.app
        size = None
        try:
            if game.install_dir and Path(game.install_dir).exists():
                size = app._dir_size_bytes(Path(game.install_dir))
        except Exception as e:
            logger.debug(f"Couldn't measure {game.install_dir}: {e}")
        version = runner = None
        try:
            version = app._resolve_game_version(game)
        except Exception as e:
            logger.debug(f"Couldn't read the version of {game.title!r}: {e}")
        try:
            runner = app._resolve_runner_version(game)
            # a Latest by the release it holds, and that it's the Latest
            from plunderhub.core.proton_store import latest_tag
            tag = latest_tag(runner)
            if tag:
                runner = self._("gp_proton_latest").format(tag=tag)
        except Exception as e:
            logger.debug(f"Couldn't read the runner of {game.title!r}: {e}")
        GLib.idle_add(self._measured, size, version, runner)

    def _measured(self, size, version, runner) -> bool:
        if self.get_window() is None:
            return False
        self.size_val.set_text(format_bytes(size) if size is not None else "-")
        self.version_val.set_text(version or self._("gp_unknown"))
        self.runner_val.set_text(runner or self._("gp_native"))
        return False


# ProtonDB's own tier colours (theme.py's .pdb-tier-* has the same)
TIER_COLOURS = {"platinum": "#b4c7dc", "gold": "#cfb53b", "silver": "#a6a6a6", "bronze": "#cd7f32",
                "borked": "#ff4d4d"}


# --- launch options --------------------------------------------------------------------------

class LaunchOptionsTab(Gtk.Box):
    """Everything the old Launch options window had, as cards. The widgets
    sit in `ui` under the old keys, so the LaunchOptionsMixin reads, writes
    and saves them as before."""

    def __init__(self, app, game: GameItem, backend, page: GamePage):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.app, self.game, self.backend, self.page = app, game, backend, page
        self._ = app._
        self.loaded = backend.load(game)
        self.opts = copy.deepcopy(self.loaded)
        self.ui: Dict[str, object] = {}
        self.flatpak = bool(game.launch_cmd) and game.launch_cmd[0] == "flatpak"
        self.missing = self._missing_tools()
        self.tool_warnings: Dict[str, Gtk.Widget] = {}
        self.set_border_width(28)

        for text, icon_name, closable in self._notices():
            notice = self._notice(text, icon_name, backend.launcher if closable else None)
            self.pack_start(notice, False, False, 0)

        chips = Gtk.Box(spacing=8)
        self.stack = Gtk.Stack()
        self.stack.set_vhomogeneous(False)
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        first = None
        try:
            self.prefix = backend.prefix(game)
        except Exception as e:
            logger.debug(f"No Wine prefix for {game.title!r}: {e}")
            self.prefix = None
        sections = [("general", "lo_tab_general"), ("gamescope", "lo_tab_gamescope"), ("env", "lo_tab_env")]
        if self.prefix is not None:
            sections.append(("prefix", "lo_tab_prefix"))
        self.chip_buttons: Dict[str, Gtk.RadioButton] = {}
        for name, key in sections + [("text", "lo_tab_text")]:
            rb = Gtk.RadioButton.new_with_label_from_widget(first, self._(key))
            rb.set_mode(False)
            rb.get_style_context().add_class("gp-chip")
            first = first or rb
            rb.connect("toggled", lambda b, n=name: b.get_active() and self._show_section(n))
            chips.pack_start(rb, False, False, 0)
            self.chip_buttons[name] = rb
        self.pack_start(chips, False, False, 0)
        self.stack.add_named(self._general(), "general")
        self.stack.add_named(self._gamescope(), "gamescope")
        self.stack.add_named(self._env(), "env")
        if self.prefix is not None:
            self.stack.add_named(self._prefix_page(), "prefix")
        self.stack.add_named(self._text_mode(), "text")
        self.pack_start(self.stack, False, False, 0)
        self.current = "general"

        app._lo_write_widgets(self.ui, self.loaded)
        self._rebuild_env_rows()
        self.unsupported = backend.unsupported(game)
        for key in self.unsupported:
            widget = self.ui.get(key)
            if widget is not None:
                widget.set_sensitive(False)
                widget.set_tooltip_text(self._("lo_unsupported"))
        self._wire_gamescope_conflicts()
        if "native" in self.ui:
            self.ui["native"].connect("toggled", lambda s: app._lo_apply_native(self.ui, s.get_active()))
        self.footer = self._footer()

    # building blocks -----------------------------------------------------------

    def _notices(self):
        backend = self.backend
        notices = []
        if backend.note_key:
            notices.append((self._(backend.note_key), "triangle-alert-symbolic", False))
        if backend.blocking_process_running():
            notices.append((self._("lo_steam_running"), "triangle-alert-symbolic", True))
        elif backend.advisory_process_running():
            notices.append((self._("lo_launcher_running").format(launcher=backend.launcher),
                            "dialog-information-symbolic", True))
        return notices

    def _notice(self, text: str, icon_name: str, close_launcher: Optional[str] = None) -> Gtk.Widget:
        """close_launcher: the running launcher it's about, with a
        button that closes it - the notice goes once it's closed."""
        box = Gtk.Box(spacing=12)
        box.get_style_context().add_class("gp-notice")
        box.pack_start(_icon(icon_name, 18), False, False, 0)
        lab = Gtk.Label(label=text, xalign=0)
        lab.set_line_wrap(True)
        box.pack_start(lab, True, True, 0)
        if close_launcher:
            close = self.app._dialog_button(self._("lo_close_launcher").format(launcher=close_launcher), "ghost")
            close.set_valign(Gtk.Align.CENTER)

            def clicked(*_):
                if self.app.close_launcher_asking(close_launcher, self.page):
                    box.destroy()
            close.connect("clicked", clicked)
            box.pack_end(close, False, False, 0)
        return box

    def _row(self, title: str, sub: Optional[str] = None, control: Optional[Gtk.Widget] = None,
             pill: Optional[str] = None) -> Gtk.Box:
        row = Gtk.Box(spacing=12)
        row.get_style_context().add_class("gp-setting")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        texts.set_valign(Gtk.Align.CENTER)
        top = Gtk.Box(spacing=8)
        top.pack_start(Gtk.Label(label=title, xalign=0), False, False, 0)
        if pill:
            p = Gtk.Label(label=pill)
            p.get_style_context().add_class("settings-pill")
            p.set_valign(Gtk.Align.CENTER)
            top.pack_start(p, False, False, 0)
        texts.pack_start(top, False, False, 0)
        if sub:
            s = Gtk.Label(label=sub, xalign=0)
            s.set_line_wrap(True)
            s.get_style_context().add_class("gp-note")
            texts.pack_start(s, False, False, 0)
        row.pack_start(texts, True, True, 0)
        if control is not None:
            control.set_valign(Gtk.Align.CENTER)
            row.pack_end(control, False, False, 0)
        return row

    def _section(self, icon_name: str, title: str, rows: List[Gtk.Widget],
                 extra: Optional[Gtk.Widget] = None) -> Gtk.Box:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.get_style_context().add_class("gp-card")
        head = Gtk.Box(spacing=8)
        head.set_margin_bottom(6)
        head.pack_start(_icon(icon_name, 16, self.app._home_accent_hex()), False, False, 0)
        lab = Gtk.Label(label=title.upper(), xalign=0)
        lab.get_style_context().add_class("gp-card-label")
        head.pack_start(lab, False, False, 0)
        if extra is not None:
            extra.set_valign(Gtk.Align.CENTER)
            head.pack_end(extra, False, False, 0)
        card.pack_start(head, False, False, 0)
        for i, row in enumerate(rows):
            if i:
                sep = Gtk.Separator()
                sep.get_style_context().add_class("gm-sep")
                card.pack_start(sep, False, False, 0)
            card.pack_start(row, False, False, 0)
        return card

    def _toggle(self, key: str, tool: Optional[str] = None) -> Gtk.Widget:
        """A switch registered under key - with a warning beside it when its
        tool isn't installed."""
        sw = ToggleSwitch()
        self.ui[key] = sw
        if tool and self.missing.get(tool):
            # Clicking it opens Shipwright on that tool; once it's
            # installed there, the warning goes.
            box = Gtk.Box(spacing=6)
            warn = Gtk.Button()
            warn.get_style_context().add_class("gp-tool-missing")
            warn.set_relief(Gtk.ReliefStyle.NONE)
            inner = Gtk.Box(spacing=6)
            inner.pack_start(_icon("triangle-alert-symbolic", 16, "#e8a33d"), False, False, 0)
            inner.pack_start(Gtk.Label(label=self._("install_btn")), False, False, 0)
            warn.add(inner)
            warn.set_valign(Gtk.Align.CENTER)
            warn.set_tooltip_text(self._("lo_tool_missing_shipwright").format(tool=TOOLS_BY_ID[tool].name))
            warn.connect("clicked", lambda *_: self._open_shipwright(f"flatpak:{tool}" if self.flatpak else tool))
            self.tool_warnings[tool] = warn
            box.pack_start(warn, False, False, 0)
            box.pack_start(sw, False, False, 0)
            return box
        return sw

    def _goverlay_button(self) -> Optional[Gtk.Widget]:
        """MangoHud's and vkBasalt's settings, in GOverlay - only while
        it's installed (natively or from Flathub)."""
        from plunderhub.ui import launcher_tasks
        argv = launcher_tasks.launch_argv("GOverlay")
        if not argv:
            return None
        btn = Gtk.Button()
        inner = Gtk.Box(spacing=6)
        inner.pack_start(_icon("preferences-other-symbolic", 14), False, False, 0)
        inner.pack_start(Gtk.Label(label="GOverlay"), False, False, 0)
        btn.add(inner)
        btn.set_tooltip_text(self._("gp_goverlay_tooltip"))
        btn.set_focus_on_click(False)

        def start(*_):
            import subprocess
            try:
                subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                logger.warning(f"Couldn't start GOverlay: {e}")
        btn.connect("clicked", start)
        return btn

    def _missing_tools(self) -> Dict[str, bool]:
        """A Flatpak launcher can't see the system's tools - it needs
        their Vulkan layers from Flathub, for its own runtime."""
        if not self.flatpak:
            return missing_tools()
        cmd = self.game.launch_cmd
        app_id = next((w for w in cmd[2:] if not w.startswith("-")), None) if cmd[1:2] == ["run"] else None
        return flatpak_layers.missing_for_app(app_id) if app_id else {}

    def _open_shipwright(self, tool: str):
        def closed():
            self.missing = self._missing_tools()
            for key, warn in self.tool_warnings.items():
                if not self.missing.get(key):
                    warn.hide()
        self.app.show_shipwright(focus=tool, parent=self.get_toplevel(), on_closed=closed)

    def _fill_proton(self, combo: Gtk.ComboBoxText, current: str):
        """The Proton list as the Proton page's games list has it (see
        proton_combo_items) - the launcher's default merged with Proton-GE
        Latest when they're the same, one row saying so with none at all."""
        from plunderhub.ui.proton_page import launcher_default_name, proton_combo_items
        choices = [(c.id, c.label) for c in self.backend.proton_choices(self.game, current or None)]
        items, active, any_proton = proton_combo_items(self.app, choices, current,
                                                       launcher_default_name(self.backend))
        for choice_id, label in items:
            combo.append(choice_id, label)
        combo.set_active_id(active)
        combo.set_sensitive(any_proton)
        # the game set to the Latest itself, shown on the default row: kept
        # as it is unless another one is picked (see _read)
        self._proton_alias = current if active != current else None

    def proton_switched(self, choice: str):
        combo = self.ui.get("proton")
        self.loaded.proton = choice or None
        self.opts.proton = choice or None
        if combo is not None:
            combo.remove_all()
            self._fill_proton(combo, choice or "")

    def _open_proton_page(self):
        """Shipwright's Proton page; once it closes, the Proton list here
        has what was downloaded or removed there."""
        def closed():
            combo = self.ui.get("proton")
            if combo is None:
                return
            picked = combo.get_active_id() or ""
            combo.remove_all()
            self._fill_proton(combo, picked or self.loaded.proton or "")
        self.app.show_proton_page(parent=self.get_toplevel(), on_closed=closed)

    def _entry(self, key: str, chars: int = 6) -> Gtk.Entry:
        e = Gtk.Entry()
        e.set_width_chars(chars)
        self.ui[key] = e
        return e

    def _combo(self, key: str, items) -> Gtk.ComboBoxText:
        c = Gtk.ComboBoxText()
        for item_id, text in items:
            c.append(item_id, text)
        c.set_active(0)
        self.ui[key] = c
        return c

    # pages ----------------------------------------------------------------------

    def _general(self) -> Gtk.Box:
        _, game, backend = self._, self.game, self.backend
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)

        recommend = self.app._dialog_button(_("lo_recommended_btn"), "accent")
        recommend.set_valign(Gtk.Align.CENTER)
        recommend.connect("clicked", lambda *_a: self._apply_recommended())
        col.pack_start(self._section("non-starred-symbolic", _("lo_recommended"), [
            self._row(_("lo_recommended_title"), None, recommend)]), False, False, 0)

        proton = Gtk.ComboBoxText()
        self._fill_proton(proton, self.loaded.proton or "")
        self.ui["proton"] = proton
        # ProtonPlus downloads more runners for the list
        protonplus = Gtk.Button()
        inner = Gtk.Box(spacing=6)
        inner.pack_start(_icon("wine-symbolic", 14), False, False, 0)
        inner.pack_start(Gtk.Label(label=_("pm_manage")), False, False, 0)
        protonplus.add(inner)
        protonplus.set_valign(Gtk.Align.CENTER)
        protonplus.connect("clicked", lambda *_: self._open_proton_page())
        proton_box = Gtk.Box(spacing=8)
        proton_box.pack_start(proton, False, False, 0)
        proton_box.pack_start(protonplus, False, False, 0)
        # "Recommended" only means something next to the "Run natively" choice
        native = backend.native_capable(game)
        proton_row = self._row(_("lo_proton_label").rstrip(":"), None, proton_box,
                               pill=_("task_recommended") if native else None)
        self.ui["proton_row"] = proton_row
        runtime = [proton_row]
        if native:
            runtime.insert(0, self._row(_("gp_lo_native"), _("gp_lo_native_sub"), self._toggle("native")))
        col.pack_start(self._section("rocket-symbolic", _("gp_runs_with"), runtime), False, False, 0)
        # a newer Proton release mentions this game - also one for a game on another Proton
        from plunderhub.ui.proton_hints import hint_strip
        self.hint_strip = hint_strip(self.app, game, lambda: self.get_toplevel(),
                                     on_switched=self.page._proton_switched, soft=True)
        col.pack_start(self.hint_strip, False, False, 0)

        fps_box = Gtk.Box(spacing=8)
        fps_entry = self._entry("fps_entry", 5)
        fps_check = ToggleSwitch()
        self.ui["fps_check"] = fps_check
        fps_check.connect("toggled", lambda s: fps_entry.set_sensitive(s.get_active()))
        fps_box.pack_start(fps_entry, False, False, 0)
        fps_box.pack_start(fps_check, False, False, 0)
        col.pack_start(self._section("sparkles-symbolic", _("lo_section_performance"), [
            self._row(_("lo_gamemode"), _("gp_lo_gamemode_sub"), self._toggle("gamemode", "gamemode")),
            self._row(_("lo_mangohud"), _("gp_lo_mangohud_sub"), self._toggle("mangohud", "mangohud")),
            self._row(_("lo_vkbasalt"), _("gp_lo_vkbasalt_sub"), self._toggle("vkbasalt", "vkbasalt")),
            self._row(_("lo_fps_limit"), _("gp_lo_fps_sub"), fps_box)], self._goverlay_button()), False, False, 0)

        compat = [self._row(_(label_key), _(sub_key), self._toggle(key)) for key, label_key, sub_key in (
            ("esync", "lo_esync", "gp_lo_esync_sub"), ("fsync", "lo_fsync", "gp_lo_fsync_sub"),
            ("wayland", "lo_wayland", "gp_lo_wayland_sub"), ("hdr", "lo_hdr", "gp_lo_hdr_sub"),
            ("fsr", "lo_fsr", "gp_lo_fsr_sub"), ("nvapi", "lo_nvapi", "gp_lo_nvapi_sub"),
            ("proton_log", "lo_proton_log", "gp_lo_proton_log_sub"))]
        compat_card = self._section("wind-symbolic", _("lo_section_compat"), compat)
        self.ui["compat_grid"] = compat_card
        col.pack_start(compat_card, False, False, 0)

        if backend.anticheat_mode != "steam":
            # With anticheat_mode None they're shown switched off (see unsupported()).
            anticheat = [self._row(_("lo_battleye"), _("gp_lo_battleye_sub"), self._toggle("battleye")),
                         self._row(_("lo_eac"), _("gp_lo_eac_sub"), self._toggle("eac"))]
        else:
            # Steam has no per-game switch: it uses a runtime whenever it's
            # installed (Library -> Tools), so its state is shown instead.
            anticheat = []
            for which, label_key in (("battleye", "lo_battleye"), ("eac", "lo_eac")):
                installed = isinstance(backend, SteamBackend) and backend.runtime_installed(which)
                status = Gtk.Label(label=_("lo_runtime_installed" if installed else "lo_runtime_missing"))
                status.get_style_context().add_class("gp-status-on" if installed else "gp-note")
                anticheat.append(self._row(_(label_key), None, status))
            hint = Gtk.Label(label=_("lo_steam_runtime_hint"), xalign=0)
            hint.set_line_wrap(True)
            hint.get_style_context().add_class("gp-note")
            anticheat.append(hint)
        anticheat_card = self._section("key-round-symbolic", _("lo_section_anticheat"), anticheat)
        self.ui["anticheat_grid"] = anticheat_card
        col.pack_start(anticheat_card, False, False, 0)

        col.pack_start(self._launch_args_section(), False, False, 0)
        return col

    def _apply_recommended(self):
        """The recommended options in the dialog - after saying exactly
        what they change; stored with Save, as any other change."""
        from plunderhub.core import launch_defaults
        _, app = self._, self.app
        current = self._read()
        after = launch_defaults.recommended(current, app._lo_context(), self.backend, self.game, self.missing)
        found = launch_defaults.changes(current, after)
        if not found:
            app._show_info_dialog(_("lo_recommended_none"), Gtk.MessageType.INFO, transient_for=self.page)
            return

        def value(v: str) -> str:
            return _("lo_value_on") if v == "on" else _("lo_value_off") if v == "off" else v or _("lo_value_none")
        lines = [f"•  {_(name)}:  {value(before)}  →  {value(now)}" for name, before, now in found]
        response = app._card_dialog(
            _("lo_recommended_confirm_title"),
            _("lo_recommended_confirm_text") + "\n\n" + "\n".join(lines) + "\n\n" + _("lo_recommended_save_note"),
            [(_("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost"),
             (_("lo_recommended_apply"), Gtk.ResponseType.OK, "accent")],
            tone="question", default=Gtk.ResponseType.OK, transient_for=self.page)
        if response != Gtk.ResponseType.OK:
            return
        self.opts = after
        app._lo_write_widgets(self.ui, after)

    # General options gamescope does itself, or that don't work inside it:
    # off (and greyed out) while it's on, as they were once it's off again.
    GAMESCOPE_CONFLICTS = ("win_fullscreen", "win_borderless", "wayland", "fsr", "fps_check")
    _SUSPENDED_KEY = "gamescope_suspended"

    def _wire_gamescope_conflicts(self):
        """While gamescope is on, the conflicting switches are off; what
        they were is kept (in the settings too, for the next time the
        dialog opens) and given back when it's switched off."""
        keys = [k for k in self.GAMESCOPE_CONFLICTS if k in self.ui and k not in self.unsupported]
        kept = (self.app.settings.get(self._SUSPENDED_KEY) or {}).get(self.game.uid)
        self.suspended: Dict[str, bool] = dict(kept) if isinstance(kept, dict) else {}

        def suspend():
            for key in keys:
                sw = self.ui[key]
                self.suspended.setdefault(key, sw.get_active())
                sw.set_active(False)
                sw.set_sensitive(False)
                sw.set_tooltip_text(self._("lo_gs_conflict"))

        def resume():
            for key in keys:
                sw = self.ui[key]
                sw.set_sensitive(True)
                sw.set_tooltip_text(None)
                sw.set_active(self.suspended.get(key, sw.get_active()))
            self.suspended = {}

        def toggled(check):
            suspend() if check.get_active() else resume()
        self.ui["gs_enabled"].connect("toggled", toggled)
        if self.ui["gs_enabled"].get_active():
            suspend()

    def _keep_suspended(self, new_opts: LaunchOptions):
        """Saved: what gamescope switched off, for when it's off again."""
        settings = self.app.settings
        kept = dict(settings.get(self._SUSPENDED_KEY) or {})
        if new_opts.gamescope.enabled and self.suspended:
            kept[self.game.uid] = dict(self.suspended)
        else:
            kept.pop(self.game.uid, None)
        if kept != (settings.get(self._SUSPENDED_KEY) or {}):
            settings[self._SUSPENDED_KEY] = kept
            save_settings(settings)

    def _launch_args_section(self) -> Gtk.Box:
        """Switches for the game's own common arguments: a window mode
        (one at most) and skipping the game's launcher. They write their
        words into the arguments - kept in an entry that isn't shown
        (any others are typed in Text mode), so reading, writing and
        saving go through it as before."""
        _ = self._
        args = self._entry("args", 30)
        switches = {}
        rows = []
        for key, words in (("win_fullscreen", "-fullscreen"), ("win_borderless", "-windowed -borderless"),
                           ("launcher_skip", LAUNCHER_SKIP_ARG)):
            sw = ToggleSwitch()
            self.ui[key] = sw
            switches[key] = sw
            rows.append(self._row(_(f"lo_{key}"), f"{_(f'lo_{key}_sub')}  ·  {words}", sw))
        syncing = {"on": False}

        def from_args(*_a):
            """The switches as the arguments are - typed or loaded."""
            if syncing["on"]:
                return
            syncing["on"] = True
            try:
                text = args.get_text()
                mode = window_mode_in(text)
                switches["win_fullscreen"].set_active(mode == "fullscreen")
                switches["win_borderless"].set_active(mode == "borderless")
                switches["launcher_skip"].set_active(LAUNCHER_SKIP_ARG in text.split())
            finally:
                syncing["on"] = False

        def to_args(sw, key):
            if syncing["on"]:
                return
            syncing["on"] = True
            try:
                text = args.get_text()
                if key == "launcher_skip":
                    text = with_arg(text, LAUNCHER_SKIP_ARG, sw.get_active())
                else:
                    other = switches["win_borderless" if key == "win_fullscreen" else "win_fullscreen"]
                    if sw.get_active():
                        other.set_active(False)
                    mode = key[len("win_"):] if sw.get_active() else None
                    text = with_window_mode(text, mode)
                args.set_text(text)
            finally:
                syncing["on"] = False
        args.connect("changed", from_args)
        for key, sw in switches.items():
            sw.connect("toggled", to_args, key)
        note = Gtk.Label(label=_("lo_args_note"), xalign=0)
        note.set_line_wrap(True)
        note.get_style_context().add_class("gp-note")
        return self._section("input-keyboard-symbolic", _("lo_args_label"), rows + [note])

    def _gamescope(self) -> Gtk.Box:
        _, app = self._, self.app
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        col.pack_start(self._section("monitor-symbolic", _("lo_tab_gamescope"), [
            self._row(_("lo_gs_enable"), _("gp_lo_gs_enable_sub"), self._toggle("gs_enabled", "gamescope"))]),
            False, False, 0)

        def pair(prefix: str) -> Gtk.Box:
            box = Gtk.Box(spacing=6)
            box.pack_start(self._entry(f"{prefix}_w"), False, False, 0)
            box.pack_start(Gtk.Label(label="×"), False, False, 0)
            box.pack_start(self._entry(f"{prefix}_h"), False, False, 0)
            return box

        monitor = app._lo_monitor_resolution()
        preset = Gtk.ComboBoxText()
        preset.append("custom", _("lo_gs_preset_custom"))
        for name, factor in QUALITY_PRESETS:
            label = f"{_('lo_gs_preset_' + name)}  ·  {factor:g}×"
            preset.append(name, label)
        self.ui["gs_preset"] = preset
        methods = [(m, label) for m, label in (("", _("lo_gs_default")), ("fsr", "FSR"), ("nis", "NIS"),
                                               ("integer", _("lo_gs_integer")), ("stretch", _("lo_gs_stretch")))
                   if m in self.backend.upscale_methods]
        extra = self._entry("gs_additional", 30)
        grab = ToggleSwitch()
        self.ui["gs_grab"] = grab
        options = self._section("view-filter-symbolic", _("gp_lo_gs_options"), [
            self._row(_("lo_gs_preset").rstrip(":"), _("gp_lo_gs_preset_sub"), preset),
            self._row(_("lo_gs_game_res").rstrip(":"), None, pair("gs_game")),
            self._row(_("lo_gs_output_res").rstrip(":"), None, pair("gs_out")),
            self._row(_("lo_gs_upscale").rstrip(":"), None, self._combo("gs_upscale", methods)),
            self._row(_("lo_gs_window").rstrip(":"), None, self._combo("gs_window", (
                ("", _("lo_gs_default")), ("fullscreen", _("lo_gs_fullscreen")),
                ("borderless", _("lo_gs_borderless"))))),
            self._row(_("lo_gs_fps").rstrip(":"), None, self._entry("gs_fps")),
            self._row(_("lo_gs_fps_unfocused").rstrip(":"), None, self._entry("gs_fps_unfocused")),
            self._row(_("lo_gs_grab"), None, grab),
            self._row(_("lo_gs_additional").rstrip(":"), None, extra)])
        self.ui["gs_grid"] = options
        col.pack_start(options, False, False, 0)
        app._lo_wire_resolution_presets(self.ui, monitor)
        return col

    def _env(self) -> Gtk.Box:
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.ui["env_store"] = Gtk.ListStore(str, str)
        self.env_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.env_empty = Gtk.Label(label=self._("gp_lo_env_empty"), xalign=0)
        self.env_empty.get_style_context().add_class("gp-note")
        add = Gtk.Button()
        add.get_style_context().add_class("home-play")
        add.get_style_context().add_class("home-pill-ghost")
        hb = Gtk.Box(spacing=8)
        hb.pack_start(Gtk.Label(label="+"), False, False, 0)
        hb.pack_start(Gtk.Label(label=self._("lo_env_add")), False, False, 0)
        add.add(hb)
        add.set_halign(Gtk.Align.START)
        add.connect("clicked", lambda *_: self._add_env_row("", ""))
        col.pack_start(self._section("type-symbolic", self._("lo_tab_env"), [self.env_empty, self.env_rows, add]),
                       False, False, 0)
        return col

    def _rebuild_env_rows(self):
        """The rows from env_store - after _lo_write_widgets refilled it."""
        for child in self.env_rows.get_children():
            child.destroy()
        for key, value in [(r[0], r[1]) for r in self.ui["env_store"]]:
            self._add_env_row(key, value, store=False)
        self.env_empty.set_visible(len(self.ui["env_store"]) == 0)

    def _add_env_row(self, key: str, value: str, store: bool = True):
        env_store = self.ui["env_store"]
        if store:
            env_store.append([key, value])
        ref = Gtk.TreeRowReference.new(env_store, Gtk.TreePath(len(env_store) - 1))
        row = Gtk.Box(spacing=8)
        k = Gtk.Entry()
        k.set_text(key)
        k.set_placeholder_text(self._("lo_env_key"))
        k.set_width_chars(22)
        eq = Gtk.Label(label="=")
        eq.get_style_context().add_class("gp-note")
        v = Gtk.Entry()
        v.set_text(value)
        v.set_placeholder_text(self._("lo_env_value"))
        v.set_hexpand(True)
        remove = Gtk.Button()
        remove.get_style_context().add_class("gp-close")
        remove.add(_icon("window-close-symbolic", 14))
        remove.set_tooltip_text(self._("lo_env_remove"))

        def edited(entry, column):
            path = ref.get_path()
            if path is not None:
                env_store[path][column] = entry.get_text().strip() if column == 0 else entry.get_text()

        def removed(*_a):
            path = ref.get_path()
            if path is not None:
                env_store.remove(env_store.get_iter(path))
            row.destroy()
            self.env_empty.set_visible(len(env_store) == 0)
        k.connect("changed", edited, 0)
        v.connect("changed", edited, 1)
        remove.connect("clicked", removed)
        for widget, grow in ((k, False), (eq, False), (v, True), (remove, False)):
            row.pack_start(widget, grow, grow, 0)
        self.env_rows.pack_start(row, False, False, 0)
        row.show_all()
        self.env_empty.set_visible(False)
        if store:
            k.grab_focus()

    def _text_mode(self) -> Gtk.Box:
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        top = Gtk.Box(spacing=8)
        hint = Gtk.Label(label=self._("gp_lo_text_sub"), xalign=0)
        hint.get_style_context().add_class("gp-note")
        top.pack_start(hint, True, True, 0)
        info = Gtk.Button()
        info.get_style_context().add_class("gp-info")
        info.add(_icon("dialog-information-symbolic", 16))
        pop = Gtk.Popover()
        pop.set_relative_to(info)
        pop.get_style_context().add_class("game-menu")
        long_text = Gtk.Label(label=self._("lo_text_hint"), xalign=0)
        long_text.set_line_wrap(True)
        long_text.set_max_width_chars(46)
        long_text.set_margin_start(8)
        long_text.set_margin_end(8)
        long_text.set_margin_top(6)
        long_text.set_margin_bottom(6)
        pop.add(long_text)
        long_text.show()
        info.connect("clicked", lambda *_: pop.popup())
        top.pack_end(info, False, False, 0)
        order = Gtk.Box(spacing=6)
        order.set_margin_top(8)
        order.set_margin_bottom(10)
        for part in ("VARIABLE=value", "wrappers", "%command%", "game arguments"):
            chip = Gtk.Label(label=part)
            chip.get_style_context().add_class("gp-code-chip")
            order.pack_start(chip, False, False, 0)
        view = Gtk.TextView()
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_monospace(True)
        for side in ("left", "right", "top", "bottom"):
            getattr(view, f"set_{side}_margin")(10)
        view.set_size_request(-1, 160)
        view.get_style_context().add_class("gp-text")
        self.ui["text"] = view
        col.pack_start(self._section("scroll-text-symbolic", self._("lo_tab_text"), [top, order, view]),
                       False, False, 0)
        return col

    # switching, saving --------------------------------------------------------------

    # the Wine prefix ------------------------------------------------------------

    def _prefix_page(self) -> Gtk.Box:
        """Where the game's prefix is and what runs it, and winecfg,
        Winetricks (Protontricks for Steam) and Run EXE on it - Heroic's
        Wine tab, for every launcher."""
        _, info = self._, self.prefix
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        if info.problem:
            col.pack_start(self._notice(_(info.problem), "dialog-information-symbolic"), False, False, 0)

        path = Gtk.Label(label=info.prefix or "-", xalign=0)
        path.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        path.set_selectable(True)
        path.set_can_focus(False)       # the arrows go to its folder button
        path.get_style_context().add_class("gp-note")
        folder = Gtk.Button()
        folder.add(_icon("folder-symbolic", 16))
        folder.set_tooltip_text(_("pfx_open_folder"))
        folder.set_sensitive(bool(info.prefix))
        folder.connect("clicked", lambda *_: self._open_folder(info.prefix))
        path_row = Gtk.Box(spacing=12)
        path_row.get_style_context().add_class("gp-setting")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        texts.pack_start(Gtk.Label(label=_("pfx_path"), xalign=0), False, False, 0)
        texts.pack_start(path, False, False, 0)
        path_row.pack_start(texts, True, True, 0)
        folder.set_valign(Gtk.Align.CENTER)
        path_row.pack_end(folder, False, False, 0)
        runner = _("pfx_runner_steam") if info.mode == "steam" else info.runner or "-"
        runner_label = Gtk.Label(label=runner)
        runner_label.get_style_context().add_class("gp-note")
        col.pack_start(self._section("folder-symbolic", _("pfx_section"), [
            path_row, self._row(_("pfx_runner"), None, runner_label)]), False, False, 0)

        tricks = "Protontricks" if info.mode == "steam" else "Winetricks"
        self.pfx_status = Gtk.Label(xalign=0)
        self.pfx_status.set_line_wrap(True)
        self.pfx_status.get_style_context().add_class("gp-note")
        self.pfx_status.set_no_show_all(True)
        rows = [self._row("Winecfg", _("pfx_winecfg_sub"), self._pfx_button(prefix_tools.WINECFG, _("sw_open"))),
                self._row(tricks, _("pfx_tricks_sub"), self._pfx_button(prefix_tools.TRICKS, _("sw_open"))),
                self._row(_("pfx_run"), _("pfx_run_sub"), self._pfx_button(prefix_tools.RUN_EXE, _("pfx_browse"))),
                self.pfx_status]
        col.pack_start(self._section("hammer-symbolic", _("pfx_tools"), rows), False, False, 0)
        return col

    def _pfx_button(self, action: str, text: str) -> Gtk.Widget:
        """The action's button - or, while its tool isn't installed, the
        amber Install one that opens Shipwright on it."""
        box = Gtk.Box(spacing=6)
        btn = Gtk.Button(label=text)
        btn.set_sensitive(not self.prefix.problem)
        btn.connect("clicked", lambda *_: self._pfx_run(action))
        tool = prefix_tools.needed_tool(self.prefix, action)
        if tool and not self.prefix.problem:
            warn = Gtk.Button()
            warn.get_style_context().add_class("gp-tool-missing")
            warn.set_relief(Gtk.ReliefStyle.NONE)
            inner = Gtk.Box(spacing=6)
            inner.pack_start(_icon("triangle-alert-symbolic", 16, "#e8a33d"), False, False, 0)
            inner.pack_start(Gtk.Label(label=self._("install_btn")), False, False, 0)
            warn.add(inner)
            warn.set_tooltip_text(self._("lo_tool_missing_shipwright").format(tool=TOOLS_BY_ID[tool].name))

            def closed():
                if not prefix_tools.needed_tool(self.prefix, action):
                    warn.hide()
                    btn.show()
            warn.connect("clicked", lambda *_: self.app.show_shipwright(
                focus=tool, parent=self.get_toplevel(), on_closed=closed))
            box.pack_start(warn, False, False, 0)
            btn.set_no_show_all(True)
        else:
            btn.show()
        box.pack_start(btn, False, False, 0)
        return box

    def _pfx_run(self, action: str):
        exe = None
        if action == prefix_tools.RUN_EXE:
            exe = self._choose_exe()
            if not exe:
                return
        try:
            prefix_tools.run(self.prefix, action, exe)
        except prefix_tools.MissingTool as e:
            self._pfx_say(self._("lo_tool_missing_shipwright").format(tool=TOOLS_BY_ID[str(e)].name))
        except OSError as e:
            logger.warning(f"Couldn't start the prefix tool: {e}")
            self._pfx_say(self._("pfx_failed").format(error=e))
        else:
            self._pfx_say(self._("pfx_started"))

    def _pfx_say(self, text: str):
        self.pfx_status.set_text(text)
        self.pfx_status.show()

    def _choose_exe(self) -> Optional[str]:
        """A card listing the programs in the game's folder and in the
        prefix's Program Files - pick one and Run; "Other file…" opens
        the desktop's own file chooser for anything elsewhere."""
        _, app = self._, self.app
        dlg, column = card_dialog(self.get_toplevel(), _("pfx_choose_title"))
        column.set_size_request(600, -1)
        chosen: Dict[str, Optional[str]] = {"path": None}

        title = Gtk.Label(label=_("pfx_run"), xalign=0)
        title.get_style_context().add_class("home-launch-title")
        sub = Gtk.Label(label=_("pfx_pick_sub"), xalign=0)
        sub.set_line_wrap(True)
        sub.set_max_width_chars(60)
        sub.get_style_context().add_class("gp-note")
        column.pack_start(title, False, False, 0)
        column.pack_start(sub, False, False, 0)

        search = Gtk.SearchEntry()
        search.set_placeholder_text(_("pfx_filter_placeholder"))
        search.set_margin_top(12)
        column.pack_start(search, False, False, 0)

        listbox = Gtk.ListBox()
        listbox.get_style_context().add_class("pfx-pick-list")
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        listbox.set_activate_on_single_click(False)
        waiting = Gtk.Label(label=_("pfx_searching"))
        waiting.get_style_context().add_class("gp-note")
        waiting.set_margin_top(40)
        waiting.set_margin_bottom(40)
        listbox.set_placeholder(waiting)
        waiting.show()
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_size_request(-1, 340)
        scroller.get_style_context().add_class("pfx-pick-scroll")
        scroller.set_margin_top(8)
        scroller.add(listbox)
        column.pack_start(scroller, True, True, 0)

        buttons = Gtk.Box(spacing=10)
        buttons.set_margin_top(16)
        other = app._dialog_button(_("pfx_other_file"), "ghost")
        cancel = app._dialog_button(_("dialog_cancel"), "ghost")
        run = app._dialog_button(_("pfx_run_btn"), "accent")
        # only there once a program is picked
        run.get_child().show_all()
        run.set_no_show_all(True)
        buttons.pack_start(other, False, False, 0)
        buttons.pack_end(run, False, False, 0)
        buttons.pack_end(cancel, False, False, 0)
        column.pack_start(buttons, False, False, 0)

        def finish(path: Optional[str]):
            chosen["path"] = path
            dlg.response(Gtk.ResponseType.OK if path else Gtk.ResponseType.CANCEL)

        def pick_other(*_a):
            native = Gtk.FileChooserNative.new(_("pfx_choose_title"), dlg, Gtk.FileChooserAction.OPEN, None, None)
            windows = Gtk.FileFilter()
            windows.set_name(_("pfx_filter"))
            for suffix in prefix_tools.PROGRAM_SUFFIXES:
                windows.add_pattern(f"*{suffix}")
                windows.add_pattern(f"*{suffix.upper()}")
            native.add_filter(windows)
            start = self.game.install_dir or self.prefix.prefix
            if start and os.path.isdir(start):
                native.set_current_folder(start)
            if native.run() == Gtk.ResponseType.ACCEPT:
                finish(native.get_filename())

        def selected_path() -> Optional[str]:
            row = listbox.get_selected_row()
            return getattr(row, "pfx_path", None) if row is not None else None

        other.connect("clicked", pick_other)
        cancel.connect("clicked", lambda *_a: finish(None))
        run.connect("clicked", lambda *_a: finish(selected_path()))
        listbox.connect("row-selected", lambda _l, row: run.set_visible(row is not None))
        listbox.connect("row-activated", lambda _l, row: finish(row.pfx_path))
        listbox.set_filter_func(lambda row: not search.get_text().strip() or
                                search.get_text().strip().lower() in row.pfx_path.lower())
        search.connect("search-changed", lambda *_a: listbox.invalidate_filter())

        def header(text: str):
            row = Gtk.ListBoxRow()
            row.pfx_path = ""
            row.set_selectable(False)
            row.set_activatable(False)
            lab = Gtk.Label(label=text.upper(), xalign=0)
            lab.get_style_context().add_class("gp-card-label")
            lab.set_margin_top(10)
            lab.set_margin_bottom(4)
            lab.set_margin_start(12)
            row.add(lab)
            return row

        def program_row(path: Path, base: Optional[str]):
            row = Gtk.ListBoxRow()
            row.pfx_path = str(path)
            line = Gtk.Box(spacing=12)
            line.get_style_context().add_class("pfx-pick-row")
            line.pack_start(_icon("app-window-symbolic", 20, app._home_accent_hex()), False, False, 0)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            name = Gtk.Label(label=path.name, xalign=0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.get_style_context().add_class("pfx-pick-name")
            try:
                where = str(path.parent.relative_to(base)) if base else str(path.parent)
            except ValueError:
                where = str(path.parent)
            folder = Gtk.Label(label="/" if where == "." else where, xalign=0)
            folder.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            folder.get_style_context().add_class("gp-note")
            texts.pack_start(name, False, False, 0)
            texts.pack_start(folder, False, False, 0)
            line.pack_start(texts, True, True, 0)
            try:
                size = Gtk.Label(label=format_bytes(path.stat().st_size))
                size.get_style_context().add_class("gp-note")
                line.pack_end(size, False, False, 0)
            except OSError:
                pass
            row.add(line)
            return row

        def filled(game_programs, prefix_programs):
            if dlg.get_window() is None:
                return False
            waiting.set_text(_("pfx_none_found"))
            for text, programs, base in ((_("pfx_group_game"), game_programs, self.game.install_dir),
                                         (_("pfx_group_prefix"), prefix_programs,
                                          os.path.join(self.prefix.prefix, "drive_c") if self.prefix.prefix else None)):
                if programs:
                    listbox.add(header(text))
                    for path in programs:
                        listbox.add(program_row(path, base))
            listbox.show_all()
            return False

        def scan():
            game_programs = prefix_tools.find_programs(self.game.install_dir) if self.game.install_dir else []
            GLib.idle_add(filled, game_programs, prefix_tools.prefix_programs(self.prefix.prefix))
        threading.Thread(target=scan, daemon=True).start()

        column.show_all()
        app._enter_dimmed_dialog()
        try:
            dlg.run()
        finally:
            app._exit_dimmed_dialog()
            dlg.destroy()
        return chosen["path"]

    def _open_folder(self, path: Optional[str]):
        if not path:
            return
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
        except GLib.Error as e:
            logger.warning(f"Couldn't open {path}: {e}")

    def _show_section(self, name: str):
        """Text mode and the other pages show the same options: leaving
        one carries its changes into the other."""
        app = self.app
        if self.current == "text" and name != "text":
            self.opts = app._lo_text_to_options(self.ui, self.opts)
            app._lo_write_widgets(self.ui, self.opts)
            self._rebuild_env_rows()
        elif name == "text" and self.current != "text":
            self.opts = app._lo_read_widgets(self.ui, self.opts)
            self.ui["text"].get_buffer().set_text(to_command_line(self.opts))
        self.current = name
        self.stack.set_visible_child_name(name)

    def _footer(self) -> Gtk.Box:
        bar = Gtk.Box(spacing=10)
        bar.get_style_context().add_class("gp-footer")
        where = Gtk.Label(label=self._("lo_stored_in").format(where=self.backend.where(self.game)), xalign=0)
        where.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        where.set_selectable(True)
        where.get_style_context().add_class("gp-note")
        bar.pack_start(where, True, True, 0)
        cancel = self.app._dialog_button(self._("dialog_cancel"), "ghost")
        cancel.connect("clicked", lambda *_: self.page.response(Gtk.ResponseType.CLOSE))
        save = self.app._dialog_button(self._("save_btn"), "accent")
        save.connect("clicked", lambda *_: self._save())
        bar.pack_end(save, False, False, 0)
        bar.pack_end(cancel, False, False, 0)
        return bar

    def _read(self) -> LaunchOptions:
        if self.current == "text":
            return self.app._lo_text_to_options(self.ui, self.opts)
        opts = self.app._lo_read_widgets(self.ui, self.opts)
        if opts.proton is None and getattr(self, "_proton_alias", None):
            opts.proton = self._proton_alias
        return opts

    def _save(self):
        app = self.app
        new_opts = self._read()
        if WRAPPER_KEY in self.unsupported and new_opts.wrappers:
            app._show_info_dialog(self._("lo_no_wrappers").format(words=" ".join(new_opts.wrappers)),
                                  message_type=Gtk.MessageType.ERROR, transient_for=self.page)
            return
        if new_opts == self.loaded:
            self.page.response(Gtk.ResponseType.CLOSE)
            return
        if app._lo_save(self.game, self.backend, new_opts, self.page):
            self._keep_suspended(new_opts)
            if self.backend.needs_rescan and hasattr(app, "reload"):
                # PlunderHub builds this launcher's command itself.
                GLib.idle_add(lambda: app.reload() and False)
            self.page.response(Gtk.ResponseType.CLOSE)



# --- the SteamGridDB picker -------------------------------------------------------------------
#
# Previews are drawn from frames made once, on worker threads, at the
# tile's size - never by GdkPixbuf on the main thread (a Gtk.Image playing
# a sized animation re-scales the full-size frame on every step, for every
# tile, and froze the window). Only tiles in view or about to be play, at
# most PICK_FPS, and their loops share a memory budget; a tile that falls
# out of it keeps its first frame.

_THUMB = {"cover": (150, 225), "hero": (330, 106), "icon": (96, 96)}
_PREVIEW_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="plunderhub-preview")
# Decoding is CPU work: two stills at a time at most, one on small
# machines.
_DECODE_SLOTS = threading.BoundedSemaphore(max(1, min(2, (os.cpu_count() or 2) // 4)))


PREVIEW_CACHE = CACHE_DIR / "sgdb-previews"
PREVIEW_CACHE_BYTES = 512 << 20


def _prune_preview_cache():
    """Down to PREVIEW_CACHE_BYTES, the least recently used going first."""
    try:
        files = [(f.stat().st_mtime, f.stat().st_size, f) for f in PREVIEW_CACHE.iterdir() if f.is_file()]
    except OSError:
        return
    total = sum(size for _t, size, _f in files)
    for _t, size, f in sorted(files, key=lambda e: e[0]):
        if total <= PREVIEW_CACHE_BYTES:
            break
        try:
            f.unlink()
            total -= size
        except OSError:
            pass


def _animation_slots() -> int:
    """How many animations are read through at once: each one's frames
    go through at full size (a 1920 px hero is ~5 MB a frame) - one on
    small machines, up to three with the cores and memory for it."""
    try:
        memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        memory = 4 << 30
    cores = os.cpu_count() or 2
    return max(1, min(3, cores // 4, int(memory // (6 << 30))))


_ANIMATION_SLOT = threading.BoundedSemaphore(_animation_slots())
# Loops are made on threads of their own: waiting for a slot there
# doesn't hold up the downloads in _PREVIEW_POOL.
_LOOP_POOL = ThreadPoolExecutor(max_workers=_animation_slots(), thread_name_prefix="plunderhub-loop")
PICK_FPS = 30
MAX_DECODED = 150        # frames read from one file at most
MAX_KEPT = 40            # frames kept of one loop (evenly thinned out)
MIN_DELAY = 1000 // PICK_FPS
MAX_ITEMS = 1000         # SteamGridDB results the picker lists at most


def _picker_budget() -> int:
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        total = 4 << 30
    return int(max(64 << 20, min(total / 32, 192 << 20)))


def _fitted(pb, w: int, h: int, crop: bool):
    pw, ph = pb.get_width(), pb.get_height()
    scale = max(w / pw, h / ph) if crop else min(w / pw, h / ph)
    nw, nh = max(1, round(pw * scale)), max(1, round(ph * scale))
    if (nw, nh) != (pw, ph):
        pb = pb.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
    if crop and (nw > w or nh > h):
        x, y = max(0, (nw - w) // 2), max(0, int((nh - h) * 0.3))
        pb = pb.new_subpixbuf(x, y, min(w, nw - x), min(h, nh - y))
    return pb


def _surface(pb, radius: float) -> cairo.ImageSurface:
    pw, ph = pb.get_width(), pb.get_height()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pw, ph)
    cr = cairo.Context(surface)
    _rounded(cr, 0, 0, pw, ph, min(radius, pw / 2, ph / 2))
    cr.clip()
    raw_set_source_pixbuf(cr, pb, 0, 0)       # pixel for pixel
    cr.paint()
    surface.flush()
    return surface


def _decode_preview(path: Path, w: int, h: int, crop: bool, radius: float, whole: bool):
    """The file's frames at the tile's size, as [(surface, delay ms)]:
    only the first one, or with whole the loop (thinned to MAX_KEPT) -
    and whether it's a still."""
    with _ANIMATION_SLOT if whole else _DECODE_SLOTS:
        animation = open_animation(path)
        if animation.is_static_image() or not whole:
            return [(_surface(_fitted(animation.get_static_image(), w, h, crop), radius), 0)], \
                animation.is_static_image()
        tv = GLib.TimeVal()
        tv.tv_sec, tv.tv_usec = 0, 0
        it = animation.get_iter(tv)
        frames, first = [], None
        for _i in range(MAX_DECODED):
            pb = it.get_pixbuf()
            # Compared in C - a Python copy of every frame's pixels would
            # hold the interpreter lock, and the window with it.
            pixels = pb.read_pixel_bytes()
            if first is None:
                first = pixels
            elif pixels.get_size() == first.get_size() and pixels.equal(first):
                break                  # round to the first frame: that's the loop
            delay = it.get_delay_time()
            delay = delay if delay > 0 else 100
            frames.append((_fitted(pb, w, h, crop), delay))
            tv.tv_usec += delay * 1000
            tv.tv_sec, tv.tv_usec = tv.tv_sec + tv.tv_usec // 1_000_000, tv.tv_usec % 1_000_000
            it.advance(tv)
    step = max(1, math.ceil(len(frames) / MAX_KEPT))
    kept = []
    for i in range(0, len(frames), step):
        group = frames[i:i + step]
        kept.append((_surface(group[0][0], radius), max(MIN_DELAY, sum(d for _pb, d in group))))
    return kept, False


class ArtPicker(Gtk.Dialog):
    """Choosing a cover, hero or icon from SteamGridDB: a tab per kind,
    filter chips, big previews (animated ones playing) and Apply.

    For something other than a game (a launcher's icon): its name to
    search for, the kinds offered, and on_apply(kind, item, preview)."""

    def __init__(self, app, game: Optional[GameItem], kind: str, parent: Gtk.Window,
                 name: Optional[str] = None, kinds: Tuple[str, ...] = ("cover", "hero", "icon"),
                 on_apply: Optional[Callable] = None):
        if not app.api_key:
            app._show_no_api_key_dialog()
            return
        super().__init__(transient_for=parent, modal=True)
        self.app, self.game, self.kind = app, game, kind
        self.query = name or game.title
        self.on_apply = on_apply
        self._ = app._
        self.set_decorated(False)
        pw, ph = parent.get_size()
        self.set_default_size(max(720, min(1100, pw - 40)), max(520, min(760, ph - 40)))
        # never smaller, whatever size a window manager remembers for it
        self.set_size_request(720, 520)
        self.get_style_context().add_class("gp-window")
        action_area = self.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()
        self.tmp = Path(tempfile.mkdtemp(prefix="plunderhub-picker-"))
        self.items: List[Dict] = []
        self.all_in = True              # every page of the current kind fetched
        self.filter = "official"
        self.sort = "popular"           # SteamGridDB's own order; "newest": the latest uploads first
        self.selected: Optional["_Tile"] = None
        self.generation = 0
        self.game_id = None
        self.budget = _picker_budget()
        self.used = 0
        self.last_tick = time.monotonic()
        _ = self._

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(spacing=14)
        head.set_border_width(20)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title = Gtk.Label(label=_("pk_title"), xalign=0)
        title.get_style_context().add_class("home-dialog-title")
        sub = Gtk.Label(label=self.query, xalign=0)
        sub.get_style_context().add_class("gp-note")
        titles.pack_start(title, False, False, 0)
        titles.pack_start(sub, False, False, 0)
        head.pack_start(titles, True, True, 0)
        tabs = Gtk.Box(spacing=2)
        tabs.get_style_context().add_class("gp-tabs")
        tabs.set_valign(Gtk.Align.CENTER)
        first = None
        for key, text in (("cover", _("pk_cover")), ("hero", _("pk_hero")), ("icon", _("pk_icon"))):
            if key not in kinds:
                continue
            rb = Gtk.RadioButton.new_with_label_from_widget(first, text)
            rb.set_mode(False)
            first = first or rb
            rb.set_active(key == kind)
            rb.connect("toggled", lambda b, k=key: b.get_active() and self._load(k))
            tabs.pack_start(rb, False, False, 0)
        if len(kinds) > 1:
            head.pack_end(tabs, False, False, 0)
        root.pack_start(head, False, False, 0)

        chips = Gtk.Box(spacing=8)
        chips.set_margin_start(20)
        chips.set_margin_bottom(10)
        chips.set_margin_end(20)
        # Official: Steam's own art; Alternate: every still one of SteamGridDB;
        # Animated: every moving one
        first = None
        self._filter_chips: Dict[str, Gtk.RadioButton] = {}
        for key, text in (("official", _("pk_official")), ("alternate", _("pk_alternate")),
                          ("animated", _("pk_animated"))):
            rb = Gtk.RadioButton.new_with_label_from_widget(first, text)
            rb.set_mode(False)
            rb.get_style_context().add_class("gp-chip")
            first = first or rb
            rb.connect("toggled", lambda b, k=key: b.get_active() and self._set_filter(k))
            chips.pack_start(rb, False, False, 0)
            self._filter_chips[key] = rb
        first = None
        sorts = []
        for key, text in (("popular", _("pk_sort_popular")), ("newest", _("pk_sort_newest"))):
            rb = Gtk.RadioButton.new_with_label_from_widget(first, text)
            rb.set_mode(False)
            rb.get_style_context().add_class("gp-chip")
            first = first or rb
            rb.connect("toggled", lambda b, k=key: b.get_active() and self._set_sort(k))
            sorts.append(rb)
        for rb in reversed(sorts):
            chips.pack_end(rb, False, False, 0)
        sort_label = Gtk.Label(label=_("pk_sort"))
        sort_label.get_style_context().add_class("gp-note")
        chips.pack_end(sort_label, False, False, 4)
        root.pack_start(chips, False, False, 0)

        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_max_children_per_line(12)
        self.flow.set_column_spacing(14)
        self.flow.set_row_spacing(14)
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_border_width(20)
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_overlay_scrolling(False)
        self.scroller.get_style_context().add_class("settings-scroll")
        self.scroller.add(self.flow)
        root.pack_start(self.scroller, True, True, 0)

        foot = Gtk.Box(spacing=10)
        foot.get_style_context().add_class("gp-footer")
        self.info = Gtk.Label(label="", xalign=0)
        self.info.get_style_context().add_class("gp-note")
        foot.pack_start(self.info, True, True, 0)
        cancel = app._dialog_button(_("dialog_cancel"), "ghost")
        cancel.connect("clicked", lambda *_: self.response(Gtk.ResponseType.CANCEL))
        self.apply_btn = app._dialog_button(_("pk_apply"), "accent")
        self.apply_btn.set_sensitive(False)
        self.apply_btn.connect("clicked", lambda *_: self._apply())
        foot.pack_end(self.apply_btn, False, False, 0)
        foot.pack_end(cancel, False, False, 0)
        root.pack_start(foot, False, False, 0)
        self.get_content_area().set_border_width(0)
        self.get_content_area().pack_start(root, True, True, 0)
        self.connect("key-press-event", lambda _w, e: e.keyval == Gdk.KEY_Escape
                     and (self.response(Gtk.ResponseType.CANCEL) or True))
        self.show_all()
        self.apply_btn.hide()           # shown once a picture is picked
        self.tick_id = GLib.timeout_add(MIN_DELAY, self._tick)
        self._load(kind)
        try:
            self.run()
        finally:
            self.generation += 1
            GLib.source_remove(self.tick_id)
            self.destroy()
            shutil.rmtree(self.tmp, ignore_errors=True)
            threading.Thread(target=_prune_preview_cache, name="plunderhub-prune-previews", daemon=True).start()

    def _tiles(self) -> List["_Tile"]:
        return [child.get_child() for child in self.flow.get_children()]

    def _view_range(self):
        adj = self.scroller.get_vadjustment()
        top, page = adj.get_value(), adj.get_page_size()
        return top, top + page, page

    def _tick(self) -> bool:
        """One clock for every tile: the loading sweep, the playing loops,
        and which loops get made next."""
        now = time.monotonic()
        dt = min(250.0, (now - self.last_tick) * 1000)
        self.last_tick = now
        if self.get_window() is None:
            return True
        # out of focus the previews hold still, but still load
        active = self.is_active()
        top, bottom, page = self._view_range()
        wanted = []
        # Only the tiles in or near the view (up to 1000 results): found by
        # bisecting the flow's rows, which lie top to bottom in child order.
        children = self.flow.get_children()
        base = self.flow.get_allocation().y
        ys = [c.get_allocation().y - base for c in children]
        first = max(0, bisect.bisect_left(ys, top - page * 0.5 - 600) - 1)
        last = bisect.bisect_right(ys, bottom + page * 0.5)
        near_now = set()
        for child in children[first:last]:
            tile = child.get_child()
            if tile is None:
                continue
            ty, th = child.get_allocation().y - base, tile.get_allocated_height()
            near_now.add(tile)
            on_screen = ty + th > top and ty < bottom
            near = ty + th > top - page * 0.5 and ty < bottom + page * 0.5
            # only what's in view or about to be gets downloaded - an
            # animated one can be tens of MB
            if near and not tile.started:
                tile.start()
            if not active:
                continue
            tile.on_screen = on_screen
            if on_screen:
                tile.last_seen = now
                if tile.loading:
                    tile.queue_draw()
                elif tile.step(dt):
                    tile.queue_draw()
            if near and tile.wants_loop():
                wanted.append((not on_screen, ty, tile))
        # the ones scrolled far away aren't on screen any more
        for tile in getattr(self, "_near_tiles", set()) - near_now:
            tile.on_screen = False
        self._near_tiles = near_now
        for _far, _y, tile in sorted(wanted, key=lambda t: t[:2])[:2]:
            tile.make_loop()
        return True

    def make_room(self, need: int, keep: "_Tile") -> bool:
        """Frees the loops of the tiles out of view longest."""
        if self.used + need <= self.budget:
            return True
        for tile in sorted((t for t in self._tiles() if t.loop_bytes and not t.on_screen and t is not keep),
                           key=lambda t: t.last_seen):
            tile.drop_loop()
            if self.used + need <= self.budget:
                return True
        return False

    def _load(self, kind: str):
        self.kind = kind
        self.selected = None
        self.apply_btn.hide()
        self.info.set_text(self._("pk_loading"))
        self.generation += 1
        generation = self.generation
        # every kind starts on Official (left for the next tab with
        # something in it when Steam has none)
        self.items = []
        if self.filter not in ("official", "animated"):
            self._filter_chips["official"].set_active(True)
        self._clear()
        for _i in range(12):
            self.flow.add(_Tile(self, None))
        self.flow.show_all()

        def work():
            # Every page (a popular game has hundreds), the first shown as
            # soon as it's in, the rest added to it as they come. Steam's
            # own art (a game's, not a launcher's) comes with the first.
            app, page, count, official = self.app, 0, 0, []
            try:
                if self.game_id is None:
                    self.game_id = app._steamgrid_search(self.query)
                if self.game is not None:
                    try:
                        official = app._steam_official_art(kind, self.game, self.game_id)
                    except Exception as e:
                        logger.debug(f"No Steam art for {self.query!r}: {e}")
                while self.game_id is not None and generation == self.generation:
                    items, total = app._steamgrid_page(kind, self.game_id, page)
                    count += len(items)
                    done = not items or count >= total or count >= MAX_ITEMS
                    GLib.idle_add(self._got if page == 0 else self._got_more, generation,
                                  official + items if page == 0 else items, done)
                    if done:
                        return
                    page += 1
            except Exception as e:
                logger.warning(f"SteamGridDB lookup failed for {self.query!r}: {e}")
            # nothing more will come: the footer stops saying Loading
            GLib.idle_add(self._got if page == 0 else self._got_more, generation,
                          official if page == 0 else [], True)
        threading.Thread(target=work, daemon=True).start()

    def _clear(self):
        for child in self.flow.get_children():
            tile = child.get_child()
            if tile is not None:
                tile.dropped = True     # its download / decode stop (a filter change)
                tile.drop_loop()
            child.destroy()

    def _got(self, generation: int, items: List[Dict], done: bool = True) -> bool:
        if generation != self.generation:
            return False
        self.items = items
        self.all_in = done
        if self.filter == "official" and not any(self._matches(i) for i in items):
            # not on Steam (or no answer): its first tab with something in it
            self._filter_chips["alternate"].set_active(True)     # refills
        else:
            self._refill()
        return False

    def _got_more(self, generation: int, items: List[Dict], done: bool) -> bool:
        if generation != self.generation:
            return False
        self.items += items
        self.all_in = done
        added = [i for i in items if self._matches(i)]
        if self.sort == "newest":
            # in place among the ones shown: the newest stay on top
            shown = [i for i in self._ordered(self.items) if self._matches(i)]
            for item in sorted(added, key=lambda i: shown.index(i)):
                self.flow.insert(_Tile(self, item), shown.index(item))
        else:
            for item in added:
                self.flow.add(_Tile(self, item))
        self.flow.show_all()
        if self.selected is None:
            self._show_count()
        return False

    def _matches(self, item: Dict) -> bool:
        if self.filter == "animated":
            return bool(item.get("animated"))
        official = item.get("style") == "official"
        if self.filter == "official":
            return official
        return not official and not item.get("animated")

    def _ordered(self, items: List[Dict]) -> List[Dict]:
        if self.sort == "newest":
            return sorted(items, key=lambda i: -(i.get("id") or 0))
        return list(items)

    def _set_sort(self, key: str):
        self.sort = key
        if self.items:
            self._refill()

    def _show_count(self):
        n = sum(1 for i in self.items if self._matches(i))
        text = self._("pk_count").format(n=n) if n else self._("pk_none")
        if not self.all_in:
            text += " · " + self._("pk_loading")
        self.info.set_text(text)

    def _set_filter(self, key: str):
        self.filter = key
        if self.items:
            self._refill()

    def _refill(self):
        self._clear()
        self.selected = None
        self.apply_btn.hide()
        for item in self._ordered(self.items):
            if self._matches(item):
                self.flow.add(_Tile(self, item))
        self.flow.show_all()
        self.scroller.get_vadjustment().set_value(0)
        self._show_count()

    def choose(self, tile: "_Tile"):
        if self.selected is not None:
            self.selected.selected = False
            self.selected.queue_draw()
        self.selected = tile
        tile.selected = True
        tile.queue_draw()
        item = tile.item
        parts = [f"{item.get('width')}×{item.get('height')}"]
        if item.get("style"):
            parts.append(item["style"])
        if item.get("author"):
            parts.append(self._("pk_by").format(author=item["author"]))
        self.info.set_text("  ·  ".join(parts))
        self.apply_btn.set_sensitive(tile.path is not None)
        self.apply_btn.show()

    def _apply(self):
        tile = self.selected
        if tile is not None and tile.path is not None:
            if self.on_apply is not None:
                self.on_apply(self.kind, tile.item, tile.path)
            else:
                self.app._apply_sgdb_art(self.kind, self.game, tile.item, tile.path)
        self.response(Gtk.ResponseType.OK)


class _Tile(Gtk.DrawingArea):
    """One preview, painted by hand from its ready frames."""

    def __init__(self, picker: ArtPicker, item: Optional[Dict]):
        super().__init__()
        self.picker, self.item = picker, item
        self.kind = picker.kind
        self.w, self.h = _THUMB[self.kind]
        self.radius = 14 if self.kind == "icon" else 10
        self.set_size_request(self.w, self.h)
        self.loading = True
        self.path: Optional[Path] = None
        self.selected = False
        self.hover = False
        self.on_screen = False
        self.last_seen = 0.0
        self.frames: List[tuple] = []     # [(surface, delay ms)]; one until the loop is made
        self.starts: List[int] = []
        self.loop_ms = 0
        self.loop_bytes = 0
        self.clock_ms = 0.0
        self.current = 0
        self.still = not (item or {}).get("animated")
        self.loop_busy = False
        self.loop_failed = False
        self.loop_reserved = 0
        self.dropped = False
        self.generation = picker.generation
        self.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
                        | Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("draw", self._draw)
        self.connect("enter-notify-event", lambda *_: (setattr(self, "hover", True), self.queue_draw()))
        self.connect("leave-notify-event", lambda *_: (setattr(self, "hover", False), self.queue_draw()))
        self.started = item is None     # the download waits until it's near the view
        if item is not None:
            self.connect("button-press-event", lambda *_: picker.choose(self))
            self.set_tooltip_text(item.get("style") or None)

    def start(self):
        self.started = True
        _PREVIEW_POOL.submit(self._fetch, self.generation)

    def _alive(self, generation: int) -> bool:
        return generation == self.picker.generation and self.get_window() is not None

    # --- worker side ------------------------------------------------------------

    def _fetch(self, generation: int):
        item, picker = self.item, self.picker
        if generation != picker.generation or self.dropped:
            return
        # SteamGridDB previews an animation as a video, which GdkPixbuf can't
        # open - the file itself, then.
        thumb = item.get("thumb") or ""
        url = item["url"] if item.get("animated") or thumb.endswith((".webm", ".mp4")) else (thumb or item["url"])
        ext = Path(urllib.parse.urlparse(url).path).suffix or ".png"
        # kept between pickers (an animated hero is tens of MB): opened
        # again, it's there at once
        target = PREVIEW_CACHE / f"{hashlib.sha1(url.encode()).hexdigest()}{ext}"
        frames, still = None, True
        try:
            if target.is_file() and target.stat().st_size > 0:
                os.utime(target)            # recently used - pruned last
            else:
                PREVIEW_CACHE.mkdir(parents=True, exist_ok=True)
                part = target.with_name(target.name + f".{threading.get_ident()}.part")
                req = urllib.request.Request(url, headers=HEADERS)
                try:
                    with urllib.request.urlopen(req, timeout=20) as resp, open(part, "wb") as out:
                        while True:
                            chunk = resp.read(1 << 16)
                            if not chunk:
                                break
                            if self.dropped:
                                return      # its tile is gone: the pool's worker is free again
                            out.write(chunk)
                    part.replace(target)    # only a whole file ever gets the name
                finally:
                    part.unlink(missing_ok=True)
            if generation != picker.generation or self.dropped:
                return
            # Its first frame at once (a few hundredths of a second) - the
            # loop, a few seconds for a big animation, comes after, for
            # what's in view (see wants_loop).
            frames, still = _decode_preview(target, self.w, self.h, self.kind == "hero", self.radius, False)
        except Exception as e:
            logger.debug(f"Couldn't load a SteamGridDB preview ({url}): {e}")
            target = None
        GLib.idle_add(self._loaded, generation, target, frames, still)

    def _make_loop_job(self, generation: int):
        frames = None
        try:
            if generation == self.picker.generation and not self.dropped:
                frames, _still = _decode_preview(self.path, self.w, self.h, self.kind == "hero", self.radius, True)
        except Exception as e:
            logger.debug(f"Couldn't decode the animated preview {self.path}: {e}")
        GLib.idle_add(self._loop_made, generation, frames)

    # --- main thread ---------------------------------------------------------------

    def _loaded(self, generation: int, path: Optional[Path], frames, still: bool) -> bool:
        if not self._alive(generation):
            return False
        self.loading = False
        self.path = path
        self.still = still or not frames
        frames = frames or []
        if len(frames) > 1:
            self.frames = frames[:1]
            if self.picker.make_room(sum(s.get_stride() * s.get_height() for s, _d in frames), self):
                self._loop_made(generation, frames)
        else:
            self.frames = frames
        self.queue_draw()
        if self.picker.selected is self:
            self.picker.apply_btn.set_sensitive(path is not None)
        return False

    def wants_loop(self) -> bool:
        return (not self.loading and not self.still and not self.loop_busy and not self.loop_failed
                and len(self.frames) <= 1 and self.path is not None)

    def make_loop(self):
        need = self.w * self.h * 4 * MAX_KEPT
        if not self.picker.make_room(need, self):
            return                     # no room now: stays on its first frame
        # counted from now: loops still being made count against the budget
        self.loop_reserved = need
        self.picker.used += need
        self.loop_busy = True
        _LOOP_POOL.submit(self._make_loop_job, self.generation)

    def _loop_made(self, generation: int, frames) -> bool:
        self.loop_busy = False
        self.picker.used -= self.loop_reserved
        self.loop_reserved = 0
        if not self._alive(generation):
            return False
        if not frames or len(frames) < 2:
            self.loop_failed = True    # a still after all, or broken: keep the first frame
            return False
        self.frames = frames
        self.starts, t = [], 0
        for _surface, delay in frames:
            self.starts.append(t)
            t += delay
        self.loop_ms = t
        self.loop_bytes = sum(s.get_stride() * s.get_height() for s, _d in frames)
        self.picker.used += self.loop_bytes
        self.clock_ms = 0.0
        self.current = 0
        return False

    def drop_loop(self):
        """Back to the first frame, its loop's memory freed."""
        if self.loop_bytes:
            self.picker.used -= self.loop_bytes
            self.frames = self.frames[:1]
            self.loop_bytes = self.loop_ms = 0
            self.current = 0

    def step(self, dt: float) -> bool:
        """Moves the loop on by dt ms - True when another frame shows."""
        if self.loop_ms <= 0:
            return False
        self.clock_ms = (self.clock_ms + dt) % self.loop_ms
        index = bisect.bisect_right(self.starts, self.clock_ms) - 1
        if index != self.current:
            self.current = index
            return True
        return False

    def _draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        tw, th = min(w, self.w), min(h, self.h)
        x, y = (w - tw) / 2, (h - th) / 2
        r = self.radius
        accent = _accent_rgb(widget)
        if self.loading:
            # placeholder, a soft light sweeping across it while it loads
            _rounded(cr, x, y, tw, th, r)
            cr.set_source_rgba(1, 1, 1, 0.05)
            cr.fill_preserve()
            sweep = (time.monotonic() * 400) % (tw * 2.5) - tw
            g = cairo.LinearGradient(x + sweep, y, x + sweep + tw * 0.6, y + th)
            g.add_color_stop_rgba(0, 1, 1, 1, 0)
            g.add_color_stop_rgba(0.5, 1, 1, 1, 0.07)
            g.add_color_stop_rgba(1, 1, 1, 1, 0)
            cr.set_source(g)
            cr.fill()
        elif self.frames:
            surface = self.frames[min(self.current, len(self.frames) - 1)][0]
            sw, sh = surface.get_width(), surface.get_height()
            x, y, tw, th = (w - sw) / 2, (h - sh) / 2, sw, sh
            cr.set_source_surface(surface, x, y)
            cr.paint()
        if self.hover and not self.selected and not self.loading:
            _rounded(cr, x, y, tw, th, r)
            cr.set_source_rgba(1, 1, 1, 0.08)
            cr.fill()
        if self.item is not None and self.item.get("animated"):
            label = self.picker._("pk_badge_animated")
            layout = PangoCairo.create_layout(cr)
            desc = widget.get_pango_context().get_font_description().copy()
            desc.set_size(int(7 * Pango.SCALE))
            desc.set_weight(Pango.Weight.BOLD)
            layout.set_font_description(desc)
            layout.set_text(label, -1)
            lw, lh = layout.get_pixel_size()
            _rounded(cr, x + 8, y + 8, lw + 16, lh + 8, (lh + 8) / 2)
            cr.set_source_rgba(0, 0, 0, 0.65)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            cr.move_to(x + 16, y + 12)
            PangoCairo.show_layout(cr, layout)
        if self.selected:
            cr.set_line_width(4)
            cr.set_source_rgb(*accent)
            _rounded(cr, x + 2, y + 2, tw - 4, th - 4, r)
            cr.stroke()
            cr.arc(x + tw - 18, y + 18, 12, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(*_on_accent_rgb(accent))
            cr.set_line_width(2.4)
            cr.move_to(x + tw - 24, y + 18)
            cr.line_to(x + tw - 19.5, y + 22.5)
            cr.line_to(x + tw - 12, y + 13.5)
            cr.stroke()
        return False
