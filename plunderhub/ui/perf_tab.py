# PlunderHub - a GTK3 game launcher for Linux
# Copyright (C) 2026 IkrajaDev
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""The game page's Performance tab (core/perf_log.py records it).

On top the switch that records it (and whether MangoHud shows meanwhile);
then the sessions as bars - average FPS, the 1 % low inside - with a mark
where the Proton, the GPU driver or the game's version changed; under them
the picked session: four tiles (each against the session before it), its
FPS over time, loads and temperatures, and deleting it or all of them.

Store launchers in Faugus have no such tab (they aren't games)."""

import math
import threading
import time
from typing import Dict, List, Optional

import cairo
import gi

from plunderhub.core import perf_log
from plunderhub.gi_setup import Gdk, GLib, Gtk, Pango
gi.require_version("PangoCairo", "1.0")
from gi.repository import PangoCairo  # noqa: E402
from plunderhub.icons import get_icon_pixbuf
from plunderhub.logging_setup import logger

BARS_SHOWN = 30
CHART_H = 230
CURVE_H = 170
COLLECT_DELAY_S = 8         # MangoHud writes the rest of its log as the game closes


def wanted(game, backend) -> bool:
    return backend is not None and game.store_platform != "store-launcher"


def after_session(app, uid: str):
    """A session is over: its log becomes a summary a little later."""
    game = app.game_index.get(uid)
    if game is None or game.store_platform == "store-launcher":
        return

    def later() -> bool:
        if perf_log.raw_dir(game).is_dir():
            threading.Thread(target=perf_log.collect, args=(game,), name="plunderhub-perf-collect",
                             daemon=True).start()
        return False
    GLib.timeout_add_seconds(COLLECT_DELAY_S, later)


def _rgb(hex_colour: str):
    hex_colour = hex_colour.lstrip("#")
    return tuple(int(hex_colour[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _icon(name: str, px: int, colour: str) -> Gtk.Widget:
    pb = get_icon_pixbuf(name, px, colour)
    return Gtk.Image.new_from_pixbuf(pb) if pb is not None else Gtk.Label(label="")


def _rounded(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class PerformanceTab(Gtk.Box):
    def __init__(self, app, game, backend, page):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.app, self.game, self.backend, self.page = app, game, backend, page
        self._ = app._
        self.set_border_width(28)
        self.accent = app._home_accent_hex()
        self.sessions: List[Dict] = []
        self.picked: Optional[int] = None
        self.hover: Optional[int] = None
        self._bars: List[tuple] = []        # (x, w) per shown bar
        self._busy_switch = False

        self.pack_start(self._record_card(), False, False, 0)
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.pack_start(self.content, False, False, 0)
        self._load()

    # recording ------------------------------------------------------------------------------

    def _opts(self):
        return self.backend.load(self.game)

    def _missing(self) -> bool:
        launch = getattr(self.page, "launch", None)
        missing = getattr(launch, "missing", None)
        if missing is None:
            from plunderhub.core.launch_options import missing_tools
            missing = missing_tools()
        return bool(missing.get("mangohud"))

    def _record_card(self) -> Gtk.Widget:
        _ = self._
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.get_style_context().add_class("gp-card")
        card.get_style_context().add_class("pf-record")
        try:
            state = perf_log.recording(self._opts())
        except Exception as e:
            logger.warning(f"Performance tab: {self.game.title}'s options not read: {e}")
            state = None
        top = Gtk.Box(spacing=14)
        top.pack_start(_icon("gauge-symbolic", 30, self.accent), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=_("pf_record"), xalign=0)
        title.get_style_context().add_class("pf-record-title")
        texts.pack_start(title, False, False, 0)
        sub = Gtk.Label(label=_("pf_record_sub"), xalign=0)
        sub.set_line_wrap(True)
        sub.get_style_context().add_class("gp-note")
        texts.pack_start(sub, False, False, 0)
        top.pack_start(texts, True, True, 0)
        self.record = Gtk.Switch()
        self.record.set_valign(Gtk.Align.CENTER)
        self.record.set_active(state is not None)
        top.pack_end(self.record, False, False, 0)
        card.pack_start(top, False, False, 0)

        self.hud_row = Gtk.Box(spacing=12)
        self.hud_row.set_margin_start(44)
        hud_label = Gtk.Label(label=_("pf_show_hud"), xalign=0)
        self.hud_row.pack_start(hud_label, True, True, 0)
        self.hud = Gtk.Switch()
        self.hud.set_valign(Gtk.Align.CENTER)
        try:
            had_hud = bool(self._opts().mangohud)
        except Exception:
            had_hud = False
        # recording: as it is; not yet: MangoHud's overlay stays as the game had it
        self.hud.set_active(bool(state) if state is not None else had_hud)
        self.hud_row.pack_end(self.hud, False, False, 0)
        card.pack_start(self.hud_row, False, False, 0)
        self.hud_row.set_no_show_all(True)
        self.hud_row.show_all()
        self.hud_row.set_visible(state is not None)

        if self._missing() and state is None:
            self.record.set_sensitive(False)
            need = Gtk.Box(spacing=10)
            need.set_margin_start(44)
            note = Gtk.Label(label=_("pf_need_mangohud"), xalign=0)
            note.set_line_wrap(True)
            note.get_style_context().add_class("gp-note")
            need.pack_start(note, True, True, 0)
            get = Gtk.Button(label=_("pf_get_mangohud"))
            get.get_style_context().add_class("sw-link")
            get.connect("clicked", lambda *_a: self.app.show_shipwright(
                focus="mangohud", parent=self.get_toplevel(), on_closed=self._tools_changed))
            need.pack_end(get, False, False, 0)
            self.need_row = need
            card.pack_start(need, False, False, 0)
        self.record.connect("notify::active", lambda *_a: self._set(self.record.get_active(), self.hud.get_active()))
        self.hud.connect("notify::active", lambda *_a: self.record.get_active() and
                         self._set(True, self.hud.get_active()))
        return card

    def _tools_changed(self):
        launch = getattr(self.page, "launch", None)
        if launch is not None:
            launch.missing = launch._missing_tools()
        if not self._missing():
            self.record.set_sensitive(True)
            if getattr(self, "need_row", None) is not None:
                self.need_row.hide()

    def _set(self, on: bool, hud: bool):
        if self._busy_switch:
            return
        try:
            opts = self._opts()
            perf_log.set_recording(opts, self.game, on, hud)
            saved = self.app._lo_save(self.game, self.backend, opts, self.get_toplevel())
        except Exception as e:
            logger.warning(f"Performance recording of {self.game.title} not changed: {e}")
            saved = False
        if not saved:
            self._busy_switch = True
            self.record.set_active(not on if self.record.get_active() == on else self.record.get_active())
            self._busy_switch = False
            return
        self.hud_row.set_visible(on)
        launch = getattr(self.page, "launch", None)
        if launch is not None:
            # Launch options keeps what it read: the same change there, or its Save undoes it
            for model in (launch.loaded, launch.opts):
                perf_log.set_recording(model, self.game, on, hud)
            try:
                self.app._lo_write_widgets(launch.ui, launch.opts)
                launch._rebuild_env_rows()
            except Exception as e:
                logger.debug(f"Launch options tab not refreshed: {e}")

    # the sessions ---------------------------------------------------------------------------

    def _load(self):
        game = self.game

        def work():
            try:
                perf_log.collect(game)
            except Exception as e:
                logger.warning(f"Performance logs of {game.title} not read: {e}")
            sessions = perf_log.load(game.uid)
            GLib.idle_add(self._loaded, sessions)
        threading.Thread(target=work, name="plunderhub-perf-load", daemon=True).start()

    def _loaded(self, sessions: List[Dict]) -> bool:
        self.sessions = sessions[-BARS_SHOWN:]
        self.picked = len(self.sessions) - 1 if self.sessions else None
        self._fill()
        if self.record.get_active() and perf_log.log_missing(self.game):
            note = Gtk.Box(spacing=10)
            note.get_style_context().add_class("wc-error")
            note.pack_start(_icon("triangle-alert-symbolic", 18, "#f0a04b"), False, False, 0)
            text = Gtk.Label(label=self._("pf_no_log"), xalign=0)
            text.set_line_wrap(True)
            note.pack_start(text, True, True, 0)
            self.content.pack_start(note, False, False, 0)
            self.content.reorder_child(note, 0)
            note.show_all()
        return False

    def _fill(self):
        for child in self.content.get_children():
            child.destroy()
        _ = self._
        if not self.sessions:
            empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            empty.get_style_context().add_class("pf-empty")
            empty.pack_start(_icon("office-chart-bar-symbolic", 42, self.accent), False, False, 0)
            head = Gtk.Label(label=_("pf_empty"))
            head.get_style_context().add_class("pf-record-title")
            empty.pack_start(head, False, False, 0)
            text = Gtk.Label(label=_("pf_empty_sub"))
            text.set_line_wrap(True)
            text.set_justify(Gtk.Justification.CENTER)
            text.set_max_width_chars(60)
            text.get_style_context().add_class("gp-note")
            empty.pack_start(text, False, False, 0)
            self.content.pack_start(empty, False, False, 0)
            self.content.show_all()
            return
        self.tiles = Gtk.Box(spacing=12, homogeneous=True)
        self.content.pack_start(self.tiles, False, False, 0)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("gp-card")
        head = Gtk.Box(spacing=8)
        label = Gtk.Label(label=_("pf_sessions").upper(), xalign=0)
        label.get_style_context().add_class("gp-card-label")
        head.pack_start(label, False, False, 0)
        legend = Gtk.Label(xalign=1)
        legend.set_markup(f'<span foreground="{self.accent}">■</span> {GLib.markup_escape_text(_("pf_avg"))}'
                          f'   <span foreground="#f0c060">▬</span> {GLib.markup_escape_text(_("pf_low1"))}'
                          f'   <span foreground="#9aa4b2">┆</span> {GLib.markup_escape_text(_("pf_marks"))}')
        legend.get_style_context().add_class("gp-note")
        head.pack_end(legend, False, False, 0)
        card.pack_start(head, False, False, 0)
        self.chart = Gtk.DrawingArea()
        self.chart.set_size_request(-1, CHART_H)
        self.chart.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.BUTTON_PRESS_MASK
                              | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.chart.connect("draw", self._draw_chart)
        self.chart.connect("motion-notify-event", lambda _w, e: self._set_hover(self._bar_at(e.x)))
        self.chart.connect("leave-notify-event", lambda *_a: self._set_hover(None))
        self.chart.connect("button-press-event", lambda _w, e: self._pick(self._bar_at(e.x)))
        card.pack_start(self.chart, False, False, 0)
        self.content.pack_start(card, False, False, 0)

        self.detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.detail.get_style_context().add_class("gp-card")
        self.content.pack_start(self.detail, False, False, 0)
        self._fill_picked()
        self.content.show_all()

    # tiles + the picked session ----------------------------------------------------------------

    def _tile(self, title: str, value: str, unit: str, delta: Optional[float], higher_better: bool = True):
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        tile.get_style_context().add_class("pf-tile")
        name = Gtk.Label(label=title.upper(), xalign=0)
        name.get_style_context().add_class("pf-tile-name")
        tile.pack_start(name, False, False, 0)
        row = Gtk.Box(spacing=6)
        big = Gtk.Label(label=value, xalign=0)
        big.get_style_context().add_class("pf-tile-value")
        row.pack_start(big, False, False, 0)
        small = Gtk.Label(label=unit, xalign=0)
        small.set_valign(Gtk.Align.END)
        small.set_margin_bottom(6)
        small.get_style_context().add_class("gp-note")
        row.pack_start(small, False, False, 0)
        tile.pack_start(row, False, False, 0)
        if delta is not None and abs(delta) >= 0.5:
            good = (delta > 0) == higher_better
            arrow = "▲" if delta > 0 else "▼"
            change = Gtk.Label(label=f"{arrow} {abs(delta):.0f}%  {self._('pf_vs_before')}", xalign=0)
            change.get_style_context().add_class("pf-up" if good else "pf-down")
            tile.pack_start(change, False, False, 0)
        elif delta is not None:
            same = Gtk.Label(label=self._("pf_same_as_before"), xalign=0)
            same.get_style_context().add_class("gp-note")
            tile.pack_start(same, False, False, 0)
        return tile

    def _fill_picked(self):
        _ = self._
        for child in self.tiles.get_children() + self.detail.get_children():
            child.destroy()
        if self.picked is None:
            return
        s = self.sessions[self.picked]
        prev = self.sessions[self.picked - 1] if self.picked > 0 else None

        def delta(key):
            if prev is None or not prev.get(key):
                return None
            return (s.get(key, 0) - prev[key]) / prev[key] * 100
        minutes = int(s.get("seconds", 0)) // 60
        played = f"{minutes // 60}:{minutes % 60:02d}" if minutes >= 60 else str(minutes)
        for tile in (self._tile(_("pf_avg"), f"{s['avg']:.0f}", "FPS", delta("avg")),
                     self._tile(_("pf_low1"), f"{s.get('low1', 0):.0f}", "FPS", delta("low1")),
                     self._tile(_("pf_frametime"), f"{s.get('ft_p99', 0):.1f}", "ms", delta("ft_p99"), False),
                     self._tile(_("pf_played"), played, "h" if minutes >= 60 else "min", None)):
            self.tiles.pack_start(tile, True, True, 0)

        head = Gtk.Box(spacing=8)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.get("at", 0)))
        label = Gtk.Label(label=_("pf_session_of").format(when=when).upper(), xalign=0)
        label.get_style_context().add_class("gp-card-label")
        head.pack_start(label, False, False, 0)
        delete_all = Gtk.Button(label=_("pf_delete_all"))
        delete_all.get_style_context().add_class("sw-link")
        delete_all.get_style_context().add_class("pf-danger")
        delete_all.connect("clicked", lambda *_a: self._delete(None))
        head.pack_end(delete_all, False, False, 0)
        delete_one = Gtk.Button(label=_("pf_delete_one"))
        delete_one.get_style_context().add_class("sw-link")
        delete_one.connect("clicked", lambda *_a: self._delete(s.get("at")))
        head.pack_end(delete_one, False, False, 0)
        self.detail.pack_start(head, False, False, 0)

        curve = Gtk.DrawingArea()
        curve.set_size_request(-1, CURVE_H)
        curve.connect("draw", self._draw_curve, s)
        self.detail.pack_start(curve, False, False, 0)

        # four even columns, each fact centred: icon, what it is, the value
        facts = Gtk.Grid()
        facts.set_column_spacing(10)
        facts.set_row_spacing(10)
        facts.set_column_homogeneous(True)
        snap = s.get("snap") or {}
        for index, (icon, name, value) in enumerate((
                ("cpu-symbolic", _("pf_cpu"), self._load_temp(s.get("cpu_load"), s.get("cpu_temp"))),
                ("monitor-symbolic", _("pf_gpu"), self._load_temp(s.get("gpu_load"), s.get("gpu_temp"))),
                ("package-symbolic", _("pf_vram"), f"{s['vram']:.1f} GB" if s.get("vram") else "—"),
                ("gauge-symbolic", _("pf_range"), f"{s.get('min', 0):.0f} – {s.get('max', 0):.0f} FPS"),
                ("wine-symbolic", "Proton", snap.get("proton") or "—"),
                ("monitor-dot-symbolic", _("wc_f_driver"), snap.get("driver") or "—"),
                ("package-symbolic", _("wc_f_game"), snap.get("game") or "—"),
                ("dialog-information-symbolic", _("pf_low01"), f"{s.get('low01', 0):.0f} FPS"))):
            fact = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            fact.get_style_context().add_class("pf-fact")
            icon_img = _icon(icon, 20, self.accent)
            icon_img.set_halign(Gtk.Align.CENTER)
            fact.pack_start(icon_img, False, False, 0)
            n = Gtk.Label(label=name.upper())
            n.set_justify(Gtk.Justification.CENTER)
            n.set_line_wrap(True)
            n.get_style_context().add_class("pf-tile-name")
            fact.pack_start(n, False, False, 0)
            v = Gtk.Label(label=value)
            v.set_justify(Gtk.Justification.CENTER)
            v.set_line_wrap(True)
            v.set_line_wrap_mode(Pango.WrapMode.WORD)      # "Proton-GE Latest" / "(GE-Proton10-25)"
            v.set_lines(2)
            v.set_ellipsize(Pango.EllipsizeMode.END)
            v.set_max_width_chars(18)
            v.set_tooltip_text(value)
            v.get_style_context().add_class("pf-fact-value")
            fact.pack_start(v, False, False, 0)
            facts.attach(fact, index % 4, index // 4, 1, 1)
        self.detail.pack_start(facts, False, False, 0)
        self.tiles.show_all()
        self.detail.show_all()

    @staticmethod
    def _load_temp(load, temp) -> str:
        parts = []
        if load is not None:
            parts.append(f"{load:.0f}%")
        if temp:
            parts.append(f"{temp:.0f} °C")
        return "  ·  ".join(parts) or "—"

    def _delete(self, at: Optional[float]):
        _ = self._
        if at is None:
            if self.app._card_dialog(_("pf_delete_all_q"), _("pf_delete_all_text"),
                                     [(_("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost"),
                                      (_("pf_delete_all"), Gtk.ResponseType.OK, "danger")],
                                     tone="warning", transient_for=self.get_toplevel()) != Gtk.ResponseType.OK:
                return
        perf_log.delete(self.game.uid, at)
        self._loaded(perf_log.load(self.game.uid))

    # drawing --------------------------------------------------------------------------------

    def _layout(self, widget, text: str, scale: float = 0.85, bold: bool = False):
        layout = widget.create_pango_layout(text)
        desc = widget.get_style_context().get_font(widget.get_style_context().get_state()).copy()
        desc.set_size(int(desc.get_size() * scale))
        if bold:
            desc.set_weight(Pango.Weight.BOLD)
        layout.set_font_description(desc)
        return layout

    def _fg(self, widget):
        c = widget.get_style_context().get_color(widget.get_style_context().get_state())
        return c.red, c.green, c.blue

    def _bar_at(self, x: float) -> Optional[int]:
        for i, (bx, bw) in enumerate(self._bars):
            if bx - 3 <= x <= bx + bw + 3:
                return i
        return None

    def _set_hover(self, index: Optional[int]):
        if index != self.hover:
            old, self.hover = self.hover, index
            for i in (old, index):
                if i is not None and i < len(self._bars):
                    x, w = self._bars[i]
                    self.chart.queue_draw_area(int(x) - 4, 0, int(w) + 8, CHART_H)
            self.chart.get_window() and self.chart.get_window().set_cursor(
                Gdk.Cursor.new_from_name(self.chart.get_display(), "pointer") if index is not None else None)

    def _pick(self, index: Optional[int]):
        if index is None or index == self.picked:
            return
        self.picked = index
        self.chart.queue_draw()
        self._fill_picked()

    def _draw_chart(self, widget, cr) -> bool:
        w = widget.get_allocated_width()
        h = CHART_H
        fg = self._fg(widget)
        accent = _rgb(self.accent)
        left, right, top, bottom = 44, 8, 30, 28
        plot_h = h - top - bottom
        top_fps = max(s.get("max", s["avg"]) for s in self.sessions)
        top_fps = max(30, math.ceil(max(s["avg"] for s in self.sessions) * 1.15 / 30) * 30) \
            if top_fps > 0 else 60
        # grid
        for i in range(5):
            value = top_fps * i / 4
            y = top + plot_h - plot_h * i / 4
            cr.set_source_rgba(*fg, 0.07)
            cr.set_line_width(1)
            cr.move_to(left, y + 0.5)
            cr.line_to(w - right, y + 0.5)
            cr.stroke()
            lay = self._layout(widget, f"{value:.0f}", 0.75)
            _tw, th = lay.get_pixel_size()
            cr.set_source_rgba(*fg, 0.45)
            cr.move_to(left - 8 - lay.get_pixel_size()[0], y - th / 2)
            PangoCairo.show_layout(cr, lay)
        n = len(self.sessions)
        # a slot per session from the left, never wider than this: the first one
        # stands at the start, not in the middle of an empty chart
        slot = min((w - left - right) / max(n, 1), 64.0)
        bar_w = max(6.0, min(46.0, slot * 0.62))
        self._bars = []
        prev_snap = None
        for i, s in enumerate(self.sessions):
            x = left + slot * i + (slot - bar_w) / 2
            self._bars.append((x, bar_w))
            snap = s.get("snap") or {}
            if prev_snap is not None:
                changed = [snap.get(k) for k in ("proton", "driver", "game")
                           if snap.get(k) and prev_snap.get(k) and snap.get(k) != prev_snap.get(k)]
                if changed:
                    mx = left + slot * i
                    cr.set_source_rgba(*fg, 0.35)
                    cr.set_dash([3, 4])
                    cr.move_to(mx + 0.5, top - 6)
                    cr.line_to(mx + 0.5, top + plot_h)
                    cr.stroke()
                    cr.set_dash([])
                    lay = self._layout(widget, changed[0], 0.7)
                    lay.set_width(int(max(slot * 3, 80) * Pango.SCALE))
                    lay.set_ellipsize(Pango.EllipsizeMode.END)
                    cr.set_source_rgba(*fg, 0.6)
                    cr.move_to(mx + 4, top - 22)
                    PangoCairo.show_layout(cr, lay)
            prev_snap = snap
            avg_h = plot_h * min(1.0, s["avg"] / top_fps)
            picked, hover = i == self.picked, i == self.hover
            alpha = 1.0 if picked else 0.78 if hover else 0.45
            grad_top = top + plot_h - avg_h
            pattern = cairo.LinearGradient(0, grad_top, 0, top + plot_h)
            pattern.add_color_stop_rgba(0, *accent, alpha)
            pattern.add_color_stop_rgba(1, *accent, alpha * 0.35)
            _rounded(cr, x, grad_top, bar_w, avg_h, min(8, bar_w / 2))
            cr.set_source(pattern)
            cr.fill()
            low = s.get("low1") or 0
            if low:
                ly = top + plot_h - plot_h * min(1.0, low / top_fps)
                cr.set_source_rgba(0.94, 0.75, 0.38, 1.0 if picked or hover else 0.7)
                cr.set_line_width(3)
                cr.move_to(x + 2, ly)
                cr.line_to(x + bar_w - 2, ly)
                cr.stroke()
            if picked or hover or n <= 12:
                lay = self._layout(widget, f"{s['avg']:.0f}", 0.75, bold=picked)
                tw, th = lay.get_pixel_size()
                cr.set_source_rgba(*fg, 0.95 if picked else 0.6)
                cr.move_to(x + bar_w / 2 - tw / 2, grad_top - th - 2)
                PangoCairo.show_layout(cr, lay)
            if picked or hover or i in (0, n - 1):
                lay = self._layout(widget, time.strftime("%m-%d", time.localtime(s.get("at", 0))), 0.7)
                tw, _th = lay.get_pixel_size()
                cr.set_source_rgba(*fg, 0.8 if picked else 0.45)
                cr.move_to(x + bar_w / 2 - tw / 2, top + plot_h + 8)
                PangoCairo.show_layout(cr, lay)
        return False

    def _draw_curve(self, widget, cr, s: Dict) -> bool:
        curve = s.get("curve") or []
        if len(curve) < 2:
            return False
        w = widget.get_allocated_width()
        h = CURVE_H
        fg = self._fg(widget)
        accent = _rgb(self.accent)
        left, right, top, bottom = 44, 8, 10, 22
        plot_w, plot_h = w - left - right, h - top - bottom
        peak = max(max(curve), s.get("avg", 0)) * 1.1 or 60
        for i in range(4):
            value = peak * i / 3
            y = top + plot_h - plot_h * i / 3
            cr.set_source_rgba(*fg, 0.07)
            cr.set_line_width(1)
            cr.move_to(left, y + 0.5)
            cr.line_to(w - right, y + 0.5)
            cr.stroke()
            lay = self._layout(widget, f"{value:.0f}", 0.75)
            tw, th = lay.get_pixel_size()
            cr.set_source_rgba(*fg, 0.45)
            cr.move_to(left - 8 - tw, y - th / 2)
            PangoCairo.show_layout(cr, lay)
        pts = [(left + plot_w * i / (len(curve) - 1), top + plot_h - plot_h * min(1.0, v / peak))
               for i, v in enumerate(curve)]
        cr.move_to(pts[0][0], top + plot_h)
        for x, y in pts:
            cr.line_to(x, y)
        cr.line_to(pts[-1][0], top + plot_h)
        cr.close_path()
        fill = cairo.LinearGradient(0, top, 0, top + plot_h)
        fill.add_color_stop_rgba(0, *accent, 0.35)
        fill.add_color_stop_rgba(1, *accent, 0.02)
        cr.set_source(fill)
        cr.fill()
        cr.move_to(*pts[0])
        for x, y in pts[1:]:
            cr.line_to(x, y)
        cr.set_source_rgba(*accent, 1)
        cr.set_line_width(2)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.stroke()
        avg_y = top + plot_h - plot_h * min(1.0, s["avg"] / peak)
        cr.set_source_rgba(*fg, 0.5)
        cr.set_dash([5, 5])
        cr.move_to(left, avg_y)
        cr.line_to(w - right, avg_y)
        cr.stroke()
        cr.set_dash([])
        minutes = int(s.get("seconds", 0)) // 60
        for frac, text in ((0, "0:00"), (1, f"{minutes // 60}:{minutes % 60:02d}")):
            lay = self._layout(widget, text, 0.7)
            tw, _th = lay.get_pixel_size()
            cr.set_source_rgba(*fg, 0.45)
            cr.move_to(left + plot_w * frac - (tw if frac else 0), top + plot_h + 6)
            PangoCairo.show_layout(cr, lay)
        return False
