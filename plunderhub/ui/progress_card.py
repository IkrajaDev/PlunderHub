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
"""The pieces of the progress cards - the Auto update window and the
launcher install / removal window share them, so the two always look
alike.

card_dialog()  an undecorated, rounded card dialog
ProgressRing   an icon inside a ring that fills as the work goes on,
               with the percentage (or a check / cross) on its foot
Stepper        the steps in a row: done ones ticked and joined by a lit
               line, the current one with a turning arc and a halo
LogPanel       the log behind a "Details" toggle

Animation is driven from outside: tick() once per frame of the owner's
own timer, which also eases the ring towards its target.
"""

import math
from typing import Dict, List, Optional, Tuple

import cairo
import gi

gi.require_version("PangoCairo", "1.0")
from gi.repository import PangoCairo  # noqa: E402

from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402
from plunderhub.icons import get_icon_pixbuf  # noqa: E402
from plunderhub.ui.home_mixin import _accent_rgb, _on_accent_rgb, _rounded  # noqa: E402

DANGER_RGB = (0.898, 0.325, 0.294)
CARD_WIDTH = 460
RING_SIZE = 132
RING_ICON = 72


def card_dialog(parent: Optional[Gtk.Window], title: str) -> Tuple[Gtk.Dialog, Gtk.Box]:
    """A modal card without window decorations; returns it and the
    column to fill."""
    dlg = Gtk.Dialog(transient_for=parent, modal=True)
    dlg.set_title(title)
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
    content = dlg.get_content_area()
    content.set_border_width(0)
    column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    column.set_border_width(26)
    column.set_size_request(CARD_WIDTH, -1)
    content.pack_start(column, True, True, 0)
    # GTK focuses the first button when the window maps - and a focused
    # pill turns white as soon as the window is active, under the
    # pointer. Nothing is focused until the keyboard moves there.
    dlg.connect("map", lambda *_: GLib.idle_add(lambda: dlg.get_window() is not None and dlg.set_focus(None)))
    return dlg, column


def status_label(text: str) -> Gtk.Label:
    """One line, cut short with "…" - a long log line must not widen
    the card, nor a wrapped one make it taller."""
    label = Gtk.Label(label=text)
    label.set_ellipsize(Pango.EllipsizeMode.END)
    # a natural width far below the card's: it takes the card's width,
    # never sets it
    label.set_width_chars(1)
    label.set_max_width_chars(1)
    label.set_hexpand(True)
    label.get_style_context().add_class("update-status")
    label.set_margin_top(4)
    return label


def show_final(label: Gtk.Label, text: str, error: bool = False):
    """The status line's last word - the outcome, or with error, in red:
    wrapped so all of it shows, within the card's width (the card grows
    taller once, at the end)."""
    label.set_text(text)
    if error:
        label.get_style_context().add_class("update-status-error")
    label.set_ellipsize(Pango.EllipsizeMode.NONE)
    label.set_line_wrap(True)
    label.set_justify(Gtk.Justification.CENTER)
    label.set_max_width_chars(44)


def _colours(widget) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    ctx = widget.get_style_context()
    fg = ctx.get_color(ctx.get_state())
    return _accent_rgb(widget), (fg.red, fg.green, fg.blue)


def _layout(cr, widget, text: str, size: float, bold: bool):
    layout = PangoCairo.create_layout(cr)
    desc = widget.get_pango_context().get_font_description().copy()
    desc.set_size(int(size * Pango.SCALE))
    desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    layout.set_font_description(desc)
    layout.set_text(text, -1)
    return layout


