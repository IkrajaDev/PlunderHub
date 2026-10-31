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
"""Where the keyboard or a controller stands, shown.

The app hides GTK's own focus rings (a click mustn't leave one behind),
so a window driven by the arrows or a pad marks its focus itself: a
setting's whole row lights up as in Ship's Controls (.settings-row-
focused), any other control gets a ring (.kb-focus). A mouse click puts
the marks away until the next arrow or pad press.

    keynav.track(window)            once, for a dialog
    keynav.track(popover, window)   a popover, inside window
    keynav.on(widget)               on an arrow / pad press
"""

from typing import Optional

from plunderhub.gi_setup import GLib, Gtk

# the rows that light up whole (a setting's row in the game page's
# Launch options)
ROW_CLASSES = ("gp-setting",)
# what shows its focus itself already (a Shipwright tile's border, a
# sidebar row's inset line)
OWN_LOOK = ("sw-tile",)


def track(root: Gtk.Widget, window: Optional[Gtk.Window] = None):
    """Marks the focus inside root (a dialog, or a popover in window)."""
    window = window or root
    state = {"on": False, "marked": []}
    root._plunderhub_keynav = state
    ids = [window.connect("set-focus", lambda *_: GLib.idle_add(_mark, root)),
           window.connect("button-press-event", lambda *_: (_set(root, False), False)[1])]
    if window is not root:
        # a popover's handlers live on a window that outlives it
        root.connect("destroy", lambda *_: [window.disconnect(i) for i in ids if window.handler_is_connected(i)])
    if isinstance(root, Gtk.Popover):
        root.connect("closed", lambda *_: _set(root, False))


def on(widget: Gtk.Widget):
    """An arrow or pad press in widget's tracked root: show the marks."""
    root = _root_of(widget)
    if root is not None and not root._plunderhub_keynav["on"]:
        _set(root, True)


def _root_of(widget: Optional[Gtk.Widget]):
    while widget is not None:
        if getattr(widget, "_plunderhub_keynav", None) is not None:
            return widget
        widget = widget.get_parent()
    return None


def remark(root):
    """The marks again (a window changed how it wants them)."""
    _mark(root)


def _set(root, on_: bool):
    root._plunderhub_keynav["on"] = on_
    _mark(root)


def _mark(root) -> bool:
    state = root._plunderhub_keynav
    for widget, cls in state["marked"]:
        widget.get_style_context().remove_class(cls)
    state["marked"] = []
    top = root.get_toplevel()
    focus = top.get_focus() if isinstance(top, Gtk.Window) else None
    if not state["on"] or focus is None or not (focus is root or focus.is_ancestor(root)):
        return False
    row = focus
    while row is not None and row is not root:
        if any(row.get_style_context().has_class(c) for c in ROW_CLASSES):
            break
        row = row.get_parent()
    if row is not None and row is not root and getattr(root, "plunderhub_inside_row", False):
        # inside a row of several controls: the row lit, and the one stood on ringed
        row.get_style_context().add_class("settings-row-focused")
        state["marked"].append((row, "settings-row-focused"))
        target, cls = focus, "kb-focus"
    elif row is not None and row is not root:
        target, cls = row, "settings-row-focused"
    elif isinstance(focus, Gtk.ListBoxRow) or any(focus.get_style_context().has_class(c) for c in OWN_LOOK):
        return False
    else:
        target, cls = focus, "kb-focus"
    target.get_style_context().add_class(cls)
    state["marked"].append((target, cls))
    return False


# --- the pointer takes over from the keyboard ---------------------------------------

# What a mouse move puts the keyboard's focus away from: things that
# light up under the pointer too - a second, keyboard-lit one beside the
# hovered one looked as if two were picked. Not text fields (typing
# goes on), nor what needs the focus for its own keys (the dashboard).
_POINTER_DROPS = (Gtk.Button, Gtk.ListBoxRow, Gtk.FlowBoxChild, Gtk.Switch)
_POINTER_SLACK = 12     # px the pointer may drift (a hand on the desk) before it counts


