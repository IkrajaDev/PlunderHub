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

"""A game's achievements (core/achievements.py).

On top, over the game's hero picture: a ring with the share unlocked and
three highlights (the latest, the rarest one held, how many rare ones).
Under it the unlocked ones (newest first) or the locked ones as cards,
each with its icon, how rare it is - a stripe and a bar in the rarity's
colour - and when it came. Sorted by newest, rarest, most common or name.
A hidden one's description stays covered until clicked.

All cards are made once; switching the tab or the order only filters and
sorts them. Icons come in the background, the ones in sight first. The
wheel moves half a card a notch (GTK's own step, a whole one, felt too
quick here)."""

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

from plunderhub.core import achievements as ach
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango
from plunderhub.logging_setup import logger
from plunderhub.perf_guard import allow_hover_relayout
from plunderhub.ui.home_mixin import _accent_rgb

ICON_PX = 56
WHEEL_STEP = 38          # px a wheel notch moves the list
# (below %, key, colour; None = the accent)
RARITY = ((5.0, "ach_rarity_ultra", "#e8b93d"), (10.0, "ach_rarity_very", "#c08af0"),
          (25.0, "ach_rarity_rare", "#5aa9e6"), (50.0, "ach_rarity_uncommon", None),
          (101.0, "ach_rarity_common", "#8a9a97"))
SORTS = ("newest", "rarest", "common", "name")


def rarity(percent: Optional[float]):
    """(key, colour - None for the accent) of a percent; (None, None) unknown."""
    if percent is None:
        return None, None
    for limit, key, colour in RARITY:
        if percent < limit:
            return key, colour
    return RARITY[-1][1], RARITY[-1][2]


def _rgb(hex_colour: str):
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


class Ring(Gtk.DrawingArea):
    """The share unlocked: a ring in the accent, the percent inside."""

    def __init__(self, px: int = 96):
        super().__init__()
        self.px = px
        self.frac = 0.0
        self.set_size_request(px, px)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def set_fraction(self, frac: float):
        self.frac = max(0.0, min(1.0, frac))
        self.queue_draw()

    def _draw(self, widget, cr):
        from gi.repository import PangoCairo
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        r, g, b = _accent_rgb(widget)
        cx, cy, rad, width = w / 2, h / 2, min(w, h) / 2 - 6, 8
        cr.set_line_width(width)
        cr.set_line_cap(1)
        cr.set_source_rgba(r, g, b, 0.16)
        cr.arc(cx, cy, rad, 0, 2 * math.pi)
        cr.stroke()
        if self.frac > 0:
            cr.set_source_rgb(r, g, b)
            cr.arc(cx, cy, rad, -math.pi / 2, -math.pi / 2 + 2 * math.pi * self.frac)
            cr.stroke()
        ctx = widget.get_style_context()
        fg = ctx.get_color(ctx.get_state())
        layout = PangoCairo.create_layout(cr)
        font = ctx.get_font(ctx.get_state()).copy()
        # sized to the ring, not the text: "100%" fits inside a small one too
        font.set_absolute_size(min(w, h) * 0.24 * Pango.SCALE)
        font.set_weight(Pango.Weight.HEAVY)
        layout.set_font_description(font)
        layout.set_text(f"{round(100 * self.frac)}%", -1)
        tw, th = (v / Pango.SCALE for v in layout.get_size())
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 1)
        cr.move_to(cx - tw / 2, cy - th / 2)
        PangoCairo.show_layout(cr, layout)
        return False


class RarityBar(Gtk.DrawingArea):
    """How many players have it, as a thin bar in the rarity's colour."""

    def __init__(self, percent: Optional[float], colour: Optional[str]):
        super().__init__()
        self.percent, self.colour = percent, colour
        self.set_size_request(96, 5)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def _draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        r, g, b = _rgb(self.colour) if self.colour else _accent_rgb(widget)

        def pill(width):
            rad = h / 2
            cr.new_sub_path()
            cr.arc(width - rad, rad, rad, -math.pi / 2, math.pi / 2)
            cr.arc(rad, rad, rad, math.pi / 2, 3 * math.pi / 2)
            cr.close_path()
        cr.set_source_rgba(r, g, b, 0.18)
        pill(w)
        cr.fill()
        if self.percent:
            cr.set_source_rgb(r, g, b)
            pill(max(h, w * min(100.0, self.percent) / 100))
            cr.fill()
        return False