class ProgressRing(Gtk.DrawingArea):
    def __init__(self, icon: Optional[GdkPixbuf.Pixbuf]):
        super().__init__()
        self.icon = icon
        # or a bundled symbolic icon, drawn in the text colour
        self.symbolic: Optional[str] = None
        self.target = 0.0
        self.shown = 0.0
        self.state = "running"     # running, done, failed
        self.pulse = 0.0
        self.set_size_request(RING_SIZE + 40, RING_SIZE + 24)
        self.set_halign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def tick(self, target: float):
        """One frame: eases towards target (glides rather than jumps)."""
        self.target = target
        self.shown += (target - self.shown) * 0.18
        if abs(target - self.shown) < 0.001:
            self.shown = target
        self.pulse += 0.12
        self.queue_draw()

    def finish(self, state: str):
        self.state = state
        if state == "done":
            self.shown = self.target = 1.0
        self.queue_draw()

    def _draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        accent, fg = _colours(widget)
        colour = DANGER_RGB if self.state == "failed" else accent
        cx, cy, r = w / 2, h / 2, RING_SIZE / 2 - 6
        # a soft glow, breathing while it works
        breathe = 0.5 + 0.5 * math.sin(self.pulse) if self.state == "running" else 1.0
        glow = cairo.RadialGradient(cx, cy, r * 0.5, cx, cy, r + 20)
        glow.add_color_stop_rgba(0, *colour, 0.16 + 0.12 * breathe)
        glow.add_color_stop_rgba(1, *colour, 0)
        cr.set_source(glow)
        cr.paint()
        cr.set_line_width(7)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(*fg, 0.10)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        fraction = 1.0 if self.state == "done" else self.shown
        if fraction > 0.002:
            start = -math.pi / 2
            cr.set_source_rgb(*colour)
            cr.arc(cx, cy, r, start, start + 2 * math.pi * min(1.0, fraction))
            cr.stroke()
        icon = self.icon
        if icon is None and self.symbolic:
            icon = get_icon_pixbuf(self.symbolic, round(RING_ICON * 0.7),
                                   "#%02x%02x%02x" % tuple(round(c * 255) for c in fg))
        if icon is not None:
            Gdk.cairo_set_source_pixbuf(cr, icon, cx - icon.get_width() / 2, cy - icon.get_height() / 2)
            cr.paint()
        # the percentage, or a check / cross, in a pill on the ring's foot
        text = {"done": "✓", "failed": "✕"}.get(self.state, f"{int(self.shown * 100)}%")
        layout = _layout(cr, widget, text, 10, True)
        tw, th = layout.get_pixel_size()
        pw, ph = max(tw + 20, 34), th + 8
        px, py = cx - pw / 2, cy + r - ph / 2 + 4
        _rounded(cr, px, py, pw, ph, ph / 2)
        cr.set_source_rgb(*colour)
        cr.fill()
        cr.set_source_rgb(*(_on_accent_rgb(colour) if self.state != "failed" else (1, 1, 1)))
        cr.move_to(cx - tw / 2, py + 4)
        PangoCairo.show_layout(cr, layout)
        return False


class Stepper(Gtk.DrawingArea):
    def __init__(self, steps: List[Tuple[str, str]]):
        """steps: (key, label) in order."""
        super().__init__()
        self.steps = steps
        self.states: Dict[str, str] = {key: "pending" for key, _label in steps}
        self.pulse = 0.0
        self.set_size_request(-1, 92)
        self.set_margin_top(10)
        self.connect("draw", self._draw)

    def set_steps(self, steps: List[Tuple[str, str]]):
        """Another set of steps (a batch once it knows what it'll run),
        keeping the state of those it had."""
        self.steps = steps
        self.states = {key: self.states.get(key, "pending") for key, _label in steps}
        self.queue_draw()

    def set_state(self, key: str, state: str):
        if key in self.states:
            self.states[key] = state
            self.queue_draw()

    def tick(self):
        self.pulse += 0.12
        if "active" in self.states.values():
            self.queue_draw()

    def _draw(self, widget, cr):
        w = widget.get_allocated_width()
        accent, fg = _colours(widget)
        n = len(self.steps)
        slot = w / n
        # room above and below the dots for the active one's pulsing
        # halo (up to dot_r + 6)
        dot_r, cy = 13, 22
        centres = [slot * (i + 0.5) for i in range(n)]
        # the line behind the dots: lit up to the last finished step
        cr.set_line_width(3)
        for i in range(n - 1):
            done = self.states[self.steps[i][0]] == "done"
            cr.set_source_rgba(*(accent if done else fg), 1.0 if done else 0.14)
            cr.move_to(centres[i] + dot_r + 4, cy)
            cr.line_to(centres[i + 1] - dot_r - 4, cy)
            cr.stroke()
        for i, (key, label) in enumerate(self.steps):
            x, state = centres[i], self.states[key]
            # The previous label left the pen where it ended: without a
            # fresh path the next circle would start with a line from there.
            cr.new_path()
            if state == "done":
                cr.arc(x, cy, dot_r, 0, 2 * math.pi)
                cr.set_source_rgb(*accent)
                cr.fill()
                cr.set_source_rgb(*_on_accent_rgb(accent))
                cr.set_line_width(2.4)
                cr.move_to(x - 5, cy)
                cr.line_to(x - 1.5, cy + 3.5)
                cr.line_to(x + 5.5, cy - 4)
                cr.stroke()
            elif state == "failed":
                cr.arc(x, cy, dot_r, 0, 2 * math.pi)
                cr.set_source_rgb(*DANGER_RGB)
                cr.fill()
                cr.set_source_rgb(1, 1, 1)
                cr.set_line_width(2.4)
                for dx in (-1, 1):
                    cr.move_to(x - 4.5 * dx, cy - 4.5)
                    cr.line_to(x + 4.5 * dx, cy + 4.5)
                cr.stroke()
            elif state == "active":
                breathe = 0.5 + 0.5 * math.sin(self.pulse * 1.5)
                cr.arc(x, cy, dot_r + 3 + 3 * breathe, 0, 2 * math.pi)
                cr.set_source_rgba(*accent, 0.12 + 0.10 * breathe)
                cr.fill()
                cr.arc(x, cy, dot_r, 0, 2 * math.pi)
                cr.set_source_rgba(*accent, 0.18)
                cr.fill()
                # a short arc running round it
                cr.set_line_width(2.6)
                cr.set_source_rgb(*accent)
                a = self.pulse * 2.2
                cr.new_sub_path()
                cr.arc(x, cy, dot_r - 1.5, a, a + 1.9)
                cr.stroke()
            else:
                cr.set_line_width(2)
                cr.set_source_rgba(*fg, 0.25)
                cr.arc(x, cy, dot_r - 1, 0, 2 * math.pi)
                cr.stroke()
            layout = _layout(cr, widget, label, 9, state == "active")
            layout.set_width(int((slot - 6) * Pango.SCALE))
            layout.set_wrap(Pango.WrapMode.WORD)
            layout.set_alignment(Pango.Alignment.CENTER)
            alpha = 1.0 if state in ("active", "done") else 0.55
            cr.set_source_rgba(*(DANGER_RGB if state == "failed" else fg), alpha)
            cr.move_to(x - (slot - 6) / 2, cy + dot_r + 9)
            PangoCairo.show_layout(cr, layout)
        return False


