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

"""Shipwright: launchers and tools as tiles, by category - picked with a
click and installed (or removed) together, under one password - plus
the Ship inspection, the system settings games trip over, with fixes.

The window: the shelves on the left, the page on the right - the
overview's one list of every shelf (the sidebar jumps along it), the
inspection or the updates - and a bar sliding up from the bottom with
what's picked and the Install / Remove buttons. The work itself is
ui/launcher_tasks'."""

import math
import subprocess

import cairo
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set

from plunderhub.core import flatpak_layers, inspection
from plunderhub.core.tools import GROUPS, TOOLS, full_upgrade_only, pending_updates, snapshots_on
from plunderhub.core.persistence import save_settings
from plunderhub.core.utils import detect_linux_distro_family, immutable_system
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango, logical_size
from plunderhub.icons import get_icon_pixbuf
from plunderhub.launchers.registry import LAUNCHER_REGISTRY
from plunderhub.logging_setup import logger
from plunderhub.perf_guard import allow_hover_relayout
from plunderhub.ui import keynav, launcher_tasks
from plunderhub.ui.home_mixin import _accent_rgb, _on_accent_rgb, _pixbuf_area, _windowless_area

AMBER = (0.91, 0.64, 0.24)
STATUS_ICONS = {inspection.OK: "emblem-ok-symbolic", inspection.WARN: "triangle-alert-symbolic",
                inspection.INFO: "dialog-information-symbolic"}
# The page order, as the sidebar lists it. "updates" shows only when
# something has one.
CATEGORIES = ("overview", "inspection", "updates", "launchers") + GROUPS
CATEGORY_ICONS = {"overview": "layout-dashboard-symbolic", "inspection": "stethoscope-symbolic",
                  "updates": "view-refresh-symbolic",
                  "launchers": "applications-games-symbolic", "performance": "zap-symbolic",
                  "graphics": "sparkles-symbolic", "proton": "rocket-symbolic", "flatpak": "package-symbolic",
                  "controllers": "input-gaming-symbolic", "apps": "cpu-symbolic",
                  "bilge": "user-trash-symbolic"}
TILE_CATEGORIES = [c for c in CATEGORIES if c not in ("overview", "inspection", "updates")]
# The Flatpak launchers' Vulkan layer of a tool: shown and installed
# along with the tool, on its tile.
FLATPAK_PARTS = {"mangohud": "flatpak:mangohud", "gamescope": "flatpak:gamescope",
                 "vkbasalt": "flatpak:vkbasalt", "obs": "flatpak:obsvkcapture"}
TOOL_OF_PART = {part: tool for tool, part in FLATPAK_PARTS.items()}
# What "Recommended" picks: what most games and launchers benefit from.
# ProtonPlus stays on its shelf as an alternative: the Proton page does its job.
RECOMMENDED = ("gamemode", "mangohud", "gamescope", "winedeps")
# Makes the Proton page (ui/proton_page.py) for a Shipwright - the design
# demo (PlunderHubDemos/proton_demo.py) sets one with made-up data.
PROTON_PAGE: Optional[Callable] = None


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb)


@dataclass
class Item:
    key: str            # tool id, launcher name, or "flatpak:<layer id>"
    name: str
    category: str
    desc: str
    used: str = ""
    icon: str = ""      # a bundled symbolic icon; launchers show their logo instead
    launcher: bool = False
    gui: bool = False
    removable: bool = True
    layer: object = None
    # the name PlunderHub knows it by (TOOLS_BY_NAME, the launcher
    # registry): name is what's shown, translated for a descriptive one
    ident: str = ""

    def __post_init__(self):
        self.ident = self.ident or self.name


def _icon_tile(icon: str, size: int = 48, colour: Optional[tuple] = None) -> Gtk.DrawingArea:
    """A rounded square in the accent tint (or colour), the icon on it."""
    tile = _windowless_area()      # in a tile's button: clicks go to the button
    tile.set_size_request(size, size)
    tile.set_halign(Gtk.Align.START)
    tile.set_valign(Gtk.Align.START)

    def draw(widget, cr):
        rgb = colour or _accent_rgb(widget)
        r = size * 0.27
        cr.new_sub_path()
        cr.arc(size - r, r, r, -math.pi / 2, 0)
        cr.arc(size - r, size - r, r, 0, math.pi / 2)
        cr.arc(r, size - r, r, math.pi / 2, math.pi)
        cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.set_source_rgba(*rgb, 0.16)
        cr.fill()
        px = round(size * 0.5)
        pb = get_icon_pixbuf(icon, px, _hex(rgb))
        if pb is not None:
            Gdk.cairo_set_source_pixbuf(cr, pb, (size - px) / 2, (size - px) / 2)
            cr.paint()
        return False
    tile.connect("draw", draw)
    return tile


def _bare_icon(icon: str, size: int, colour: Optional[tuple] = None) -> Gtk.DrawingArea:
    """The icon on its own, in the accent (or colour) - no plate under it."""
    area = _windowless_area()
    area.set_size_request(size, size)
    area.set_halign(Gtk.Align.CENTER)
    area.set_valign(Gtk.Align.CENTER)

    def draw(widget, cr):
        pb = get_icon_pixbuf(icon, size, _hex(colour or _accent_rgb(widget)))
        if pb is not None:
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.paint()
        return False
    area.connect("draw", draw)
    return area


def _item_icon(item: Item, size: int) -> Gtk.Widget:
    if item.launcher:
        pb = launcher_tasks.launcher_pixbuf(item.ident, size)
        if pb is not None:
            image = _pixbuf_area(pb)
            image.set_halign(Gtk.Align.START)
            return image
        return _icon_tile("applications-games-symbolic", size)
    return _icon_tile(item.icon, size)


class NavIcon(Gtk.DrawingArea):
    """A sidebar row's icon - with a dot in the accent at its corner for
    a shelf with updates, like a notification's. Windowless (see
    _windowless_area)."""

    def __init__(self, pb: Optional[GdkPixbuf.Pixbuf]):
        super().__init__()
        self.set_has_window(False)
        self.pb = pb
        self.dot = False
        self.set_size_request(22, 22)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def set_dot(self, on: bool):
        if on != self.dot:
            self.dot = on
            self.queue_draw()

    def _draw(self, widget, cr) -> bool:
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        if self.pb is not None:
            Gdk.cairo_set_source_pixbuf(cr, self.pb, (w - logical_size(self.pb)[0]) / 2, (h - logical_size(self.pb)[1]) / 2)
            cr.paint()
        if self.dot:
            x, y = w / 2 + 7, h / 2 - 7
            cr.arc(x, y, 4.6, 0, 2 * math.pi)
            cr.set_source_rgba(0.04, 0.09, 0.08, 1)
            cr.fill()
            cr.arc(x, y, 3.2, 0, 2 * math.pi)
            cr.set_source_rgba(*_accent_rgb(widget), 1)
            cr.fill()
        return False


