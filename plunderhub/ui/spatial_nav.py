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
"""Arrows and the d-pad on a page of buttons laid out freely (the
Bounty Board, the Loading Dock, the Treasure Map's tabs and chips): from
what's stood on, onto the nearest button that way - by where they are on
the screen, not their order in the widget tree. What's stood on gets the
keyboard's ring (.kb-focus) and is scrolled into view; Enter / Space /
the pad's A press it as GTK does a focused button.

    stops(root)                      what can be stood on in root
    pick(window, stops, frm, way)    the nearest one way of frm (a widget or a rect)
    stand(window, widget)            focus it, ring it, scroll to it
"""

from typing import List, Optional, Sequence, Tuple, Union

from plunderhub.gi_setup import Gtk

Rect = Tuple[float, float, float, float]


def stops(root: Gtk.Widget, extra: Sequence[Gtk.Widget] = ()) -> List[Gtk.Widget]:
    """The focusable buttons, switches and fields showing in root (a
    button's own insides aren't stops), plus extra (a tile grid)."""
    out: List[Gtk.Widget] = []

    def walk(w: Gtk.Widget):
        if not (w.get_visible() and w.get_mapped() and w.is_sensitive()):
            return
        if w in extra:
            out.append(w)
            return
        if isinstance(w, (Gtk.Button, Gtk.Switch, Gtk.Entry)) and w.get_can_focus():
            out.append(w)
            return
        if isinstance(w, Gtk.Container):
            w.foreach(walk)
    walk(root)
    return out


def rect(widget: Gtk.Widget, window: Gtk.Window) -> Optional[Rect]:
    at = widget.translate_coordinates(window, 0, 0)
    if at is None:
        return None
    return at[0], at[1], widget.get_allocated_width(), widget.get_allocated_height()


def pick(window: Gtk.Window, candidates: Sequence[Gtk.Widget], frm: Union[Gtk.Widget, Rect],
         way: str) -> Optional[Gtk.Widget]:
    """The candidate nearest frm in that direction: ahead of it, and the
    least sideways - sideways counts more across a row (left / right stay
    in it) than down a column."""
    here = rect(frm, window) if isinstance(frm, Gtk.Widget) else frm
    if here is None:
        return None
    hx, hy, hw, hh = here
    cx, cy = hx + hw / 2, hy + hh / 2
    best, best_score = None, None
    for w in candidates:
        if w is frm:
            continue
        r = rect(w, window)
        if r is None:
            continue
        x, y, ww, wh = r
        if way == "down":
            along, across, overlap = y - (hy + hh), abs(x + ww / 2 - cx), min(x + ww, hx + hw) - max(x, hx)
        elif way == "up":
            along, across, overlap = hy - (y + wh), abs(x + ww / 2 - cx), min(x + ww, hx + hw) - max(x, hx)
        elif way == "right":
            along, across, overlap = x - (hx + hw), abs(y + wh / 2 - cy), min(y + wh, hy + hh) - max(y, hy)
        else:
            along, across, overlap = hx - (x + ww), abs(y + wh / 2 - cy), min(y + wh, hy + hh) - max(y, hy)
        if along < -4:          # not ahead (a little overlap is fine: rows of uneven height)
            continue
        if way in ("left", "right"):
            # the same row first; another row only as a last resort
            score = (0 if overlap > 0 else 1, max(along, 0) + 2 * across)
        else:
            score = (0, max(along, 0) + (0 if overlap > 0 else 1.5 * across))
        if best_score is None or score < best_score:
            best, best_score = w, score
    return best


def first(window: Gtk.Window, candidates: Sequence[Gtk.Widget]) -> Optional[Gtk.Widget]:
    """Where a page is entered (from the sidebar, on its left): near its top
    left - a header's button far right (Refresh) comes after what's under
    it on the left (the Bounty Board's store chips)."""
    placed = [(r[0] + 2 * r[1], w) for w in candidates for r in [rect(w, window)] if r is not None]
    return min(placed, key=lambda t: t[0])[1] if placed else None


def stand(window: Gtk.Window, widget: Gtk.Widget):
    """Focus on widget, its ring on (the app hides GTK's own rings), the
    page scrolled to show it."""
    old = getattr(window, "_spatial_ring", None)
    if old is not None and old is not widget:
        old.get_style_context().remove_class("kb-focus")
    widget.grab_focus()
    widget.get_style_context().add_class("kb-focus")
    window._spatial_ring = widget
    if not getattr(window, "_spatial_watch", False):
        # the ring goes with the focus (a click, the pointer, another key's move)
        window._spatial_watch = True
        window.connect("set-focus", _focus_moved)
    into_view(widget)


def _focus_moved(window, focus):
    ringed = getattr(window, "_spatial_ring", None)
    if ringed is not None and focus is not ringed:
        ringed.get_style_context().remove_class("kb-focus")
        window._spatial_ring = None


def into_view(widget: Gtk.Widget):
    scroller = widget.get_ancestor(Gtk.ScrolledWindow)
    if scroller is None or scroller.get_child() is None:
        return
    pos = widget.translate_coordinates(scroller.get_child(), 0, 0)
    if pos is None:
        return
    adj = scroller.get_vadjustment()
    top, bottom = pos[1] - 12, pos[1] + widget.get_allocated_height() + 12
    if top < adj.get_value():
        adj.set_value(max(adj.get_lower(), top))
    elif bottom > adj.get_value() + adj.get_page_size():
        adj.set_value(min(adj.get_upper() - adj.get_page_size(), bottom - adj.get_page_size()))