def focus_changed(window: Gtk.Window, widget: Optional[Gtk.Widget]):
    """gi_setup's set-focus hook on every window: a new focus starts
    watching for the pointer afresh."""
    window._plunderhub_pointer_anchor = None
    window._plunderhub_kb_focus = widget is not None


def pointer_moved(window: Gtk.Window, x: float, y: float):
    """gi_setup's motion hook on every window: once the pointer has
    really moved since the focus last changed, the keyboard's focus goes
    (a popover's stays - it would close - only its mark goes)."""
    if not getattr(window, "_plunderhub_kb_focus", False):
        return
    anchor = getattr(window, "_plunderhub_pointer_anchor", None)
    if anchor is None:
        window._plunderhub_pointer_anchor = (x, y)
        return
    if abs(x - anchor[0]) + abs(y - anchor[1]) <= _POINTER_SLACK:
        return
    window._plunderhub_kb_focus = False
    focus = window.get_focus()
    if focus is None or not isinstance(focus, _POINTER_DROPS):
        return
    popover = focus.get_ancestor(Gtk.Popover)
    if popover is not None:
        root = _root_of(focus)
        if root is not None:
            _set(root, False)
        return
    window._plunderhub_kb_last = focus
    window.set_focus(None)
    root = _root_of(focus)
    if root is not None:
        _set(root, False)


def restore(window: Gtk.Window) -> bool:
    """The first arrow or pad press after the pointer took over: back
    where the keyboard was (True), not on to the next thing."""
    last = getattr(window, "_plunderhub_kb_last", None)
    window._plunderhub_kb_last = None
    if window.get_focus() is not None or last is None or not last.get_mapped() or not last.is_sensitive():
        return False
    last.grab_focus()
    on(last)
    return True


# --- out of a list at its ends -----------------------------------------------------------

def _focusables(widget: Gtk.Widget, out: list):
    if not (widget.get_visible() and widget.get_mapped() and widget.is_sensitive()):
        return out
    if isinstance(widget, Gtk.ComboBox) or (widget.get_can_focus() and not isinstance(
            widget, (Gtk.Label, Gtk.ScrolledWindow, Gtk.Viewport, Gtk.Window))):
        out.append(widget)
        return out
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            _focusables(child, out)
    return out


def escape_list(window: Gtk.Window, direction: str) -> bool:
    """Down on a list's last row (Up on its first): onto the nearest
    control under (over) the list - GTK's list kept the focus at its
    ends, and the buttons under it couldn't be reached."""
    if direction not in ("up", "down"):
        return False
    focus = window.get_focus()
    row = focus if isinstance(focus, Gtk.ListBoxRow) else (
        focus.get_ancestor(Gtk.ListBoxRow) if focus is not None else None)
    if row is None or not isinstance(row.get_parent(), Gtk.ListBox):
        return False
    rows = [r for r in row.get_parent().get_children()
            if r.get_visible() and r.get_child_visible() and r.get_can_focus() and r.is_sensitive()]
    if not rows or row is not (rows[-1] if direction == "down" else rows[0]):
        return False
    area = row.get_parent().get_ancestor(Gtk.ScrolledWindow) or row.get_parent()
    at = area.translate_coordinates(window, 0, 0)
    if not at:
        return False
    top, bottom = at[-1], at[-1] + area.get_allocated_height()
    left = at[-2]
    best = None
    for widget in _focusables(window, []):
        if widget is area or widget.is_ancestor(area):
            continue
        xy = widget.translate_coordinates(window, 0, 0)
        if not xy:
            continue
        wy, wx, wh = xy[-1], xy[-2], widget.get_allocated_height()
        if direction == "down" and wy < bottom - 1 or direction == "up" and wy + wh > top + 1:
            continue
        score = (abs(wy - bottom) if direction == "down" else abs(top - (wy + wh)), abs(wx - left))
        if best is None or score < best[0]:
            best = (score, widget)
    if best is None:
        return False
    best[1].grab_focus()
    return True