class Tick(Gtk.Button):
    """A tile's tick box in its corner: picks it for doing several
    together. Drawn (the icon theme's check differs from desktop to
    desktop), the box itself windowless so a click lands on the button."""
    SIZE = 22

    def __init__(self, on_toggled: Callable[[bool], None]):
        super().__init__()
        self.active = False
        self.danger = False
        self._on_toggled = on_toggled
        self.get_style_context().add_class("sw-tick")
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_focus_on_click(False)
        # the tile is what the keyboard stands on (Space picks it)
        self.set_can_focus(False)
        box = _windowless_area()
        box.set_size_request(self.SIZE, self.SIZE)
        box.connect("draw", self._draw)
        self.box = box
        self.add(box)
        self.connect("clicked", lambda *_: self.set_active(not self.active, notify=True))

    def set_active(self, active: bool, notify: bool = False):
        if active != self.active:
            self.active = active
            self.box.queue_draw()
        if notify:
            self._on_toggled(active)

    def _draw(self, widget, cr) -> bool:
        n = self.SIZE
        accent = (0.90, 0.33, 0.29) if self.danger else _accent_rgb(widget)
        r = 6
        cr.new_sub_path()
        cr.arc(n - r - 1, r + 1, r, -math.pi / 2, 0)
        cr.arc(n - r - 1, n - r - 1, r, 0, math.pi / 2)
        cr.arc(r + 1, n - r - 1, r, math.pi / 2, math.pi)
        cr.arc(r + 1, r + 1, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        if self.active:
            cr.set_source_rgba(*accent, 1)
            cr.fill()
            cr.set_source_rgba(*_on_accent_rgb(accent), 1)
            cr.set_line_width(2.4)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.move_to(n * 0.27, n * 0.52)
            cr.line_to(n * 0.44, n * 0.68)
            cr.line_to(n * 0.74, n * 0.34)
            cr.stroke()
        else:
            ctx = widget.get_style_context()
            fg = ctx.get_color(ctx.get_state())
            cr.set_source_rgba(0, 0, 0, 0.18)
            cr.fill_preserve()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
            cr.set_line_width(1.6)
            cr.stroke()
        return False


class Tile(Gtk.Overlay):
    """One launcher or tool: its icon, name and state. A click opens its
    card (what it is, and Install / Remove / Open); the tick in its corner
    picks it, for installing or removing several together."""

    def __init__(self, sw: "Shipwright", item: Item):
        super().__init__()
        self.sw, self.item = sw, item
        btn = Gtk.Button()
        btn.get_style_context().add_class("sw-tile")
        btn.set_size_request(240, -1)
        # a click doesn't leave a focus ring behind (keyboard focus still shows)
        btn.set_focus_on_click(False)
        row = Gtk.Box(spacing=14)
        if item.launcher:
            icon = _item_icon(item, 44)
            icon.set_valign(Gtk.Align.CENTER)
        else:
            icon = _bare_icon(item.icon, 32)
            icon.set_size_request(44, 44)
        row.pack_start(icon, False, False, 0)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_valign(Gtk.Align.CENTER)
        name = Gtk.Label(label=item.name, xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_max_width_chars(18)
        name.set_tooltip_text(item.name)
        name.get_style_context().add_class("sw-tile-name")
        # room for the tick in the corner
        name.set_margin_end(26)
        box.pack_start(name, False, False, 0)
        self.name = name
        state = Gtk.Box(spacing=8)
        self.status = Gtk.Label(xalign=0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status.set_max_width_chars(22)
        self.status.get_style_context().add_class("sw-tile-status")
        state.pack_start(self.status, False, False, 0)
        self.update = Gtk.Label(label=sw._("sw_update_available"))
        self.update.get_style_context().add_class("sw-badge")
        self.update.set_valign(Gtk.Align.CENTER)
        self.update.set_no_show_all(True)
        state.pack_start(self.update, False, False, 0)
        box.pack_start(state, False, False, 0)
        row.pack_start(box, True, True, 0)
        btn.add(row)
        btn.connect("clicked", lambda *_: sw.show_card(item.key))
        btn.sw_cat = item.category      # the sidebar marks this shelf while it's stood on
        self.button = btn
        self.add(btn)

        self.tick = Tick(lambda on: sw.set_selected(item.key, on))
        self.tick.set_tooltip_text(sw._("sw_pick_tooltip"))
        self.tick.set_halign(Gtk.Align.END)
        self.tick.set_valign(Gtk.Align.START)
        self.tick.set_margin_top(8)
        self.tick.set_margin_end(8)
        self.add_overlay(self.tick)

    def toggle(self):
        """Space on the tile, or the controller's pick button."""
        self.tick.set_active(not self.tick.active, notify=True)

    def set_active(self, active: bool):
        self.tick.set_active(active)
        self._mark()

    def _mark(self):
        installed = self.sw._installed(self.item.key)
        self.tick.danger = installed and self.item.key not in self.sw.updates
        ctx = self.button.get_style_context()
        picked = self.tick.active
        (ctx.add_class if picked and not self.tick.danger else ctx.remove_class)("sw-picked")
        (ctx.add_class if picked and self.tick.danger else ctx.remove_class)("sw-picked-remove")
        self.tick.box.queue_draw()

    def refresh(self):
        sw, key = self.sw, self.item.key
        text, on = sw.state_line(key)
        self.status.set_text(text)
        ctx = self.status.get_style_context()
        ctx.remove_class("settings-status-off" if on else "settings-status-on")
        ctx.add_class("settings-status-on" if on else "settings-status-off")
        installed = sw._installed(key)
        bctx = self.button.get_style_context()
        (bctx.add_class if installed and key not in sw.updates else bctx.remove_class)("sw-installed")
        self.update.set_visible(key in sw.updates)
        # nothing to pick while its state isn't known yet
        self.tick.set_sensitive(key in sw.states and not sw.states[key].get("unavailable"))
        self.set_active(key in sw.selected)


class ToolCard:
    """A launcher's or tool's own card, opened from its tile: its icon
    popping in, what it is and where it's used, and what can be done
    with it - Install (from the system or Flathub, where both have it),
    Remove, Update, Open."""

    def __init__(self, sw: "Shipwright", key: str, keyboard: bool = False):
        self.sw, self.key = sw, key
        item = self.item = sw.items[key]
        _ = sw._
        dlg = Gtk.Dialog(transient_for=sw.dialog, modal=True)
        dlg.set_decorated(False)
        dlg.set_resizable(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.get_style_context().add_class("home-launch-dialog")
        dlg.get_style_context().add_class("sw-card")
        action_area = dlg.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()
        area = dlg.get_content_area()
        area.set_border_width(0)
        self.dialog = dlg

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        column.set_border_width(28)
        column.set_size_request(440, -1)
        title = Gtk.Label(label=item.name)
        title.set_line_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        title.get_style_context().add_class("home-dialog-title")
        state = Gtk.Box(spacing=8)
        state.set_halign(Gtk.Align.CENTER)
        self.status = Gtk.Label()
        self.status.get_style_context().add_class("sw-tile-status")
        state.pack_start(self.status, False, False, 0)
        self.update = Gtk.Label(label=_("sw_update_available"))
        self.update.get_style_context().add_class("sw-badge")
        self.update.set_no_show_all(True)
        state.pack_start(self.update, False, False, 0)
        desc = Gtk.Label(label=item.desc)
        desc.set_line_wrap(True)
        desc.set_max_width_chars(46)
        desc.set_justify(Gtk.Justification.CENTER)
        desc.get_style_context().add_class("sw-card-desc")
        desc.set_margin_top(6)
        parts = [title, state, desc]
        if item.used:
            used = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            used.set_margin_top(6)
            head = Gtk.Label(label=_("sw_used_in").upper())
            head.get_style_context().add_class("sw-card-caption")
            where = Gtk.Label(label=item.used)
            where.set_line_wrap(True)
            where.set_justify(Gtk.Justification.CENTER)
            used.pack_start(head, False, False, 0)
            used.pack_start(where, False, False, 0)
            parts.append(used)
        # the Flatpak launchers' copy of a tool, while there are some
        self.part = Gtk.Label()
        self.part.set_line_wrap(True)
        self.part.set_justify(Gtk.Justification.CENTER)
        self.part.set_max_width_chars(46)
        self.part.get_style_context().add_class("sw-tile-status")
        self.part.set_no_show_all(True)
        parts.append(self.part)
        # where an install comes from, where Flathub has it too
        self.sources = Gtk.Box(spacing=10)
        self.sources.set_halign(Gtk.Align.CENTER)
        self.sources.set_margin_top(8)
        self.sources.set_no_show_all(True)
        caption = Gtk.Label(label=_("sw_install_from"))
        caption.get_style_context().add_class("sw-card-caption")
        self.sources.pack_start(caption, False, False, 0)
        pair = Gtk.Box()
        pair.get_style_context().add_class("linked")
        self.source_btns: Dict[str, Gtk.ToggleButton] = {}
        for source in ("native", "flatpak"):
            b = Gtk.ToggleButton(label=_(f"sw_source_{source}"))
            b.get_style_context().add_class("sw-source")
            b.set_focus_on_click(False)
            b.connect("toggled", self._source_toggled, source)
            pair.pack_start(b, False, False, 0)
            self.source_btns[source] = b
        self.sources.pack_start(pair, False, False, 0)
        for widget in self.sources.get_children():
            widget.show_all()
        parts.append(self.sources)

        buttons = Gtk.Box(spacing=10)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_margin_top(16)
        close = sw.app._dialog_button(_("close_btn"), "ghost", "back", "Esc")
        close.connect("clicked", lambda *_: self.close())
        self.open_btn = sw.app._dialog_button(_("sw_open"), "ghost")
        self.open_btn.connect("clicked", lambda *_: (sw._open(item), self.close()))
        self.update_btn = sw.app._dialog_button(_("sw_update"), "accent")
        self.update_btn.connect("clicked", lambda *_: self._do(lambda: sw._run_updates([key])))
        self.remove_btn = sw.app._dialog_button(_("task_remove_btn"), "danger")
        allow_hover_relayout(self.remove_btn, "turns red with light text under the pointer")
        self.remove_btn.connect("clicked", lambda *_: self._do(lambda: sw.run_keys([key], "remove")))
        self.install_btn = sw.app._dialog_button(_("install_btn"), "accent", "activate", "Enter")
        self.install_btn.connect("clicked", lambda *_: self._do(lambda: sw.run_keys([key], "install")))
        for b in (close, self.open_btn, self.update_btn, self.remove_btn, self.install_btn):
            b.set_focus_on_click(False)
            buttons.pack_start(b, False, False, 0)
        for b in (self.open_btn, self.update_btn, self.remove_btn, self.install_btn):
            b.get_child().show_all()
            b.set_no_show_all(True)
        parts.append(buttons)

        art = sw.app._popping_logo(72, glow=True, then=parts, glow_room=(150, 70), picture=self._art)
        art.set_halign(Gtk.Align.CENTER)
        column.pack_start(art, False, False, 0)
        for widget in parts:
            column.pack_start(widget, False, False, 0)
        area.pack_start(column, True, True, 0)
        dlg.connect("key-press-event", self._on_key)
        dlg.connect("delete-event", lambda *_: (self.close(), True)[1])
        dlg.plunderhub_nav = self._nav
        keynav.track(dlg)
        if key in sw.states and sw._offers_choice(key) and key not in sw.native_plans:
            sw._ask_native(key)
        self.refresh()
        area.show_all()
        dlg.show()
        dlg.set_focus(None)
        if keyboard:
            main = next((b for b in (self.install_btn, self.update_btn, self.remove_btn, self.open_btn)
                         if b.get_visible()), close)
            GLib.idle_add(lambda: (main.grab_focus(), keynav.on(main)) and False)

    def _art(self, px: int) -> Optional[GdkPixbuf.Pixbuf]:
        if self.item.launcher:
            pb = launcher_tasks.launcher_pixbuf(self.item.ident, px)
            if pb is not None:
                return pb
            return get_icon_pixbuf("applications-games-symbolic", px, _hex(_accent_rgb(self.dialog)))
        return get_icon_pixbuf(self.item.icon, px, _hex(_accent_rgb(self.dialog)))

    def refresh(self):
        sw, key, _ = self.sw, self.key, self.sw._
        text, on = sw.state_line(key)
        self.status.set_text(text)
        ctx = self.status.get_style_context()
        ctx.remove_class("settings-status-off" if on else "settings-status-on")
        ctx.add_class("settings-status-on" if on else "settings-status-off")
        known = key in sw.states
        here, installed = sw._here(key), sw._installed(key)
        self.update.set_visible(key in sw.updates)
        self.open_btn.set_visible(here and self.item.gui)
        self.update_btn.set_visible(key in sw.updates and installed)
        self.remove_btn.set_visible(known and installed and self.item.removable)
        self.install_btn.set_visible(known and not installed and not sw.states[key].get("unavailable"))
        for b in (self.update_btn, self.remove_btn, self.install_btn):
            b.set_sensitive(not sw.busy)
        part = sw._part(key)
        self.part.set_visible(part is not None)
        if part is not None:
            pst = sw.states[part]
            mark = "◐ " + _("sw_partly") if pst["partial"] else (
                "● " + _("source_installed") if sw._here(part) else "○ " + _("source_not_installed"))
            n = len(sw.flatpak_launchers)
            where = (_("sw_flatpak_part").format(launchers=sw.flatpak_names) if n == 1
                     else _("sw_flatpak_part_n").format(n=n))
            self.part.set_text(f"{mark} · {where}")
            self.part.set_tooltip_text(_("sw_flatpak_note" if n == 1 else "sw_flatpak_note_n")
                                       .format(launchers=sw.flatpak_names))
        choice = known and sw._offers_choice(key)
        self.sources.set_visible(choice)
        if choice:
            native = sw.native_plans.get(key)
            usable = isinstance(native, launcher_tasks.Plan)
            nb, fb = self.source_btns["native"], self.source_btns["flatpak"]
            nb.set_label(_("sw_source_native") + (" ★" if usable else ""))
            nb.set_sensitive(usable)
            if key not in sw.native_plans:
                nb.set_tooltip_text(_("task_native_checking"))
            elif usable:
                nb.set_tooltip_text(f"{_('task_recommended')} · {launcher_tasks._native_desc(sw.app, native)}")
            else:
                nb.set_tooltip_text(_(native.reason).format(**native.args))
            fb.set_tooltip_text(_("task_flatpak_desc").format(
                app_id=launcher_tasks.flatpak_plan(self.item.ident).package))
            picked = sw.source_of(key)
            self._setting = True
            nb.set_active(picked == "native")
            fb.set_active(picked == "flatpak")
            self._setting = False

    _setting = False

    def _source_toggled(self, btn, source: str):
        if self._setting:
            return
        if btn.get_active():
            self.sw.set_source(self.key, source)
        else:
            # one of the two is always on
            self.refresh()

    def _do(self, work: Callable[[], None]):
        self.close()
        work()

    def close(self):
        if self.sw.card is self:
            self.sw.card = None
        self.dialog.destroy()
        back = self.sw.card_from
        self.sw.card_from = None
        if back is not None and back.get_mapped():
            back.grab_focus()
        else:
            self.sw._drop_focus()

    def _on_key(self, _w, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            # what's stood on - else Install, the card's own Enter
            focus = self.dialog.get_focus()
            if isinstance(focus, Gtk.Button):
                focus.clicked()
            elif self.install_btn.get_visible():
                self.install_btn.clicked()
            return True
        arrows = {Gdk.KEY_Up: "up", Gdk.KEY_Down: "down", Gdk.KEY_Left: "left", Gdk.KEY_Right: "right"}
        if event.keyval in arrows:
            self._nav(arrows[event.keyval])
            return True
        return False

    def _nav(self, action: str):
        keynav.on(self.dialog)
        if action == "back":
            self.close()
        elif action in ("left", "right", "up", "down"):
            self.dialog.child_focus({"left": Gtk.DirectionType.LEFT, "right": Gtk.DirectionType.RIGHT,
                                     "up": Gtk.DirectionType.UP, "down": Gtk.DirectionType.DOWN}[action])
        elif action == "activate":
            focus = self.dialog.get_focus()
            if focus is not None:
                focus.activate()
            elif self.install_btn.get_visible():
                self.install_btn.clicked()


class Shipwright:
    def __init__(self, app, parent: Gtk.Window, focus: Optional[str] = None,
                 on_closed: Optional[Callable[[], None]] = None):
        self.app = app
        self._ = app._
        self.focus = focus
        self.on_closed = on_closed
        self.busy = False
        self.closed = False
        self.immutable = immutable_system()
        self.items: Dict[str, Item] = {}
        self.states: Dict[str, Dict] = {}
        self.updates: Dict[str, object] = {}         # key -> tools.Update
        self.system_updates: Dict[str, object] = {}  # every upgradable package
        self.family = detect_linux_distro_family()
        self.full_only = full_upgrade_only(self.family)
        self.selected: Set[str] = set()
        self.install_picks: Set[str] = set()
        self.checks: List = []
        self.flatpak_launchers: List = []
        self.flatpak_names = ""
        self.layer_refs: Dict[str, List[str]] = {}
        self.layer_got: Dict[str, List[str]] = {}
        self.tiles: Dict[str, List[Tile]] = {}
        self.cat_tiles: Dict[str, Gtk.Label] = {}
        self.sections: Dict[str, Gtk.Box] = {}
        self.sources: Dict[str, str] = {}           # key -> "native" | "flatpak", as picked on the tile
        self.native_plans: Dict[str, object] = {}   # key -> launcher_tasks.native_plan()'s answer
        self.flatpak_ok: Dict[str, bool] = {}
        self.asking: Set[str] = set()
        self.filter = "all"
        self._spying = False
        self._nav_target = None
        self._scroll_anim = None
        self.page = ""      # none yet: the first goto() shows the overview
        self.card: Optional[ToolCard] = None     # a tile's card, while it's open
        self.card_from: Optional[Gtk.Widget] = None     # the tile it was opened from by keyboard
        self._build_items()

        dlg = Gtk.Dialog(transient_for=parent, modal=True)
        dlg.set_title(self._("sw_title"))
        dlg.set_decorated(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.get_style_context().add_class("home-launch-dialog")
        # A set size, fitted to the main window: the content wraps into
        # it, rather than the window growing to the content's (translated)
        # natural width.
        w, h = parent.get_size()
        dlg.set_size_request(860, 560)
        dlg.set_default_size(max(860, min(1180, w - 80)), max(560, min(880, h - 60)))
        action_area = dlg.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()
        dlg.connect("key-press-event", self._on_key)
        dlg.connect("delete-event", lambda *_: (self.close(), True)[1])
        dlg.plunderhub_nav = self._nav
        dlg.connect("set-focus", lambda _d, widget: GLib.idle_add(self._keep_in_view, widget))
        keynav.track(dlg)
        self.dialog = dlg

        content = dlg.get_content_area()
        content.set_border_width(0)
        outer = Gtk.Overlay()
        content.pack_start(outer, True, True, 0)
        body = Gtk.Box()
        outer.add(body)
        body.pack_start(self._sidebar(), False, False, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right.pack_start(self._header(), False, False, 0)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(150)
        right.pack_start(self.stack, True, True, 0)
        right.pack_start(self._action_bar(), False, False, 0)
        body.pack_start(right, True, True, 0)

        self.overview_scroller = self._scroll(self._overview_page())
        self.overview_scroller.get_vadjustment().connect("value-changed", self._on_scrolled)
        self.stack.add_named(self.overview_scroller, "overview")
        self.stack.add_named(self._scroll(self._inspection_page()), "inspection")
        self.updates_flow = self._flow()
        updates_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        # what updating means here, and everything updated in one go
        updates_head = Gtk.Box(spacing=20)
        self.updates_note = Gtk.Label(xalign=0)
        self.updates_note.set_line_wrap(True)
        self.updates_note.set_max_width_chars(90)
        self.updates_note.get_style_context().add_class("settings-row-sub")
        updates_head.pack_start(self.updates_note, True, True, 0)
        self.update_all_btn = self.app._dialog_button(self._("sw_update"), "accent")
        self.update_all_btn.set_valign(Gtk.Align.CENTER)
        self.update_all_btn.set_focus_on_click(False)
        self.update_all_btn.connect("clicked", lambda *_: self._run_updates(list(self.updates)))
        updates_head.pack_end(self.update_all_btn, False, False, 0)
        updates_box.pack_start(updates_head, False, False, 0)
        updates_box.pack_start(self.updates_flow, False, False, 0)
        self.stack.add_named(self._scroll(updates_box), "updates")
        from plunderhub.ui import proton_page
        self.proton_page = (PROTON_PAGE or (lambda sw: proton_page.ProtonPage(
            sw, proton_page.RealProtonData(sw.app))))(self)
        self.stack.add_named(self._scroll(self.proton_page), "protons")
        from plunderhub.ui import bilge_page
        self.bilge_page = bilge_page.BilgePage(self)
        self.stack.add_named(self._scroll(self.bilge_page), "bilge")

        content.show_all()
        self._set_collapsed(not self.app.settings.get("shipwright_sidebar_open", True))
        self.nav_rows["updates"].hide()
        self._refresh_tiles()
        dlg.connect("map", lambda *_: GLib.idle_add(lambda: dlg.get_window() is not None and dlg.set_focus(None)))
        app._enter_dimmed_dialog()
        dlg.show()
        self.goto("overview")
        self._refresh()
        self._reveal_focus()

    # --- the catalogue ----------------------------------------------------------------

    def _build_items(self):
        _ = self._
        for ldef in LAUNCHER_REGISTRY:
            self.items[ldef.name] = Item(ldef.name, ldef.name, "launchers",
                                         _(f"sw_desc_launcher_{ldef.name.lower()}"), launcher=True, gui=True)
        for tool in TOOLS:
            used = f"sw_used_{tool.id}"
            # product names stay as they are; a descriptive one is translated
            name = _(f"sw_name_{tool.id}") if f"sw_name_{tool.id}" in self.app.strings else tool.name
            self.items[tool.id] = Item(tool.id, name, tool.group, _(f"sw_desc_{tool.id}"),
                                       _(used) if used in self.app.strings else "", tool.icon,
                                       gui=tool.gui, removable=tool.removable, ident=tool.name)
        for layer in flatpak_layers.LAYERS:
            key = f"flatpak:{layer.id}"
            used = f"sw_used_{layer.id}"
            self.items[key] = Item(key, layer.name, "flatpak", _(f"sw_layer_desc_{layer.id}"),
                                   _(used) if used in self.app.strings else "", layer.icon, layer=layer)

    def _drawn_icon(self, icon: str, px: int) -> Gtk.Widget:
        """The app's tinted icon, drawn (see _pixbuf_area) - on the nav rows
        and buttons, an image re-laid the window out at every hover or
        change of the picked row, scrolling included."""
        pb = self.app._icon_pixbuf(icon, px)
        return _pixbuf_area(pb) if pb is not None else self.app._icon_image_at_size(icon, px)

    def _in(self, category: str) -> List[Item]:
        return [i for i in self.items.values() if i.category == category]

    # --- building ---------------------------------------------------------------------

    def _scroll(self, child: Gtk.Widget) -> Gtk.ScrolledWindow:
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_width(False)
        # Settings' scrollbar: always there, full width, in the accent
        scroller.set_overlay_scrolling(False)
        scroller.get_style_context().add_class("settings-scroll")
        child.set_margin_start(28)
        child.set_margin_end(28)
        child.set_margin_top(6)
        child.set_margin_bottom(24)
        scroller.add(child)
        return scroller

    def _sidebar(self) -> Gtk.Box:
        _ = self._
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        side.get_style_context().add_class("settings-sidebar")
        side.set_size_request(250, -1)
        brand = Gtk.Box(spacing=12)
        brand.set_margin_top(20)
        brand.set_margin_start(18)
        brand.set_margin_bottom(6)
        badge = Gtk.DrawingArea()
        badge.set_size_request(42, 42)

        def draw(widget, cr):
            accent = _accent_rgb(widget)
            cr.arc(21, 21, 20, 0, 2 * math.pi)
            cr.set_source_rgba(*accent, 0.18)
            cr.fill()
            pb = get_icon_pixbuf("hammer-symbolic", 22, _hex(accent))
            if pb is not None:
                Gdk.cairo_set_source_pixbuf(cr, pb, 10, 10)
                cr.paint()
            return False
        badge.connect("draw", draw)
        brand.pack_start(badge, False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        titles.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=_("sw_title"), xalign=0)
        title.get_style_context().add_class("settings-app-title")
        self.summary = Gtk.Label(xalign=0)
        self.summary.get_style_context().add_class("settings-app-sub")
        titles.pack_start(title, False, False, 0)
        titles.pack_start(self.summary, False, False, 0)
        brand.pack_start(titles, False, False, 0)
        side.pack_start(brand, False, False, 0)

        self.immutable_note = None
        self.nav = Gtk.ListBox()
        self.nav.get_style_context().add_class("settings-nav")
        self.nav.get_style_context().add_class("sw-nav")
        self.nav.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self.nav_rows: Dict[str, Gtk.ListBoxRow] = {}
        self.nav_badges: Dict[str, Gtk.Label] = {}
        self.nav_icons: Dict[str, "NavIcon"] = {}
        cats = list(CATEGORIES)
        cats.insert(cats.index("inspection") + 1, "protons")
        cats.insert(cats.index("protons") + 1, "bilge")
        for cat in cats:
            row = Gtk.ListBoxRow()
            row.page = cat
            line = Gtk.Box(spacing=12)
            icon = NavIcon(self.app._icon_pixbuf(CATEGORY_ICONS.get(cat, "wine-symbolic"), 18))
            line.pack_start(icon, False, False, 0)
            self.nav_icons[cat] = icon
            name = Gtk.Label(label=self._cat_title(cat), xalign=0)
            line.pack_start(name, True, True, 0)
            badge_label = Gtk.Label()
            badge_label.get_style_context().add_class("sw-nav-count")
            # shown only with something in it (see _set_badge)
            badge_label.set_no_show_all(True)
            line.pack_end(badge_label, False, False, 0)
            row.add(line)
            row.sw_line, row.sw_name = line, name
            self.nav.add(row)
            self.nav_rows[cat] = row
            self.nav_badges[cat] = badge_label
            if cat == "updates":
                # the pages, then a hairline, then the shelves
                self.nav.add(self._nav_divider())
        self.nav.connect("row-selected", self._on_nav)
        side.pack_start(self.nav, False, False, 0)
        if self.immutable:
            note = Gtk.Label(label=_("sw_immutable_note").format(system=self.immutable), xalign=0)
            note.set_line_wrap(True)
            note.set_max_width_chars(28)
            note.get_style_context().add_class("settings-app-sub")
            note.set_margin_start(18)
            note.set_margin_end(14)
            side.pack_end(note, False, False, 16)
            self.immutable_note = note
        self.collapse_btn = Gtk.Button()
        self.collapse_btn.get_style_context().add_class("sw-collapse")
        self.collapse_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.collapse_btn.set_focus_on_click(False)
        self.collapse_btn.connect("clicked", lambda *_: self._set_collapsed(not self.collapsed, save=True))
        side.pack_end(self.collapse_btn, False, False, 12)
        self.side, self.brand, self.brand_titles = side, brand, titles
        return side

    @staticmethod
    def _nav_divider() -> Gtk.ListBoxRow:
        """A hairline between the sidebar's pages and its shelves - one
        that nobody lands on."""
        row = Gtk.ListBoxRow()
        row.set_selectable(False)
        row.set_activatable(False)
        row.set_can_focus(False)
        row.page = None
        row.get_style_context().add_class("sw-nav-divider")
        line = Gtk.Box()
        line.set_size_request(-1, 1)
        line.get_style_context().add_class("sw-nav-divider-line")
        row.add(line)
        return row

    def _set_collapsed(self, collapsed: bool, save: bool = False):
        """The sidebar opened out with the shelves' names (the default),
        or folded to its icons, counts and update markers."""
        self.collapsed = collapsed
        ctx = self.side.get_style_context()
        (ctx.add_class if collapsed else ctx.remove_class)("sw-collapsed")
        self.side.set_size_request(84 if collapsed else 250, -1)
        self.brand_titles.set_visible(not collapsed)
        self.brand.set_halign(Gtk.Align.CENTER if collapsed else Gtk.Align.FILL)
        self.brand.set_margin_start(0 if collapsed else 18)
        if self.immutable_note is not None:
            self.immutable_note.set_visible(not collapsed)
        for cat, row in self.nav_rows.items():
            row.sw_name.set_visible(not collapsed)
            row.sw_line.set_orientation(Gtk.Orientation.VERTICAL if collapsed else Gtk.Orientation.HORIZONTAL)
            row.sw_line.set_spacing(3 if collapsed else 12)
            row.sw_line.set_halign(Gtk.Align.CENTER if collapsed else Gtk.Align.FILL)
            # Folded, every row is as tall as one with a count under its
            # icon, content centred - Overview, with no count, has its
            # icon in the middle of the same-sized highlight.
            row.sw_line.set_valign(Gtk.Align.CENTER)
            row.set_size_request(-1, 54 if collapsed else -1)
            row.set_tooltip_text(self._cat_title(cat) if collapsed else None)
        btn = self.collapse_btn
        child = btn.get_child()
        if child is not None:
            btn.remove(child)
        icon = self._drawn_icon("panel-left-open-symbolic" if collapsed else "panel-left-close-symbolic", 18)
        # in the middle of the button, its hover highlight round it
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        btn.add(icon)
        btn.show_all()
        btn.set_halign(Gtk.Align.CENTER if collapsed else Gtk.Align.START)
        btn.set_margin_start(0 if collapsed else 14)
        btn.set_tooltip_text(self._("sw_sidebar_expand" if collapsed else "sw_sidebar_collapse"))
        if save:
            self.app.settings["shipwright_sidebar_open"] = not collapsed
            save_settings(self.app.settings)

    def _cat_title(self, cat: str) -> str:
        if cat == "inspection":
            return self._("insp_title")
        if cat == "updates":
            return self._("sw_updates")
        if cat == "protons":
            return self.proton_page.TITLE if getattr(self, "proton_page", None) else "Proton"
        if cat == "bilge":
            return self._("bg_title")
        if cat == "launchers":
            return self._("sources_header_launchers")
        return self._(f"sw_group_{cat}" if cat != "overview" else "sw_overview")

    def _header(self) -> Gtk.Box:
        head = Gtk.Box(spacing=16)
        head.set_margin_start(28)
        head.set_margin_end(28)
        head.set_margin_top(22)
        head.set_margin_bottom(14)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.page_title = Gtk.Label(xalign=0)
        self.page_title.get_style_context().add_class("settings-page-title")
        self.page_sub = Gtk.Label(xalign=0)
        self.page_sub.set_line_wrap(True)
        self.page_sub.set_max_width_chars(60)
        self.page_sub.get_style_context().add_class("settings-page-sub")
        texts.pack_start(self.page_title, False, False, 0)
        texts.pack_start(self.page_sub, False, False, 0)
        head.pack_start(texts, True, True, 0)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(self._("sw_search"))
        self.search.set_width_chars(22)
        self.search.set_valign(Gtk.Align.CENTER)
        self.search.connect("search-changed", self._on_search)
        # closed from the top-right corner
        self.close_btn = Gtk.Button()
        self.close_btn.get_style_context().add_class("about-close")
        cross = self._drawn_icon("window-close-symbolic", 16)
        cross.set_halign(Gtk.Align.CENTER)
        cross.set_valign(Gtk.Align.CENTER)
        self.close_btn.add(cross)
        self.close_btn.set_valign(Gtk.Align.CENTER)
        self.close_btn.set_focus_on_click(False)
        self.close_btn.set_tooltip_text(self._("close_btn"))
        self.close_btn.connect("clicked", lambda *_: self.close())
        head.pack_end(self.close_btn, False, False, 0)
        head.pack_end(self.search, False, False, 0)
        return head

    @staticmethod
    def _flow() -> Gtk.FlowBox:
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_min_children_per_line(2)
        flow.set_max_children_per_line(4)
        flow.set_column_spacing(14)
        flow.set_row_spacing(14)
        flow.set_valign(Gtk.Align.START)
        return flow

    def _add_tile(self, flow: Gtk.FlowBox, item: Item) -> Tile:
        tile = Tile(self, item)
        flow.add(tile)
        # the tile's button takes the focus, not the flowbox's child
        tile.get_parent().set_can_focus(False)
        self.tiles.setdefault(item.key, []).append(tile)
        return tile

    def _section(self, cat: str) -> Gtk.Box:
        """One shelf of the list: its name, how much of it is installed
        and a Pick missing, then its tiles."""
        _ = self._
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(14)
        head = Gtk.Box(spacing=10)
        head.pack_start(_bare_icon(CATEGORY_ICONS[cat], 18), False, False, 0)
        title = Gtk.Label(label=self._cat_title(cat).upper(), xalign=0)
        title.get_style_context().add_class("settings-group-title")
        head.pack_start(title, False, False, 0)
        count = Gtk.Label(xalign=0)
        count.get_style_context().add_class("sw-section-count")
        head.pack_start(count, False, False, 0)
        pick = Gtk.Button(label=_("sw_pick_missing"))
        pick.get_style_context().add_class("sw-link")
        pick.set_relief(Gtk.ReliefStyle.NONE)
        pick.set_focus_on_click(False)
        pick.set_no_show_all(True)
        pick.connect("clicked", lambda *_: self._pick_missing(self._in(cat)))
        head.pack_end(pick, False, False, 0)
        box.pack_start(head, False, False, 0)
        flow = self._flow()
        for item in self._in(cat):
            self._add_tile(flow, item)
        box.pack_start(flow, False, False, 0)
        box.sw_flow, box.sw_pick = flow, pick
        pick.sw_cat = cat
        self.cat_tiles[cat] = count
        self.sections[cat] = box
        return box

    def _hero(self, icon: str, title: Gtk.Label, sub: Gtk.Label, button: Gtk.Button) -> Gtk.Box:
        """A card for something to do: icon, a line, what it's about and
        its button on the right."""
        card = Gtk.Box(spacing=18)
        card.get_style_context().add_class("sw-hero")
        card.pack_start(_bare_icon(icon, 34), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        texts.set_valign(Gtk.Align.CENTER)
        title.get_style_context().add_class("settings-row-title")
        sub.set_line_wrap(True)
        sub.set_max_width_chars(60)
        sub.get_style_context().add_class("settings-row-sub")
        texts.pack_start(title, False, False, 0)
        texts.pack_start(sub, False, False, 0)
        card.pack_start(texts, True, True, 0)
        button.set_valign(Gtk.Align.CENTER)
        button.set_focus_on_click(False)
        card.pack_end(button, False, False, 0)
        card.set_no_show_all(True)
        for widget in card.get_children():
            widget.show_all()
        return card

    def _overview_page(self) -> Gtk.Box:
        """Everything on one list: the inspection in a line, cards only
        for what's waiting (the missing part of the Recommended kit,
        updates), the filters, then every shelf - the sidebar jumps to
        them and follows the scrolling."""
        _ = self._
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        # the Ship inspection at a glance - a card of its own only with warnings
        strip = Gtk.Box(spacing=14)
        strip.get_style_context().add_class("sw-strip")
        ring = Gtk.DrawingArea()
        ring.set_size_request(40, 40)
        ring.set_valign(Gtk.Align.CENTER)
        ring.connect("draw", self._draw_score)
        self.score_ring = ring
        strip.pack_start(ring, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        texts.set_valign(Gtk.Align.CENTER)
        t = Gtk.Label(label=_("insp_title"), xalign=0)
        t.set_ellipsize(Pango.EllipsizeMode.END)
        t.get_style_context().add_class("sw-strip-title")
        self.strip_title = t
        self.score_text = Gtk.Label(label=_("insp_running"), xalign=0)
        self.score_text.set_ellipsize(Pango.EllipsizeMode.END)
        self.score_text.get_style_context().add_class("settings-row-sub")
        texts.pack_start(t, False, False, 0)
        texts.pack_start(self.score_text, False, False, 0)
        strip.pack_start(texts, True, True, 0)
        look = Gtk.Button(label=_("sw_open_inspection") + "  ›")
        look.get_style_context().add_class("sw-link")
        look.set_relief(Gtk.ReliefStyle.NONE)
        look.set_focus_on_click(False)
        look.set_valign(Gtk.Align.CENTER)
        look.connect("clicked", lambda *_: self.goto("inspection"))
        strip.pack_end(look, False, False, 0)
        self.strip_look = look
        # with warnings: fixed from right here
        self.strip_fix = self.app._dialog_button(_("insp_fix"), "accent")
        self.strip_fix.set_valign(Gtk.Align.CENTER)
        self.strip_fix.set_focus_on_click(False)
        self.strip_fix.connect("clicked", lambda *_: self._run_fixes(self._fixable()))
        self.strip_fix.get_child().show_all()
        self.strip_fix.set_no_show_all(True)
        strip.pack_end(self.strip_fix, False, False, 0)
        self.strip = strip
        box.pack_start(strip, False, False, 0)

        # the Recommended kit - while some of it is missing
        self.rec_sub = Gtk.Label(xalign=0)
        self.rec_btn = self.app._dialog_button(_("sw_recommended_pick"), "accent")
        self.rec_btn.connect("clicked", lambda *_: self._pick_recommended())
        self.rec_card = self._hero("sparkles-symbolic", Gtk.Label(label=_("sw_recommended"), xalign=0),
                                   self.rec_sub, self.rec_btn)
        box.pack_start(self.rec_card, False, False, 0)

        # updates, while some wait
        self.update_card_title = Gtk.Label(xalign=0)
        self.update_card_sub = Gtk.Label(xalign=0)
        self.update_card_btn = self.app._dialog_button(self._update_label(None), "accent")
        self.update_card_btn.connect("clicked", lambda *_: self._run_updates(list(self.updates)))
        self.update_card = self._hero("view-refresh-symbolic", self.update_card_title,
                                      self.update_card_sub, self.update_card_btn)
        box.pack_start(self.update_card, False, False, 0)

        chips = Gtk.Box(spacing=8)
        chips.set_margin_top(6)
        self.chips: Dict[str, Gtk.ToggleButton] = {}
        for key in ("all", "missing", "installed", "updates"):
            chip = Gtk.ToggleButton(label=_(f"sw_filter_{key}"))
            chip.get_style_context().add_class("sw-chip")
            allow_hover_relayout(chip, "its dimmed text lights up under the pointer")
            chip.set_focus_on_click(False)
            chip.set_active(key == "all")
            chip.connect("toggled", self._on_chip, key)
            chips.pack_start(chip, False, False, 0)
            self.chips[key] = chip
        self.chips["updates"].set_no_show_all(True)
        box.pack_start(chips, False, False, 0)
        self.chips_box = chips

        for cat in TILE_CATEGORIES:
            box.pack_start(self._section(cat), False, False, 0)
        self.list_empty = Gtk.Label(label=_("sw_no_results"))
        self.list_empty.get_style_context().add_class("settings-page-sub")
        self.list_empty.set_margin_top(30)
        self.list_empty.set_no_show_all(True)
        box.pack_start(self.list_empty, False, False, 0)
        self.overview_box = box
        return box

    def _draw_score(self, widget, cr):
        checks = self.checks
        accent = _accent_rgb(widget)
        ctx = widget.get_style_context()
        fg = ctx.get_color(ctx.get_state())
        size = widget.get_allocated_width()
        line = max(4, round(size / 10))
        cx = cy = size / 2
        r = (size - line) / 2 - 1
        cr.set_line_width(line)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.10)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        if checks:
            ok = sum(1 for c in checks if c.status == inspection.OK)
            warn = sum(1 for c in checks if c.status == inspection.WARN)
            colour = AMBER if warn else accent
            cr.set_line_cap(1)
            cr.set_source_rgb(*colour)
            cr.arc(cx, cy, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * max(0.02, ok / len(checks)))
            cr.stroke()
            px = round(size * 0.4)
            pb = get_icon_pixbuf("triangle-alert-symbolic" if warn else "emblem-ok-symbolic", px, _hex(colour))
            if pb is not None:
                Gdk.cairo_set_source_pixbuf(cr, pb, cx - px / 2, cy - px / 2)
                cr.paint()
        return False

    def _inspection_page(self) -> Gtk.Box:
        _ = self._
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        buttons = Gtk.Box(spacing=8)
        self.fix_all = self.app._dialog_button(_("sw_fix_all"), "accent")
        self.fix_all.connect("clicked", lambda *_: self._run_fixes(self._fixable()))
        # only there while something can be fixed
        self.fix_all.get_child().show_all()
        self.fix_all.set_no_show_all(True)
        self.inspect_again = self.app._dialog_button(_("insp_again"), "ghost")
        self.inspect_again.connect("clicked", lambda *_: self._run_inspection())
        buttons.pack_start(self.inspect_again, False, False, 0)
        buttons.pack_start(self.fix_all, False, False, 0)
        box.pack_start(buttons, False, False, 0)
        self.inspect_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.inspect_card.get_style_context().add_class("settings-card")
        box.pack_start(self.inspect_card, False, False, 0)
        return box

    def _action_bar(self) -> Gtk.Revealer:
        """What's picked and what can be done with it - sliding up only
        while something is picked."""
        _ = self._
        bar = Gtk.Box(spacing=10)
        bar.get_style_context().add_class("sw-actionbar")
        self.picked = Gtk.Label(xalign=0)
        self.picked.set_ellipsize(Pango.EllipsizeMode.END)
        self.picked.set_max_width_chars(40)
        self.picked.get_style_context().add_class("sw-picked")
        bar.pack_start(self.picked, True, True, 0)
        self.clear_btn = self.app._dialog_button(_("sw_clear"), "ghost")
        self.clear_btn.connect("clicked", lambda *_: self._clear())
        self.remove_btn = self.app._dialog_button(_("task_remove_btn"), "danger")
        self.remove_btn.connect("clicked", lambda *_: self._run_selected("remove"))
        self.install_btn = self.app._dialog_button(_("install_btn"), "accent")
        self.install_btn.connect("clicked", lambda *_: self._run_selected("install"))
        self.update_btn = self.app._dialog_button(_("sw_update"), "accent")
        self.update_btn.connect("clicked", lambda *_: self._run_updates(
            [k for k in self.selected if k in self.updates and self._installed(k)]))
        for b in (self.clear_btn, self.remove_btn, self.update_btn, self.install_btn):
            b.get_child().show_all()     # no_show_all keeps show_all() off the label too
            b.set_no_show_all(True)
            b.set_focus_on_click(False)
            bar.pack_start(b, False, False, 0)
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        revealer.set_transition_duration(180)
        revealer.add(bar)
        self.action_revealer = revealer
        return revealer

    @staticmethod
    def _set_label(btn: Gtk.Button, text: str):
        # None once Shipwright is closed: an update check still running then
        # comes back to a button that's gone
        child = btn.get_child()
        if child is not None:
            child.get_children()[0].set_text(text)

    # --- navigation -------------------------------------------------------------------

    def goto(self, page: str):
        """A page - or a shelf, which is a stretch of the overview's list."""
        if page not in self.nav_rows:
            return
        if page not in TILE_CATEGORIES and page != "overview" and self.search.get_text():
            self.search.set_text("")
        row = self.nav_rows[page]
        if self.nav.get_selected_row() is not row:
            self.nav.select_row(row)
        else:
            self._on_nav(self.nav, row)
        # a jump from a tile mustn't leave the focus ring on the old row
        self._drop_focus()

    def _drop_focus(self):
        """Clears the focus and any hover / pressed look a modal card
        dialog left behind on the button that opened it."""
        focus = self.dialog.get_focus()
        if isinstance(focus, Gtk.Widget):
            focus.unset_state_flags(Gtk.StateFlags.PRELIGHT | Gtk.StateFlags.ACTIVE)
        self.update_card_btn.unset_state_flags(Gtk.StateFlags.PRELIGHT | Gtk.StateFlags.ACTIVE)
        self.dialog.set_focus(None)

    def _on_nav(self, _list, row):
        if row is None or self._spying:
            return
        page = "overview" if row.page in TILE_CATEGORIES else row.page
        if page != self.page:
            # what was picked on the other page isn't carried over
            self._clear()
            self._show_page(page)
        if row.page in TILE_CATEGORIES:
            self._scroll_to_section(row.page)
        elif row.page == "overview":
            self._animate_scroll(self.overview_scroller.get_vadjustment(), 0)

    def _show_page(self, page: str):
        _ = self._
        self.page = page
        self.stack.set_visible_child_name(page)
        self.page_title.set_text(_("sw_headline") if page == "overview" else self._cat_title(page))
        self.page_sub.set_text(self.proton_page.SUB if page == "protons" else _("bg_sub") if page == "bilge"
                               else _(f"sw_cat_sub_{page}"))
        self._update_action_bar()

    # --- the list: jumping to a shelf, the sidebar following the scrolling ----------------

    def _section_top(self, cat: str) -> Optional[int]:
        coords = self.sections[cat].translate_coordinates(self.overview_box, 0, 0)
        return None if coords is None else coords[1]

    def _scroll_to_section(self, cat: str):
        if not self.sections[cat].get_visible():
            return
        if self.overview_box.get_allocated_height() <= 1:
            # not laid out yet (just opened)
            GLib.idle_add(lambda: (self._scroll_to_section(cat), False)[1])
            return
        adj = self.overview_scroller.get_vadjustment()
        top = self._section_top(cat)
        if top is None:
            return
        target = max(adj.get_lower(), min(top - 6, adj.get_upper() - adj.get_page_size()))
        self._nav_target = (cat, target)
        self._animate_scroll(adj, target)

    def _animate_scroll(self, adj: Gtk.Adjustment, target: float, ms: int = 260):
        """Glides to target, easing out."""
        if self._scroll_anim is not None:
            GLib.source_remove(self._scroll_anim)
            self._scroll_anim = None
        start, t0 = adj.get_value(), GLib.get_monotonic_time()
        if abs(target - start) < 1:
            return

        def step() -> bool:
            p = min(1.0, (GLib.get_monotonic_time() - t0) / (ms * 1000))
            adj.set_value(start + (target - start) * (1 - (1 - p) ** 3))
            if p >= 1:
                self._scroll_anim = None
                return False
            return True
        self._scroll_anim = GLib.timeout_add(16, step)

    def _on_scrolled(self, adj):
        """The sidebar marks the shelf at the top of the view - or the one
        just jumped to, when the list can't scroll it all the way up."""
        if self.page != "overview" or self.closed:
            return
        value = adj.get_value()
        stood = self._stood_on_shelf()
        if stood is not None:
            # the keyboard or a controller is on the list: its shelf, not the scroll's
            current = stood
        elif self._nav_target and abs(value - self._nav_target[1]) < 2:
            current = self._nav_target[0]
        else:
            if self._nav_target and self._scroll_anim is None:
                self._nav_target = None
            current = "overview"
            for cat in TILE_CATEGORIES:
                top = self._section_top(cat) if self.sections[cat].get_visible() else None
                if top is not None and top - 40 <= value:
                    current = cat
        row = self.nav_rows[current]
        if self.nav.get_selected_row() is not row:
            self._spying = True
            self.nav.select_row(row)
            self._spying = False

    def _stood_on_shelf(self) -> Optional[str]:
        """The shelf of what the focus is on in the list (the cards and
        filters over the shelves: the overview), else None."""
        focus = self.dialog.get_focus() if getattr(self, "dialog", None) is not None else None
        if focus is None or self.page != "overview" or not focus.is_ancestor(self.overview_box):
            return None
        return getattr(focus, "sw_cat", None) or "overview"

    def _mark_stood_shelf(self):
        cat = self._stood_on_shelf()
        if cat is not None and cat in self.nav_rows and self.nav.get_selected_row() is not self.nav_rows[cat]:
            self._spying = True
            self.nav.select_row(self.nav_rows[cat])
            self._spying = False

    # --- searching and the filters --------------------------------------------------------

    def _on_search(self, entry):
        if self.page != "overview":
            self.goto("overview")
        self._apply_filter()

    def _on_chip(self, chip, key: str):
        if not chip.get_active():
            # one is always on: clicking the lit one keeps it lit
            if self.filter == key:
                chip.set_active(True)
            return
        self.filter = key
        for other, c in self.chips.items():
            if other != key and c.get_active():
                c.set_active(False)
        self._apply_filter()

    def _shown(self, item: Item) -> bool:
        text = self.search.get_text().strip().lower()
        if text and text not in item.name.lower() and text not in item.desc.lower():
            return False
        if self.filter == "all":
            return True
        if item.key not in self.states:
            return False
        if self.filter == "missing":
            return not self._installed(item.key)
        if self.filter == "installed":
            return self._installed(item.key)
        return item.key in self.updates

    def _apply_filter(self):
        """Shows what the search and the filter let through; shelves left
        empty go, and so do the cards above while searching."""
        searching = bool(self.search.get_text().strip())
        any_shown = False
        for cat, section in self.sections.items():
            shown = False
            for child in section.sw_flow.get_children():
                visible = self._shown(child.get_child().item)
                child.set_visible(visible)
                shown = shown or visible
            section.set_visible(shown)
            any_shown = any_shown or shown
        self.list_empty.set_visible(not any_shown)
        self.strip.set_visible(not searching)
        self._update_heroes()

    # --- picking ----------------------------------------------------------------------

    def _here(self, key: str) -> bool:
        """Installed itself - for a tool, on the system."""
        st = self.states.get(key)
        return bool(st and st["installed"] and not st["partial"])

    def _part(self, key: str) -> Optional[str]:
        """The tool's Flatpak part, while a launcher is a Flatpak."""
        part = FLATPAK_PARTS.get(key)
        return part if part and self.flatpak_launchers and part in self.states else None

    def _installed(self, key: str) -> bool:
        """All there: the tool, and its Flatpak part where one's needed."""
        part = self._part(key)
        return self._here(key) and (part is None or self._here(part))

    def set_selected(self, key: str, on: bool):
        (self.selected.add if on else self.selected.discard)(key)
        if on and self._on_flathub(key) and key not in self.native_plans:
            self._ask_native(key)
        for tile in self.tiles.get(key, []):
            tile.refresh()
        self._update_action_bar()

    def show_card(self, key: str):
        """A tile's card: what it is, and Install / Remove."""
        if self.card is not None:
            self.card.close()
        # opened from the keyboard or a controller (a click doesn't focus
        # a tile): back onto the tile afterwards, the card's own button first
        focus = self.dialog.get_focus()
        tile = focus.get_parent() if focus is not None else None
        self.card_from = focus if isinstance(tile, Tile) and tile.item.key == key else None
        self.card = ToolCard(self, key, keyboard=self.card_from is not None)

    def _refresh_card(self, key: Optional[str] = None):
        if self.card is not None and (key is None or self.card.key == key):
            self.card.refresh()

    def state_line(self, key: str):
        """A tile's (and its card's) state in words, and whether it's on."""
        _ = self._
        st = self.states.get(key)
        if st is None:
            return _("task_native_checking"), False
        if st["partial"]:
            return "◐ " + _("sw_partly"), False
        if st["installed"]:
            return "● " + _("source_installed") + (f" · {st['source']}" if st["source"] else ""), True
        if st.get("unavailable"):
            return "○ " + _("sw_unavailable_here"), False
        return "○ " + _("source_not_installed"), False

    # --- where an install comes from: the system package or the Flatpak ---------------------

    def _on_flathub(self, key: str) -> bool:
        if key not in self.flatpak_ok:
            item = self.items[key]
            self.flatpak_ok[key] = item.layer is None and launcher_tasks.flatpak_plan(item.ident) is not None
        return self.flatpak_ok[key]

    def _offers_choice(self, key: str) -> bool:
        """Not there, and on Flathub too: its card offers both sources."""
        return key in self.states and not self._here(key) and self._on_flathub(key)

    def _has_choice(self, key: str) -> bool:
        """Picked for installing, with a choice of where from."""
        return key in self.selected and self._offers_choice(key)

    def source_of(self, key: str) -> str:
        """As picked - else, as in Settings, the system package once it's
        known to be there, the Flatpak till then or without one."""
        if key in self.sources:
            return self.sources[key]
        return "native" if isinstance(self.native_plans.get(key), launcher_tasks.Plan) else "flatpak"

    def set_source(self, key: str, source: str):
        self.sources[key] = source
        self._refresh_card(key)
        self._update_action_bar()

    def _ask_native(self, key: str):
        """Whether the repositories have it - a moment's package queries."""
        if key in self.asking:
            return
        self.asking.add(key)
        name = self.items[key].ident

        def work():
            try:
                plan = launcher_tasks.native_plan(name)
            except Exception:
                logger.exception(f"Checking {name}'s system package")
                plan = launcher_tasks.Unavailable("task_native_unavailable")

            def done() -> bool:
                self.asking.discard(key)
                if not self.closed:
                    self.native_plans[key] = plan
                    self._refresh_card(key)
                    self._update_action_bar()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _select_all(self):
        """Ctrl+A: every update on Updates, else whatever's missing of
        what the list shows."""
        if self.page == "updates":
            for key in self.updates:
                self.set_selected(key, True)
        elif self.page == "overview":
            self._pick_missing([c.get_child().item for s in self.sections.values() if s.get_visible()
                                for c in s.sw_flow.get_children() if c.get_visible()])

    def _pick_missing(self, items: List[Item]):
        for item in items:
            if item.key in self.states and not self._installed(item.key) \
                    and not self.states[item.key].get("unavailable"):
                self.set_selected(item.key, True)

    def _missing_recommended(self) -> List[str]:
        return [k for k in RECOMMENDED if k in self.items and k in self.states and not self._installed(k)
                and not self.states[k].get("unavailable")]

    def _pick_recommended(self):
        for key in self._missing_recommended():
            self.set_selected(key, True)

    def _clear(self):
        for key in list(self.selected):
            self.set_selected(key, False)

    def _update_action_bar(self):
        _ = self._
        to_install = [k for k in self.selected if not self._installed(k)]
        to_remove = [k for k in self.selected if self._installed(k)]
        self._set_label(self.install_btn, _("sw_install_n").format(n=len(to_install)) if to_install
                        else _("install_btn"))
        self.install_btn.set_visible(bool(to_install))
        self.install_btn.set_sensitive(not self.busy)
        self._set_label(self.remove_btn, _("sw_remove_n").format(n=len(to_remove)))
        self.remove_btn.set_visible(bool(to_remove))
        self.remove_btn.set_sensitive(not self.busy)
        self.clear_btn.set_visible(bool(self.selected))
        to_update = [k for k in to_remove if k in self.updates]
        self.update_btn.set_visible(bool(to_update))
        self.update_btn.set_sensitive(not self.busy)
        self.update_all_btn.set_sensitive(not self.busy)
        if to_update:
            self._set_label(self.update_btn, self._update_label(to_update))
        if self.selected:
            names = sorted((self._pick_name(k) for k in self.selected), key=str.lower)
            shown = ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")
            self.picked.set_text(_("sw_picked").format(n=len(names), names=shown))
        self.action_revealer.set_reveal_child(bool(self.selected))
        self._update_heroes()

    def _pick_name(self, key: str) -> str:
        """What the bar calls a pick - " · Flatpak" where only that comes."""
        name = self.items[key].name
        flatpak_only = self._has_choice(key) and self.source_of(key) == "flatpak"
        part = self._part(key)
        if part and self._here(key) and not self._here(part):
            flatpak_only = True
        return name + (" · Flatpak" if flatpak_only else "")

    def _update_heroes(self):
        """The cards above the list and the shelves' Pick missing - each
        only while it has something to do."""
        _ = self._
        searching = bool(self.search.get_text().strip())
        missing = self._missing_recommended()
        unpicked = [k for k in missing if k not in self.selected]
        self.rec_card.set_visible(bool(missing) and not searching)
        if missing:
            names = ", ".join(self.items[k].name for k in missing)
            self.rec_sub.set_text(_("sw_recommended_missing").format(n=len(missing), names=names))
            self._set_label(self.rec_btn, _("sw_recommended_pick_n").format(n=len(unpicked)))
        self.rec_btn.set_visible(bool(unpicked))
        self.update_card.set_visible(bool(self.updates) and not searching)
        for section in self.sections.values():
            section.sw_pick.set_visible(any(
                c.get_visible() and c.get_child().item.key in self.states
                and not self._installed(c.get_child().item.key) and c.get_child().item.key not in self.selected
                for c in section.sw_flow.get_children()))

    # --- state --------------------------------------------------------------------------

    def _refresh_tiles(self):
        for tiles in self.tiles.values():
            for tile in tiles:
                tile.refresh()
        self._refresh_card()
        self._update_counts()
        self._apply_filter()
        self._update_action_bar()

    def _update_counts(self):
        _ = self._
        total = installed = 0
        for cat in TILE_CATEGORIES:
            items = self._in(cat)
            have = sum(1 for i in items if self._installed(i.key))
            updates = sum(1 for i in items if i.key in self.updates)
            known = all(i.key in self.states for i in items)
            label = f"{have}/{len(items)}" if known else ""
            self._set_badge(cat, label)
            # a shelf with updates: a dot on its icon, as on a notification
            self.nav_icons[cat].set_dot(bool(updates))
            if cat in self.cat_tiles:
                text = _("sw_cat_count").format(have=have, n=len(items)) if known else _("task_native_checking")
                if updates:
                    text += " · " + _("sw_cat_updates").format(n=updates)
                self.cat_tiles[cat].set_text(text)
            total += len(items)
            installed += have
        self.summary.set_text(_("sw_summary").format(n=installed, total=total))
        # its icon says what they are: just how many, in the accent
        updates_badge = self._set_badge("updates", str(len(self.updates)) if self.updates else "")
        updates_badge.get_style_context().add_class("sw-nav-accent")
        warnings = sum(1 for c in self.checks if c.status == inspection.WARN)
        badge = self._set_badge("inspection", f"⚠ {warnings}" if warnings else ("✓" if self.checks else ""))
        bctx = badge.get_style_context()
        (bctx.add_class if warnings else bctx.remove_class)("sw-nav-warn")

    def _set_badge(self, cat: str, text: str) -> Gtk.Label:
        badge = self.nav_badges[cat]
        badge.set_text(text)
        badge.set_visible(bool(text))
        return badge

    def _refresh(self):
        """Asks what's installed (the package queries take a moment),
        then whether updates wait; runs the Ship inspection alongside."""
        self.states.clear()
        self.updates = {}
        self._refresh_tiles()
        self._run_inspection()

        def work():
            states = {}
            for item in self.items.values():
                if item.layer is not None:
                    continue
                try:
                    st = launcher_tasks.launcher_state(item.ident)
                except Exception:
                    logger.exception(f"Checking {item.name}")
                    st = launcher_tasks.LauncherState(installed=False)
                locked = (self._("settings_bundle_lock").format(bundle=", ".join(st.locked_by), name=item.name)
                          if st.locked_by else None)
                states[item.key] = {"installed": st.installed, "partial": st.partial, "source": st.source_label,
                                    "method": st.method, "locked": locked, "package": st.package,
                                    "flatpak_id": st.flatpak_id}
            # What can't be had here isn't offered: not in the repositories
            # (gamescope on Ubuntu 24.04 / Mint 22), or a read-only system
            # (GameMode, LACT on Bazzite) - its tile says so, and can't be picked
            # On a read-only system every tile without a Flatpak is looked
            # at, the rest only for the Recommended kit (a package query each).
            look = list(self.items) if immutable_system() else RECOMMENDED
            for key in look:
                st = states.get(key)
                if st is None or st["installed"] or key not in self.items or self.items[key].layer is not None:
                    continue
                if launcher_tasks._flatpak_ids(self.items[key].ident):
                    continue        # the Flatpak can still be had
                try:
                    plan = launcher_tasks.native_plan(self.items[key].ident)
                except Exception:
                    logger.exception(f"Looking up {key}")
                    continue
                if (isinstance(plan, launcher_tasks.Unavailable)
                        and plan.reason.startswith(("task_native_not_in_repos", "task_native_not_in_components",
                                                    "task_native_immutable"))):
                    st["unavailable"] = True
            try:
                launchers = flatpak_layers.flatpak_launchers()
                have = flatpak_layers.installed_refs() if launchers else set()
            except Exception:
                logger.exception("Checking Flatpak launchers")
                launchers, have = [], set()
            GLib.idle_add(self._apply, states, launchers, have)
            family = detect_linux_distro_family()
            packages = {k: s["package"] for k, s in states.items() if s["installed"] and s["package"]}
            flatpaks = {k: s["flatpak_id"] for k, s in states.items() if s["installed"] and s["flatpak_id"]}
            try:
                updates, system = pending_updates(family, packages, flatpaks)
            except Exception:
                logger.exception("Checking for updates")
                updates, system = {}, {}
            GLib.idle_add(self._apply_updates, updates, system)
        threading.Thread(target=work, daemon=True).start()

    def _apply(self, states, launchers, have) -> bool:
        if self.closed:
            return False
        self.states.update(states)
        self.flatpak_launchers = launchers
        if launchers:
            names = ", ".join(sorted({name for name, _app, _branch in launchers}))
            self.flatpak_names = names
            for layer in flatpak_layers.LAYERS:
                key = f"flatpak:{layer.id}"
                needed = flatpak_layers.needed_refs(layer, launchers)
                got = [r for r in needed if r in have]
                self.layer_refs[key], self.layer_got[key] = needed, got
                self.states[key] = {"installed": bool(got), "partial": bool(got) and len(got) < len(needed),
                                    "source": "Flathub" if got else "", "method": "flatpak", "locked": None}
        else:
            for layer in flatpak_layers.LAYERS:
                self.selected.discard(f"flatpak:{layer.id}")
        # what was picked for installing and now is installed is done
        for key in list(self.selected):
            if key in self.install_picks and self._installed(key):
                self.selected.discard(key)
        # the tool asked for from Launch options turned out to be there
        if self.focus:
            if self._installed(self.focus) and self.focus not in self.install_picks:
                self.set_selected(self.focus, False)
            self.focus = None
        self._refresh_tiles()
        self.app._set_shipwright_attention(recommended=[self.items[k].name for k in self._missing_recommended()])
        return False

    def _apply_updates(self, updates, system) -> bool:
        if self.closed:
            return False
        _ = self._
        self.updates = dict(updates)
        self.system_updates = dict(system)
        for child in self.updates_flow.get_children():
            tile = child.get_child()
            self.tiles[tile.item.key].remove(tile)
            child.destroy()
        for key in sorted(self.updates, key=lambda k: self.items[k].name.lower() if k in self.items else k):
            if key in self.items:
                self._add_tile(self.updates_flow, self.items[key])
        self.updates_flow.show_all()
        n = len(self.updates)
        self.nav_rows["updates"].set_visible(bool(n))
        self.chips["updates"].set_visible(bool(n))
        if not n and self.filter == "updates":
            self.chips["all"].set_active(True)
        if n:
            names = sorted(self.items[k].name for k in self.updates if k in self.items)
            self.update_card_title.set_text(_("sw_updates_waiting").format(n=n))
            self.update_card_sub.set_text(", ".join(names[:5]) + ("…" if len(names) > 5 else ""))
            self._set_label(self.update_card_btn, self._update_label(list(self.updates)))
            self._set_label(self.update_all_btn, self._update_all_label())
            self.updates_note.set_text(_("sw_updates_note_full" if self.full_only else "sw_updates_note")
                                       .format(n=len(self.system_updates)))
        elif self.page == "updates":
            self.goto("overview")
        self._refresh_tiles()
        self.app._set_shipwright_attention(updates=n)
        return False

    def _update_all_label(self) -> str:
        """The Updates page's button: "Update all (n)" - or "System update"
        where only the whole system can be updated (and that's what it does)."""
        if self.full_only and any(not u.flatpak for u in self.updates.values()):
            return self._("sw_system_update")
        return self._("sw_update_all_n").format(n=len(self.updates))

    def _update_label(self, keys) -> str:
        """"Update (n)" - or "System update" where only the whole system
        can be (and some of keys are system packages)."""
        native = [k for k in (keys or []) if k in self.updates and not self.updates[k].flatpak]
        if self.full_only and (native or keys is None):
            return self._("sw_system_update")
        return self._("sw_update_n").format(n=len(keys)) if keys else self._("sw_update")

    def _reveal_focus(self):
        """Opened on one item, straight away - not waiting for the package
        queries: its tile scrolled into view - and picked, for a tool
        asked for from Launch options (a launcher, from its game menu, is
        there already)."""
        if self.focus in self.nav_rows and self.focus not in TILE_CATEGORIES:
            # a page of its own (the Proton page, from Launch options)
            self.goto(self.focus)
            self.focus = None
            return
        self.focus = TOOL_OF_PART.get(self.focus, self.focus)
        item = self.items.get(self.focus)
        if item is None:
            self.focus = None
            return
        if item.launcher:
            self.focus = None
        self.goto("overview")
        if not item.launcher:
            self.set_selected(item.key, True)
        tile = next((t for t in self.tiles.get(item.key, []) if t.is_ancestor(self.overview_box)), None)
        if tile is not None:
            handler = []

            def allocated(*_):
                tile.disconnect(handler[0])
                GLib.idle_add(self._scroll_to, tile)
            handler.append(tile.connect("size-allocate", allocated))

    def _scroll_to(self, widget: Gtk.Widget) -> bool:
        """Glides the list to widget, in the middle of the view."""
        coords = widget.translate_coordinates(self.overview_box, 0, 0)
        if coords is not None:
            adj = self.overview_scroller.get_vadjustment()
            h = widget.get_allocated_height()
            top = coords[1] - (adj.get_page_size() - h) / 2
            self._animate_scroll(adj, max(adj.get_lower(), min(top, adj.get_upper() - adj.get_page_size())))
        return False

    # --- Ship inspection ------------------------------------------------------------------

    def _run_inspection(self):
        card = self.inspect_card
        for child in card.get_children():
            child.destroy()
        waiting = Gtk.Label(label=self._("insp_running"), xalign=0)
        waiting.get_style_context().add_class("settings-row-sub")
        waiting.get_style_context().add_class("settings-row")
        card.pack_start(waiting, False, False, 0)
        card.show_all()
        self.inspect_again.set_sensitive(False)
        self.fix_all.set_sensitive(False)

        def work():
            try:
                checks = inspection.run_checks()
            except Exception:
                logger.exception("Ship inspection")
                checks = []
            GLib.idle_add(self._show_inspection, checks)
        threading.Thread(target=work, daemon=True).start()

    def _fixable(self) -> List:
        """What "Fix all" runs - multilib first: the 32-bit fixes it
        unblocks come after it in the same batch."""
        fixes = [c for c in self.checks if c.fix is not None and c.status != inspection.OK and not c.blocked_key]
        if any(c.id == "multilib" for c in fixes):
            fixes += [c for c in self.checks if c.blocked_key == "insp_needs_multilib" and c.fix is not None]
        return sorted(fixes, key=lambda c: c.id != "multilib")

    def _show_inspection(self, checks) -> bool:
        if self.closed:
            return False
        _ = self._
        self.checks = checks
        card = self.inspect_card
        for child in card.get_children():
            child.destroy()
        passed = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        n_passed = 0
        for check in sorted(checks, key=lambda c: {inspection.WARN: 0, inspection.INFO: 1}.get(c.status, 2)):
            row = self._check_row(check)
            if check.status == inspection.OK:
                self._add_row(passed, row)
                n_passed += 1
            else:
                self._add_row(card, row)
        if n_passed:
            revealer = Gtk.Revealer()
            revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
            revealer.add(passed)
            toggle = Gtk.Button()
            toggle.set_relief(Gtk.ReliefStyle.NONE)
            toggle.get_style_context().add_class("settings-row")
            toggle.get_style_context().add_class("insp-passed")
            inner = Gtk.Box(spacing=10)
            inner.pack_start(self._drawn_icon("emblem-ok-symbolic", 16), False, False, 0)
            label = Gtk.Label(label=_("insp_passed").format(n=n_passed), xalign=0)
            label.get_style_context().add_class("settings-status-on")
            inner.pack_start(label, True, True, 0)
            chevron = Gtk.Label(label="▾")
            inner.pack_end(chevron, False, False, 0)
            toggle.add(inner)

            def flip(_b):
                shown = not revealer.get_reveal_child()
                revealer.set_reveal_child(shown)
                chevron.set_text("▴" if shown else "▾")
            toggle.connect("clicked", flip)
            self._add_row(card, toggle)
            card.pack_start(revealer, False, False, 0)
        card.show_all()
        self.inspect_again.set_sensitive(True)
        fixable = self._fixable()
        self.fix_all.set_visible(bool(fixable))
        self.fix_all.set_sensitive(not self.busy)
        self._set_label(self.fix_all, _("sw_fix_all_n").format(n=len(fixable)) if fixable else _("sw_fix_all"))
        warns = [c for c in checks if c.status == inspection.WARN]
        warnings = len(warns)
        # the strip names the problem - one by itself, several by their titles
        if warnings == 1:
            self.strip_title.set_text(_(warns[0].title_key).format(**warns[0].args))
            self.score_text.set_text(_(warns[0].text_key).format(**warns[0].args))
        elif warnings:
            self.strip_title.set_text(_("insp_summary_warn").format(n=warnings))
            self.score_text.set_text(" · ".join(_(c.title_key).format(**c.args) for c in warns))
        else:
            self.strip_title.set_text(_("insp_title"))
            self.score_text.set_text(_("insp_passed").format(n=n_passed))
        self.strip_fix.set_visible(bool(warnings and fixable))
        self.strip_fix.set_sensitive(not self.busy)
        self._set_label(self.strip_fix, _("insp_fix") if len(fixable) == 1 else _("sw_fix_all_n").format(n=len(fixable)))
        self.score_ring.queue_draw()
        ctx = self.strip.get_style_context()
        (ctx.add_class if warnings else ctx.remove_class)("sw-strip-warn")
        self._update_counts()
        self.app._set_shipwright_attention(warnings=warnings)
        return False

    @staticmethod
    def _add_row(card: Gtk.Box, widget: Gtk.Widget):
        if card.get_children():
            sep = Gtk.Box()
            sep.get_style_context().add_class("settings-row-sep")
            sep.set_margin_start(16)
            sep.set_margin_end(16)
            card.pack_start(sep, False, False, 0)
        card.pack_start(widget, False, False, 0)

    def _check_row(self, check) -> Gtk.Widget:
        _ = self._
        row = Gtk.Box(spacing=14)
        row.get_style_context().add_class("settings-row")
        tile = _icon_tile(STATUS_ICONS[check.status], 42, AMBER if check.status == inspection.WARN else None)
        if check.status == inspection.INFO:
            tile.set_opacity(0.6)
        row.pack_start(tile, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        texts.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=_(check.title_key).format(**check.args), xalign=0)
        title.set_line_wrap(True)       # a long card name mustn't widen the window
        title.set_max_width_chars(60)
        title.get_style_context().add_class("settings-row-title")
        texts.pack_start(title, False, False, 0)
        text = Gtk.Label(label=_(check.text_key).format(**check.args), xalign=0)
        text.set_line_wrap(True)
        text.set_max_width_chars(60)
        text.get_style_context().add_class("settings-row-sub")
        texts.pack_start(text, False, False, 0)
        if check.blocked_key:
            blocked = Gtk.Label(label=_(check.blocked_key), xalign=0)
            blocked.set_line_wrap(True)
            blocked.set_max_width_chars(60)
            blocked.get_style_context().add_class("settings-status-off")
            texts.pack_start(blocked, False, False, 0)
        row.pack_start(texts, True, True, 0)
        if check.fix is not None and check.status != inspection.OK:
            fix = self.app._dialog_button(_("insp_fix"), "accent")
            fix.set_valign(Gtk.Align.CENTER)
            fix.set_sensitive(check.blocked_key is None)
            fix.connect("clicked", lambda _b, c=check: self._run_fixes([c]))
            row.pack_end(fix, False, False, 0)
        return row

    # --- doing it -------------------------------------------------------------------------

    def _batch_item(self, key: str, action: str) -> launcher_tasks.BatchItem:
        item = self.items[key]
        if item.layer is not None:
            refs = ([r for r in self.layer_refs.get(key, []) if r not in self.layer_got.get(key, [])]
                    if action == "install" else list(self.layer_got.get(key, [])))
            return launcher_tasks.BatchItem(key, f"{item.name} · Flatpak", action, layer=item.layer, refs=refs)
        if action == "install" and self._offers_choice(key):
            source = self.source_of(key)
            return launcher_tasks.BatchItem(key, item.name + (" · Flatpak" if source == "flatpak" else ""), action,
                                            source=source, logo=item.ident)
        return launcher_tasks.BatchItem(key, item.name, action, logo=item.ident)

    def _start(self, batch: List[launcher_tasks.BatchItem]):
        if self.busy or not batch:
            return
        self.busy = True
        self._update_action_bar()
        self._refresh_card()

        def finished():
            self.busy = False
            if not self.closed:
                self._refresh()
        launcher_tasks.start_batch(self.app, self.dialog, batch, finished)

    def _run_selected(self, action: str):
        self.run_keys(list(self.selected), action)

    def run_keys(self, keys: List[str], action: str):
        """Installs (or removes) keys - the picked ones, or one from its card."""
        keys = sorted((k for k in keys if self._installed(k) == (action == "remove")),
                      key=lambda k: (TILE_CATEGORIES.index(self.items[k].category), self.items[k].name.lower()))
        if not keys:
            return
        if action == "install":
            self.install_picks = set(keys)
        else:
            # what's being removed isn't picked any more afterwards
            for key in keys:
                self.selected.discard(key)
        # a tool and its Flatpak part: whichever of the two is (or isn't) there
        batch = []
        for key in keys:
            for k in (key, self._part(key)):
                if k is not None and self._here(k) == (action == "remove"):
                    batch.append(self._batch_item(k, action))
        self._start(batch)

    def _run_updates(self, keys: List[str]):
        """Updates keys: Flatpaks one by one; system packages one by one
        too where apt / dnf / zypper on Leap can - on Arch and Tumbleweed
        as one update of the whole system, after asking."""
        _ = self._
        keys = [k for k in keys if k in self.updates]
        if not keys or self.busy:
            return
        flat = [k for k in keys if self.updates[k].flatpak]
        native = [k for k in keys if not self.updates[k].flatpak]
        batch = []
        if native and self.full_only:
            others = len(self.system_updates)
            names = ", ".join(sorted(self.items[k].name for k in native))
            text = _("sw_full_update_text").format(names=names, n=others)
            if snapshots_on():
                text += "\n\n" + _("sw_full_update_snapper")
            response = self.app._card_dialog(_("sw_system_update"), text,
                                             [(_("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost"),
                                              (_("sw_system_update"), Gtk.ResponseType.OK, "accent")],
                                             icon_name="view-refresh-symbolic", transient_for=self.dialog)
            self._drop_focus()
            if response != Gtk.ResponseType.OK:
                return
            aur = [self.updates[k].name for k in native if self.updates[k].aur]
            batch.append(launcher_tasks.BatchItem("update:system", _("sw_system_update_item").format(n=others),
                                                  "update", full=True, aur=aur))
        else:
            for key in native:
                update = self.updates[key]
                # its 32-bit half goes along when that has an update too
                extras = [p for p in launcher_tasks._extras(self.family, self.items[key].ident)
                          if p.split(":")[0].split(".")[0] in self.system_updates]
                batch.append(launcher_tasks.BatchItem(f"update:{key}", self.items[key].name, "update",
                                                      packages=[update.name] + extras))
        for key in flat:
            batch.append(launcher_tasks.BatchItem(f"update:{key}", f"{self.items[key].name} · Flatpak", "update",
                                                  flatpak_ids=[self.updates[key].name]))
        for key in keys:
            self.selected.discard(key)
        self._start(batch)

    def _run_fixes(self, checks):
        _ = self._
        self._start([launcher_tasks.BatchItem(f"fix:{c.id}", _(c.title_key).format(**c.args), "fix", fix=c.fix)
                     for c in checks])

    def _open(self, item: Optional[Item]):
        argv = launcher_tasks.launch_argv(item.ident) if item and item.layer is None else None
        if not argv:
            return
        try:
            subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            logger.warning(f"Couldn't start {argv}: {e}")

    # --- keyboard and controller ----------------------------------------------------------

    def _on_key(self, _w, event) -> bool:
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        if event.keyval == Gdk.KEY_Escape:
            if self.search.get_text():
                self.search.set_text("")
            else:
                self.close()
            return True
        if event.keyval == Gdk.KEY_space and self._toggle_focused():
            return True
        arrows = {Gdk.KEY_Up: "up", Gdk.KEY_Down: "down", Gdk.KEY_Left: "left", Gdk.KEY_Right: "right"}
        if event.keyval in arrows and not isinstance(self.dialog.get_focus(), Gtk.Entry) and not ctrl:
            self._move(arrows[event.keyval])
            return True
        if ctrl and event.keyval in (Gdk.KEY_a, Gdk.KEY_A) and not self.search.has_focus():
            self._select_all()
            return True
        if ctrl and event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._run_selected("install")
            return True
        if ctrl and event.keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search.grab_focus()
            return True
        return False

    def _nav(self, action: str) -> bool:
        """The controller (and the arrow keys): the d-pad moves inside the
        page showing - Left from its edge onto the sidebar, where Up /
        Down go from page to page and Right goes back in; A opens a
        tile's card (or presses what's stood on), X / Y tick a tile, the
        shoulder buttons switch page, Start installs, B closes."""
        if action in ("up", "down", "left", "right"):
            self._move(action)
        elif action == "activate":
            focus = self.dialog.get_focus()
            if focus is None:
                self._move("down")
            else:
                focus.activate()
                # what was pressed may be gone now (Pick missing): onto the page again
                GLib.idle_add(self._refocus)
        elif action == "back":
            self.close()
        elif action in ("prev_tab", "next_tab"):
            self._step_page(1 if action == "next_tab" else -1)
        elif action == "settings":
            self._run_selected("install")
        elif action in ("favorite", "sort"):
            self._toggle_focused()
        return True

    def _page_widget(self) -> Gtk.Widget:
        return self.stack.get_visible_child()

    def _step_page(self, step: int):
        visible = [c for c in self.nav_rows if self.nav_rows[c].get_visible()]
        row = self.nav.get_selected_row()
        current = row.page if row is not None else self.page
        i = visible.index(current) if current in visible else 0
        self.goto(visible[(i + step) % len(visible)])
        self.nav_rows[visible[(i + step) % len(visible)]].grab_focus()

    def _move(self, action: str):
        keynav.on(self.dialog)
        if keynav.restore(self.dialog):
            return
        direction = {"up": Gtk.DirectionType.UP, "down": Gtk.DirectionType.DOWN,
                     "left": Gtk.DirectionType.LEFT, "right": Gtk.DirectionType.RIGHT}[action]
        focus = self.dialog.get_focus()
        page = self._page_widget()
        if focus is not None and focus.is_ancestor(self.nav):
            if action in ("up", "down"):
                self._step_page(1 if action == "down" else -1)
            elif action == "right":
                self._enter_page(focus.page)
            return
        if focus is None or not focus.is_ancestor(page):
            # nothing stood on yet (or the search field, the header): on
            # the list its first tile, else the page's first control -
            # never the search, a pad can't type
            self._enter_page("overview" if self.page == "overview" else self.page)
            return
        if self.page == "overview" and self._overview_move(focus, action):
            return
        if not page.child_focus(direction) and action == "left":
            self._to_sidebar()

    def _to_sidebar(self):
        row = self.nav.get_selected_row()
        if row is not None:
            row.grab_focus()

    def _enter_page(self, cat: Optional[str]):
        """Into the page from the sidebar (or from nowhere): a shelf's - or
        the list's - first tile; another page's first control."""
        if self.page == "overview":
            rows = self._overview_rows()
            for widgets, where in rows:
                if where == "tiles" and (cat not in TILE_CATEGORIES or widgets[0].sw_cat == cat):
                    widgets[0].grab_focus()
                    return
        self._page_widget().child_focus(Gtk.DirectionType.TAB_FORWARD)

    def _overview_rows(self):
        """The list's stops, a row at a time from the top: the inspection's
        line, the cards waiting, the filters, then each shelf's tiles
        (its Pick missing at the end of its first row, where it sits
        above them)."""
        def usable(w):
            return w.get_visible() and w.get_mapped() and w.is_sensitive() and w.get_can_focus()
        rows = []
        for group in ([self.strip_fix, self.strip_look], [self.rec_btn], [self.update_card_btn],
                      list(self.chips.values())):
            widgets = [w for w in group if usable(w)]
            if widgets:
                rows.append((widgets, "top"))
        for cat in TILE_CATEGORIES:
            section = self.sections.get(cat)
            if section is None or not section.get_visible():
                continue
            by_y: Dict[int, List[Gtk.Widget]] = {}
            for child in section.sw_flow.get_children():
                if not child.get_visible() or not child.get_child_visible():
                    continue
                tile = child.get_child()
                if not usable(tile.button):
                    continue
                tile.button.sw_cat = cat
                by_y.setdefault(child.get_allocation().y, []).append(tile.button)
            for i, y in enumerate(sorted(by_y)):
                widgets = sorted(by_y[y], key=lambda b: b.get_parent().get_parent().get_allocation().x)
                if i == 0 and usable(section.sw_pick):
                    section.sw_pick.sw_cat = cat
                    widgets = widgets + [section.sw_pick]
                rows.append((widgets, "tiles"))
        return rows

    def _overview_move(self, focus: Gtk.Widget, action: str) -> bool:
        rows = self._overview_rows()
        at = next(((r, row[0].index(focus)) for r, row in enumerate(rows) if focus in row[0]), None)
        if at is None:
            return False
        r, c = at
        widgets = rows[r][0]
        if action in ("left", "right"):
            j = c + (1 if action == "right" else -1)
            if 0 <= j < len(widgets):
                widgets[j].grab_focus()
            elif action == "left":
                self._to_sidebar()
            return True
        nr = r + (1 if action == "down" else -1)
        if not 0 <= nr < len(rows):
            return True

        def centre(w):
            xy = w.translate_coordinates(self.overview_box, w.get_allocated_width() // 2, 0)
            return xy[-2] if xy else 0
        x = centre(focus)
        min(rows[nr][0], key=lambda w: abs(centre(w) - x)).grab_focus()
        return True

    def _refocus(self) -> bool:
        if not self.closed and self.card is None and self.dialog.get_focus() is None:
            self._page_widget().child_focus(Gtk.DirectionType.TAB_FORWARD)
        return False

    def _keep_in_view(self, widget: Optional[Gtk.Widget]) -> bool:
        """The page scrolls to what the keyboard or controller stands on -
        and the sidebar marks its shelf."""
        if widget is None or self.closed:
            return False
        self._mark_stood_shelf()
        scroller = widget.get_ancestor(Gtk.ScrolledWindow)
        if scroller is None:
            return False
        content = scroller.get_child()
        if isinstance(content, Gtk.Viewport):
            content = content.get_child()
        at = widget.translate_coordinates(content, 0, 0) if content is not None else None
        if not at:
            return False
        adj = scroller.get_vadjustment()
        top, h, view, margin = at[-1], widget.get_allocated_height(), adj.get_page_size(), 24
        if top - margin < adj.get_value():
            adj.set_value(max(adj.get_lower(), top - margin))
        elif top + h + margin > adj.get_value() + view:
            adj.set_value(min(adj.get_upper() - view, top + h + margin - view))
        return False

    def _toggle_focused(self) -> bool:
        """Space / the controller's pick button: the tick of the tile
        stood on."""
        focus = self.dialog.get_focus()
        tile = focus.get_parent() if focus is not None else None
        if isinstance(tile, Tile) and tile.tick.get_sensitive():
            tile.toggle()
            return True
        return False

    def close(self):
        if self.closed or self.busy:
            return
        self.closed = True
        # Destroyed by itself: held here, the dialog's destroy only takes
        # it out - and it kept listening to the Proton jobs (a failure
        # was told once by every Shipwright opened before).
        self.proton_page.destroy()
        self.dialog.destroy()
        self.app._exit_dimmed_dialog()
        if self.on_closed is not None:
            self.on_closed()


def show(app, parent: Optional[Gtk.Window] = None, focus: Optional[str] = None,
         on_closed: Optional[Callable[[], None]] = None) -> Shipwright:
    return Shipwright(app, parent or app._dialog_transient_parent(), focus, on_closed)