class Stripe(Gtk.DrawingArea):
    """The rarity's colour down a card's left edge."""

    def __init__(self, colour: Optional[str]):
        super().__init__()
        self.colour = colour
        self.set_size_request(4, -1)
        self.connect("draw", self._draw)

    def _draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        r, g, b = _rgb(self.colour) if self.colour else _accent_rgb(widget)
        rad = w / 2
        cr.new_sub_path()
        cr.arc(rad, rad, rad, math.pi, 0)
        cr.arc(rad, h - rad, rad, 0, math.pi)
        cr.close_path()
        cr.set_source_rgb(r, g, b)
        cr.fill()
        return False


class _Bendable(Gtk.Box):
    """A row of two that asks only for the width of the wider one: when
    the window is narrower than both side by side, _fit stacks them."""

    def do_get_preferred_width(self):
        kids = [c for c in self.get_children() if c.get_visible()]
        if not kids:
            return 0, 0
        _minimum, natural = Gtk.Box.do_get_preferred_width(self)
        minimum = max(c.get_preferred_width()[0] for c in kids)
        return minimum, max(minimum, natural)


class AchievementsWindow:
    def __init__(self, app, parent: Optional[Gtk.Window], uid: str, title: str):
        self.app, self.uid, self.title = app, uid, title
        # where its icons are kept (the data's own key once read)
        self.appid = uid.split(":", 1)[1] if uid.startswith("steam:") else uid.rsplit(":", 1)[1]
        self._ = app._
        self.data: Optional[ach.GameAchievements] = None
        self.tab = "unlocked"
        self.sort = "newest"
        self.rows: List[Gtk.ListBoxRow] = []
        self._icons_asked: set = set()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="plunderhub-ach-icon")
        self._icon_timer = 0

        self.dialog = dlg = Gtk.Dialog(transient_for=parent, modal=True)
        dlg.set_title(self._("ach_title"))
        dlg.set_decorated(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.get_style_context().add_class("home-launch-dialog")
        area = dlg.get_action_area()
        if area is not None:
            area.set_no_show_all(True)
            area.hide()
        dlg.get_content_area().set_border_width(0)
        dlg.set_default_size(*self._size(parent))
        dlg.connect("key-press-event", self._on_key)
        dlg.connect("response", lambda *_: dlg.destroy())
        dlg.connect("destroy", lambda *_: self._pool.shutdown(wait=False, cancel_futures=True))

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_start(22)
        body.set_margin_end(22)
        body.set_margin_top(20)
        body.set_margin_bottom(18)
        dlg.get_content_area().pack_start(body, True, True, 0)
        body.pack_start(self._top(), False, False, 0)
        body.pack_start(self._controls(), False, False, 0)
        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.loading = Gtk.Label(label=self._("ach_loading"))
        self.loading.get_style_context().add_class("gp-card-sub")
        self.stack.add_named(self.loading, "loading")
        self.stack.add_named(self._list(), "list")
        self.empty = Gtk.Label()
        self.empty.set_line_wrap(True)
        self.empty.set_justify(Gtk.Justification.CENTER)
        self.empty.get_style_context().add_class("gp-card-sub")
        self.stack.add_named(self.empty, "empty")
        body.pack_start(self.stack, True, True, 0)
        dlg.show_all()
        self.highlights.set_no_show_all(True)
        self.stack.set_visible_child_name("loading")
        # the room it has: a small screen, a big scale (see _fit)
        self._fit_state = None
        self._fit_queued = False
        dlg.connect("size-allocate", lambda *_: self._fit_soon())
        threading.Thread(target=self._load, name="plunderhub-achievements", daemon=True).start()

    @staticmethod
    def _size(parent):
        width, height = 920, 860
        try:
            display = Gdk.Display.get_default()
            gdk_window = parent.get_window() if parent is not None else None
            monitor = (display.get_monitor_at_window(gdk_window) if gdk_window is not None else None) \
                or display.get_primary_monitor() or display.get_monitor(0)
            work = monitor.get_workarea()
            width = min(width, int(work.width * 0.92))
            height = min(height, int(work.height * 0.88))
        except Exception:
            pass
        return max(width, 480), max(height, 360)

    # --- the top: the ring and the highlights, over the hero -----------------------------

    def _top(self) -> Gtk.Widget:
        from plunderhub.ui.loading_dock import HeroBack
        card = Gtk.Box()
        card.get_style_context().add_class("gp-card")
        card.get_style_context().add_class("ld-card")
        self.hero = HeroBack()
        card.pack_start(self.hero, True, True, 0)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        outer.set_margin_start(20)
        outer.set_margin_end(14)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        self.hero.pack_start(outer, True, True, 0)
        content = Gtk.Box(spacing=20)
        outer.pack_start(content, False, False, 0)
        self.ring = Ring(100)
        content.pack_start(self.ring, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        texts.set_valign(Gtk.Align.CENTER)
        head = Gtk.Box(spacing=10)
        title = Gtk.Label(label=self._("ach_title"), xalign=0)
        title.get_style_context().add_class("home-launch-title")
        head.pack_start(title, False, False, 0)
        texts.pack_start(head, False, False, 0)
        game = Gtk.Label(label=self.title, xalign=0)
        game.set_ellipsize(Pango.EllipsizeMode.END)
        game.get_style_context().add_class("gp-card-sub")
        texts.pack_start(game, False, False, 0)
        self.count = Gtk.Label(label="", xalign=0)
        self.count.get_style_context().add_class("ach-count")
        self.app._apply_tabular_figures(self.count)
        texts.pack_start(self.count, False, False, 0)
        content.pack_start(texts, True, True, 0)
        # the highlights, as wide as each other under it: filled in once read
        self.highlights = Gtk.Box(spacing=10, homogeneous=True)
        self.highlights.set_margin_end(6)
        outer.pack_start(self.highlights, False, False, 0)
        close = Gtk.Button()
        close.get_style_context().add_class("ld-icon-btn")
        close.set_valign(Gtk.Align.START)
        close.add(self.app._icon_image_at_size("window-close-symbolic", 16))
        close.connect("clicked", lambda *_: self.dialog.destroy())
        content.pack_end(close, False, False, 0)
        self._find_hero()
        return card

    def _find_hero(self):
        uid = self.uid
        paths = []
        try:
            lib = self.app.hero_path(uid)
            if lib is not None:
                paths.append(str(lib))
        except Exception:
            pass
        scale = self.dialog.get_scale_factor() or 1

        def work():
            from pathlib import Path
            from plunderhub.core import launcher_art
            more = list(paths)
            if uid.startswith("steam:"):
                h = launcher_art._steam(self.appid).get("hero")
                if h:
                    more.append(str(h))
            for path in more:
                if not Path(path).is_file():
                    continue
                try:
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 1100 * scale, -1, True)
                except Exception:
                    continue
                GLib.idle_add(lambda p=pb: self.dialog.get_window() is not None and self.hero.set_picture(p)
                              and False)
                return
        threading.Thread(target=work, name="plunderhub-ach-hero", daemon=True).start()

    def _highlight(self, caption: str, value: str, sub: str = "", colour: Optional[str] = None) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.get_style_context().add_class("ach-highlight")
        cap = Gtk.Label(label=caption.upper(), xalign=0)
        cap.get_style_context().add_class("ld-stat-cap")
        cap.set_ellipsize(Pango.EllipsizeMode.END)
        cap.set_max_width_chars(1)
        cap.set_tooltip_text(caption)
        box.pack_start(cap, False, False, 0)
        val = Gtk.Label(xalign=0)
        val.set_ellipsize(Pango.EllipsizeMode.END)
        val.set_max_width_chars(1)          # the card's width, not the name's
        text = GLib.markup_escape_text(value)
        val.set_markup(f'<span foreground="{colour}">{text}</span>' if colour else text)
        val.get_style_context().add_class("ach-highlight-value")
        val.set_tooltip_text(value)
        box.pack_start(val, False, False, 0)
        if sub:
            s = Gtk.Label(label=sub, xalign=0)
            s.set_ellipsize(Pango.EllipsizeMode.END)
            s.set_max_width_chars(1)
            s.get_style_context().add_class("ach-when")
            self.app._apply_tabular_figures(s)
            box.pack_start(s, False, False, 0)
        return box

    # --- fitting the room (1080p at 150-200 %, a laptop) -------------------------------------

    def _fit_soon(self):
        if not self._fit_queued:
            self._fit_queued = True
            GLib.idle_add(self._fit)

    def _fit(self) -> bool:
        """Narrow: the order's buttons go under the tabs. Short or very
        narrow: the highlights stay out (the list keeps its room) - all of
        it back as the window grows."""
        self._fit_queued = False
        if self.dialog.get_window() is None:
            return False
        width = self.controls.get_allocated_width()
        height = self.dialog.get_allocated_height()
        if width <= 1:
            return False
        need = self.tabs_box.get_preferred_width()[1] + self.sorts_box.get_preferred_width()[1] + 16
        stacked = need > width
        # three cards with whole names, or none (cut names say little)
        highlights = height >= 660 and width >= 820 and bool(self.highlights.get_children())
        small_ring = height < 600
        state = (stacked, highlights, small_ring)
        if state == self._fit_state:
            return False
        self._fit_state = state
        self.controls.set_orientation(Gtk.Orientation.VERTICAL if stacked else Gtk.Orientation.HORIZONTAL)
        self.controls.set_spacing(10 if stacked else 16)
        self.sorts_box.set_halign(Gtk.Align.START if stacked else Gtk.Align.FILL)
        self.tabs_box.set_halign(Gtk.Align.START if stacked else Gtk.Align.FILL)
        self.highlights.set_visible(highlights)
        self.ring.set_size_request(*((72, 72) if small_ring else (100, 100)))
        return False

    def _fill_highlights(self):
        _ = self._
        for child in self.highlights.get_children():
            self.highlights.remove(child)
        got = [a for a in self.data.items if a.unlocked_at is not None]
        if not got:
            return
        latest = max(got, key=lambda a: a.unlocked_at or 0)
        self.highlights.pack_start(self._highlight(_("ach_hl_latest"), latest.name, self._date(latest.unlocked_at)),
                                   True, True, 0)
        known = [a for a in got if a.percent is not None]
        if known:
            rarest = min(known, key=lambda a: a.percent)
            _key, colour = rarity(rarest.percent)
            self.highlights.pack_start(self._highlight(_("ach_hl_rarest"), rarest.name, self._pct(rarest.percent),
                                                       colour), True, True, 0)
            rare = sum(1 for a in known if a.percent < 10.0)
            self.highlights.pack_start(self._highlight(_("ach_hl_rare"), str(rare), _("ach_hl_rare_sub"),
                                                       RARITY[1][2]), True, True, 0)
        for child in self.highlights.get_children():
            child.show_all()
        self._fit_state = None
        self._fit_soon()

    # --- the tabs and the order ------------------------------------------------------------

    def _controls(self) -> Gtk.Widget:
        row = _Bendable(spacing=16)
        self.controls = row
        tabs = Gtk.Box(spacing=0)
        tabs.get_style_context().add_class("ach-segment")
        self.tab_chips: Dict[str, Gtk.ToggleButton] = {}
        for key in ("unlocked", "locked"):
            chip = self._chip(lambda k=key: self._set_tab(k), "ach-segment-btn")
            self.tab_chips[key] = chip
            tabs.pack_start(chip, False, False, 0)
        row.pack_start(tabs, False, False, 0)
        sorts = Gtk.Box(spacing=6)
        self.sort_chips: Dict[str, Gtk.ToggleButton] = {}
        for key in SORTS:
            chip = self._chip(lambda k=key: self._set_sort(k), "sw-chip")
            chip.set_label(self._(f"ach_sort_{key}"))
            self.sort_chips[key] = chip
            sorts.pack_start(chip, False, False, 0)
        row.pack_end(sorts, False, False, 0)
        self.tabs_box, self.sorts_box = tabs, sorts
        self._sync_chips()
        return row

    def _chip(self, on, css: str) -> Gtk.ToggleButton:
        chip = Gtk.ToggleButton()
        chip.get_style_context().add_class(css)
        allow_hover_relayout(chip, "its dimmed text lights up under the pointer")
        chip.set_focus_on_click(False)
        chip.plunderhub_handler = chip.connect("toggled", lambda b: b.get_active() and on())
        return chip

    def _sync_chips(self):
        for group, current in ((self.tab_chips, self.tab), (self.sort_chips, self.sort)):
            for key, chip in group.items():
                if chip.get_active() != (key == current):
                    chip.handler_block(chip.plunderhub_handler)
                    chip.set_active(key == current)
                    chip.handler_unblock(chip.plunderhub_handler)
        # the locked ones have no "newest"
        self.sort_chips["newest"].set_visible(self.tab == "unlocked")

    # --- loading -------------------------------------------------------------------------

    def _load(self):
        try:
            data = ach.for_uid(self.uid, getattr(self.app, "lang", "en"))
        except Exception as e:
            logger.warning(f"Achievements of {self.appid} couldn't be read: {e}")
            data = None
        GLib.idle_add(self._loaded, data)

    def _loaded(self, data) -> bool:
        if self.dialog.get_window() is None:
            return False
        self.data = data
        if data is not None:
            self.appid = data.appid
        if data is None:
            self.empty.set_text(self._("ach_none"))
            self.stack.set_visible_child_name("empty")
            return False
        n, total = data.unlocked, data.total
        self.count.set_text(self._("ach_progress").format(n=n, total=total))
        self.ring.set_fraction(n / total if total else 0)
        self.tab_chips["unlocked"].set_label(f"{self._('ach_unlocked')}  {n}")
        self.tab_chips["locked"].set_label(f"{self._('ach_locked')}  {total - n}")
        self._fill_highlights()
        if n == 0:
            self.tab = "locked"
            self.sort = "common"
            self._sync_chips()
        for a in data.items:
            self.rows.append(self._row(a))
        # added a slice at a time: a thousand cards don't stall the window
        self._add_rows(0)
        return False

    def _add_rows(self, start: int) -> bool:
        if self.dialog.get_window() is None:
            return False
        for row in self.rows[start:start + 120]:
            self.listbox.add(row)
            row.show_all()
        if start + 120 < len(self.rows):
            GLib.idle_add(self._add_rows, start + 120)
        else:
            self._refilter()
        return False

    # --- the list ------------------------------------------------------------------------

    def _list(self) -> Gtk.Widget:
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.get_style_context().add_class("ach-list")
        self.listbox.set_filter_func(self._filter)
        self.listbox.set_sort_func(self._compare)
        self.listbox.connect("row-activated", lambda _lb, row: self._reveal(row))
        # a tab or order switched: the cards' places are known once laid out again
        self.listbox.connect("size-allocate", lambda *_: self._want_icons_soon())
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_overlay_scrolling(False)
        self.scroll.get_style_context().add_class("settings-scroll")
        self.scroll.add(self.listbox)
        self.scroll.connect("scroll-event", self._on_wheel)
        self.listbox.set_adjustment(self.scroll.get_vadjustment())
        self.scroll.get_vadjustment().connect("value-changed", lambda *_: self._want_icons_soon())
        self.scroll.connect("size-allocate", lambda *_: self._want_icons_soon())
        return self.scroll

    def _on_wheel(self, _w, event) -> bool:
        """Half a card a notch; a touchpad's smooth scroll at the same pace."""
        adj = self.scroll.get_vadjustment()
        if event.direction == Gdk.ScrollDirection.UP:
            delta = -WHEEL_STEP
        elif event.direction == Gdk.ScrollDirection.DOWN:
            delta = WHEEL_STEP
        elif event.direction == Gdk.ScrollDirection.SMOOTH:
            ok, _dx, dy = event.get_scroll_deltas()
            if not ok or dy == 0:
                return False
            delta = dy * WHEEL_STEP
        else:
            return False
        adj.set_value(max(adj.get_lower(), min(adj.get_upper() - adj.get_page_size(), adj.get_value() + delta)))
        return True

    def _row(self, a: ach.Achievement) -> Gtk.ListBoxRow:
        _ = self._
        got = a.unlocked_at is not None
        key, colour = rarity(a.percent)
        row = Gtk.ListBoxRow()
        row.get_style_context().add_class("ach-row")
        row.plunderhub_ach = a
        card = Gtk.Box(spacing=14)
        card.get_style_context().add_class("ach-card")
        if not got:
            card.get_style_context().add_class("ach-card-locked")
        card.pack_start(Stripe(colour) if key is not None else Gtk.Box(), False, False, 0)
        icon = Gtk.Image()
        icon.set_size_request(ICON_PX, ICON_PX)
        icon.set_valign(Gtk.Align.CENTER)
        icon.set_margin_top(10)
        icon.set_margin_bottom(10)
        row.plunderhub_icon = icon
        card.pack_start(icon, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        texts.set_valign(Gtk.Align.CENTER)
        texts.set_margin_top(10)
        texts.set_margin_bottom(10)
        name = Gtk.Label(label=a.name, xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.get_style_context().add_class("ach-name")
        texts.pack_start(name, False, False, 0)
        desc = Gtk.Label(label=a.desc, xalign=0)
        desc.set_line_wrap(True)
        # a long word (a URL, a run of symbols) breaks too: no card wider than the window
        desc.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        desc.set_lines(2)
        desc.set_ellipsize(Pango.EllipsizeMode.END)
        desc.set_max_width_chars(60)
        desc.get_style_context().add_class("gp-card-sub")
        texts.pack_start(desc, False, False, 0)
        row.plunderhub_cover = None
        if a.hidden and not got:
            # not said before it's earned - unless asked
            cover = Gtk.Label(label=_("ach_hidden"), xalign=0)
            cover.set_line_wrap(True)
            cover.get_style_context().add_class("ach-hidden")
            texts.pack_start(cover, False, False, 0)
            row.plunderhub_cover = cover
            row.plunderhub_desc = desc
            desc.set_no_show_all(True)
        card.pack_start(texts, True, True, 0)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.set_valign(Gtk.Align.CENTER)
        side.set_margin_end(14)
        if a.percent is not None:
            line = Gtk.Box(spacing=8)
            line.set_halign(Gtk.Align.END)
            tier = Gtk.Label(xalign=1)
            text = GLib.markup_escape_text(_(key))
            tier.set_markup(f'<span foreground="{colour}">{text}</span>' if colour else text)
            tier.get_style_context().add_class("ach-tier")
            if colour is None:
                tier.get_style_context().add_class("ach-tier-accent")
            line.pack_start(tier, False, False, 0)
            pct = Gtk.Label(label=self._pct(a.percent), xalign=1)
            pct.get_style_context().add_class("ach-pct-small")
            pct.set_tooltip_text(_("ach_players").format(p=self._num(a.percent)))
            self.app._apply_tabular_figures(pct)
            line.pack_start(pct, False, False, 0)
            side.pack_start(line, False, False, 0)
            bar = RarityBar(a.percent, colour)
            bar.set_halign(Gtk.Align.END)
            side.pack_start(bar, False, False, 0)
        if got:
            when = Gtk.Label(label=self._date(a.unlocked_at), xalign=1)
            when.get_style_context().add_class("ach-when")
            self.app._apply_tabular_figures(when)
            side.pack_start(when, False, False, 0)
        card.pack_end(side, False, False, 0)
        row.add(card)
        return row

    @staticmethod
    def _date(when: Optional[float]) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(when)) if when else ""

    def _num(self, value: float) -> str:
        """One decimal, with the language's own decimal mark."""
        text = f"{value:.1f}"
        return text if getattr(self.app, "lang", "en") == "en" else text.replace(".", ",")

    def _pct(self, value: float) -> str:
        # a brand-new one hardly anyone has yet: not "0.0 %"
        return f"< {self._num(0.1)} %" if value < 0.05 else f"{self._num(value)} %"

    def _reveal(self, row):
        cover = getattr(row, "plunderhub_cover", None)
        if cover is not None and cover.get_visible():
            cover.hide()
            row.plunderhub_desc.set_no_show_all(False)
            row.plunderhub_desc.show()

    def _filter(self, row) -> bool:
        a = row.plunderhub_ach
        return (a.unlocked_at is not None) == (self.tab == "unlocked")

    def _compare(self, r1, r2) -> int:
        a, b = r1.plunderhub_ach, r2.plunderhub_ach
        if self.sort == "newest":
            ka, kb = -(a.unlocked_at or 0), -(b.unlocked_at or 0)
        elif self.sort == "rarest":
            ka = a.percent if a.percent is not None else 1000.0
            kb = b.percent if b.percent is not None else 1000.0
        elif self.sort == "common":
            ka = -(a.percent if a.percent is not None else -1.0)
            kb = -(b.percent if b.percent is not None else -1.0)
        else:
            ka, kb = 0, 0
        if ka != kb:
            return -1 if ka < kb else 1
        na, nb = a.name.casefold(), b.name.casefold()
        return (na > nb) - (na < nb)

    def _set_tab(self, key: str):
        self.tab = key
        if key == "locked" and self.sort == "newest":
            self.sort = "common"        # the next ones within reach first
        self._sync_chips()
        self._refilter()

    def _set_sort(self, key: str):
        self.sort = key
        self._sync_chips()
        self._refilter()

    def _refilter(self):
        if self.data is None:
            return
        self.listbox.invalidate_filter()
        self.listbox.invalidate_sort()
        shown = sum(1 for a in self.data.items if (a.unlocked_at is not None) == (self.tab == "unlocked"))
        if shown == 0:
            self.empty.set_text(self._("ach_none_unlocked" if self.tab == "unlocked" else "ach_all_unlocked"))
            self.stack.set_visible_child_name("empty")
        else:
            self.stack.set_visible_child_name("list")
            self.scroll.get_vadjustment().set_value(0)
        self._want_icons_soon()

    # --- icons, the ones in sight first ----------------------------------------------------

    def _want_icons_soon(self):
        if not self._icon_timer:
            self._icon_timer = GLib.timeout_add(120, self._want_icons)

    def _want_icons(self) -> bool:
        self._icon_timer = 0
        if self.data is None or self.dialog.get_window() is None:
            return False
        adj = self.scroll.get_vadjustment()
        top, bottom = adj.get_value() - 400, adj.get_value() + adj.get_page_size() + 800
        for row in self.listbox.get_children():
            if not row.get_child_visible() or id(row) in self._icons_asked:
                continue
            alloc = row.get_allocation()
            if alloc.height <= 1 or alloc.y + alloc.height < top or alloc.y > bottom:
                continue
            self._icons_asked.add(id(row))
            a = row.plunderhub_ach
            name = a.icon if a.unlocked_at is not None else (a.icon_gray or a.icon)
            scale = self.dialog.get_scale_factor() or 1
            try:
                self._pool.submit(self._fetch_icon, row, name, scale)
            except RuntimeError:
                return False        # the window closed meanwhile
        return False

    def _fetch_icon(self, row, name: str, scale: int):
        path = ach.icon_file(self.appid, name)
        if path is None:
            return
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), ICON_PX * scale, ICON_PX * scale, True)
        except Exception:
            return
        GLib.idle_add(self._set_icon, row, pb, scale)

    def _set_icon(self, row, pb, scale) -> bool:
        if self.dialog.get_window() is None:
            return False
        row.plunderhub_icon.set_from_surface(_rounded_surface(pb, scale, 10))
        return False

    def _on_key(self, _w, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.dialog.destroy()
            return True
        return False


def _rounded_surface(pb, scale: int, radius: float):
    """The icon with rounded corners (an image's CSS radius doesn't clip it)."""
    import cairo
    w, h = pb.get_width(), pb.get_height()
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    r = radius * scale
    cr.new_sub_path()
    cr.arc(w - r, r, r, -math.pi / 2, 0)
    cr.arc(w - r, h - r, r, 0, math.pi / 2)
    cr.arc(r, h - r, r, math.pi / 2, math.pi)
    cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()
    cr.clip()
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.paint()
    surf.set_device_scale(scale, scale)
    return surf


def open_achievements(app, parent: Optional[Gtk.Window], uid: str, title: str) -> AchievementsWindow:
    return AchievementsWindow(app, parent, uid, title)