class LogPanel:
    """The log, behind a "Details ▾" toggle. toggle goes in the button
    row, revealer above it."""

    def __init__(self, details_text: str):
        self.details_text = details_text
        self.buffer = Gtk.TextBuffer()
        # A right-gravity mark stays after everything inserted at the
        # end, so scrolling to it always follows the newest line.
        self.end_mark = self.buffer.create_mark(None, self.buffer.get_end_iter(), False)
        self.view = Gtk.TextView(buffer=self.buffer)
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        for side in ("left", "right", "top", "bottom"):
            getattr(self.view, f"set_{side}_margin")(10)
        self.view.get_style_context().add_class("update-log")
        scroller = Gtk.ScrolledWindow()
        scroller.set_size_request(-1, 170)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.get_style_context().add_class("update-log-box")
        scroller.add(self.view)
        scroller.set_margin_top(10)
        self.revealer = Gtk.Revealer()
        self.revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.revealer.add(scroller)
        self.toggle = Gtk.Button()
        self.toggle.get_style_context().add_class("update-details-btn")
        self.toggle_label = Gtk.Label()
        self.toggle.add(self.toggle_label)
        self.toggle.connect("clicked", lambda *_: self.set_open(not self.revealer.get_reveal_child()))
        self._label()

    def set_open(self, open_: bool):
        self.revealer.set_reveal_child(open_)
        self._label()

    def _label(self):
        arrow = "▴" if self.revealer.get_reveal_child() else "▾"
        self.toggle_label.set_text(f"{self.details_text}  {arrow}")

    def append(self, line: str):
        self.buffer.insert(self.buffer.get_end_iter(), line + "\n")
        self.view.scroll_to_mark(self.end_mark, 0.0, False, 0.0, 1.0)


def pill_buttons(app, specs) -> List[Gtk.Button]:
    """The card's pill buttons, hidden until shown: specs are
    (text, kind) with kind "accent", "danger" or "ghost". Their insides
    are shown now, so a later show() brings the label along."""
    buttons = []
    for text, kind in specs:
        btn = app._dialog_button(text, kind)
        btn.get_child().show_all()
        btn.set_no_show_all(True)
        buttons.append(btn)
    return buttons


class BatchList:
    """The items of a batch install, one line each, with how each went:
    pending ○, active ◌ (accent), done ✓, failed ✕, skipped - with the
    reason under it."""

    GLYPHS = {"pending": "○", "active": "◌", "done": "✓", "failed": "✕", "skipped": "–"}

    def __init__(self, items: List[Tuple[str, str]]):
        self.rows: Dict[str, Tuple[Gtk.Label, Gtk.Label, Gtk.Label]] = {}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(10)
        for key, label in items:
            line = Gtk.Box(spacing=10)
            glyph = Gtk.Label(label=self.GLYPHS["pending"])
            glyph.set_size_request(18, -1)
            glyph.get_style_context().add_class("batch-glyph")
            name = Gtk.Label(label=label, xalign=0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            line.pack_start(glyph, False, False, 0)
            line.pack_start(name, True, True, 0)
            note = Gtk.Label(xalign=0)
            note.set_line_wrap(True)
            note.set_max_width_chars(48)
            note.set_margin_start(28)
            note.get_style_context().add_class("settings-row-sub")
            note.set_no_show_all(True)
            box.pack_start(line, False, False, 0)
            box.pack_start(note, False, False, 0)
            self.rows[key] = (glyph, name, note)
        self.widget = Gtk.ScrolledWindow()
        self.widget.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.widget.set_propagate_natural_height(True)
        self.widget.set_max_content_height(190)
        self.widget.set_overlay_scrolling(False)
        self.widget.get_style_context().add_class("settings-scroll")
        self.widget.add(box)

    def set_state(self, key: str, state: str, note: Optional[str] = None):
        row = self.rows.get(key)
        if row is None:
            return
        glyph, name, label = row
        glyph.set_text(self.GLYPHS[state])
        for widget in (glyph, name):
            ctx = widget.get_style_context()
            for s in self.GLYPHS:
                ctx.remove_class(f"batch-{s}")
            ctx.add_class(f"batch-{state}")
        if note:
            label.set_text(note)
            label.show()
