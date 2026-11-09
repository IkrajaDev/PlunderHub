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
"""Checks for the GTK3 traps that made windows slow, run by the self-test
(selftest.py) so a new button or animation can't bring one back unseen:

- hovering a button, row or tile must not lay its whole window out again
  (a Gtk.Image in it with an icon effect, a fading text colour...);
- a window left alone must not keep repainting (an endless fade or tick).

Both work on the real widgets, with the state GTK itself gives them under
the pointer, and name the widget that failed.
"""

import time
from typing import Iterator, List

from plunderhub.gi_setup import Gtk

_HOVERABLE = (Gtk.Button, Gtk.ListBoxRow, Gtk.FlowBoxChild)


def _descendants(root: Gtk.Widget) -> Iterator[Gtk.Widget]:
    """Every widget under root, the internal ones too (scrollbars...)."""
    stack = [root]
    while stack:
        widget = stack.pop()
        yield widget
        if isinstance(widget, Gtk.Container):
            kids: List[Gtk.Widget] = []
            widget.forall(lambda child, *_: kids.append(child), None)
            stack.extend(reversed(kids))


def _describe(widget: Gtk.Widget) -> str:
    classes = ".".join(widget.get_style_context().list_classes())
    text = ""
    for inner in _descendants(widget):
        if isinstance(inner, Gtk.Label) and inner.get_text().strip():
            text = inner.get_text().strip()[:30]
            break
    name = type(widget).__name__ + (f".{classes}" if classes else "")
    return f"{name} '{text}'" if text else name


def settle(window: Gtk.Widget, frames: int = 1, limit_ms: int = 250) -> int:
    """Lets GTK run until `window` has painted `frames` more frames (or the
    limit has passed); returns how many it painted."""
    clock = window.get_frame_clock()
    painted = [0]
    handler = clock.connect("after-paint", lambda *_: painted.__setitem__(0, painted[0] + 1)) if clock else None
    window.queue_draw_area(0, 0, 1, 1)
    end = time.monotonic() + limit_ms / 1000
    while painted[0] < frames and time.monotonic() < end:
        if not Gtk.main_iteration_do(False):
            time.sleep(0.002)
    if handler is not None:
        clock.disconnect(handler)
    return painted[0]


def allow_hover_relayout(widget: Gtk.Widget, reason: str) -> Gtk.Widget:
    """Marks a widget whose hover may lay its window out once, on purpose:
    its text changes colour (GTK3 lays a label out again for any colour
    change) - a filled button that needs dark text on it, say. The
    check passes it over; anything new is still caught."""
    widget.plunderhub_hover_relayout = reason
    return widget


def _relayouts_on_hover(window: Gtk.Widget, widget: Gtk.Widget, count: List[int]) -> bool:
    """One try: True when hovering widget laid the window out and a
    moment with nothing done just before didn't (a timer of the app's
    own, say, isn't the widget's fault)."""
    settle(window)
    count[0] = 0
    settle(window, 2)
    if count[0]:
        return False                # busy anyway: no verdict
    widget.set_state_flags(Gtk.StateFlags.PRELIGHT, False)
    settle(window, 2)
    widget.unset_state_flags(Gtk.StateFlags.PRELIGHT)
    settle(window, 2)
    return count[0] > 0


def hover_relayouts(window: Gtk.Widget, root: Gtk.Widget = None) -> List[str]:
    """The buttons, rows and tiles under root (default: all of window)
    whose hover lays the window out again - twice running, to be sure."""
    count = [0]
    handler = window.connect("size-allocate", lambda *_: count.__setitem__(0, count[0] + 1))
    offenders: List[str] = []
    try:
        settle(window, 2)
        for widget in list(_descendants(root or window)):
            if (not isinstance(widget, _HOVERABLE) or not widget.get_mapped() or not widget.is_sensitive()
                    or getattr(widget, "plunderhub_hover_relayout", None)):
                continue
            if _relayouts_on_hover(window, widget, count) and _relayouts_on_hover(window, widget, count):
                offenders.append(_describe(widget))
    finally:
        window.disconnect(handler)
    return offenders


def repaints_when_idle(window: Gtk.Widget, seconds: float = 1.0, tries: int = 4) -> int:
    """How many frames window paints in `seconds` with nothing happening
    (after half a second for what was going on to finish - a page's fade).
    The real pointer moving over the window paints a pixel every frame on
    purpose (gi_setup._pointer_moved) - someone watching the self-test did
    that and it failed: that is paused while measuring, and a second the
    pointer moved in (hovers light up) is measured again."""
    window._plunderhub_motion_paused = True
    try:
        settle(window, 3, 500)
        end = time.monotonic() + 0.5
        while time.monotonic() < end:
            if not Gtk.main_iteration_do(False):
                time.sleep(0.005)
        clock = window.get_frame_clock()
        if clock is None:
            return 0
        for _attempt in range(tries):
            moved_before = getattr(window, "_plunderhub_moved_us", 0)
            painted = [0]
            handler = clock.connect("after-paint", lambda *_: painted.__setitem__(0, painted[0] + 1))
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if not Gtk.main_iteration_do(False):
                    time.sleep(0.005)
            clock.disconnect(handler)
            if getattr(window, "_plunderhub_moved_us", 0) == moved_before:
                break
        return painted[0]
    finally:
        window._plunderhub_motion_paused = False


def assert_calm(window: Gtk.Widget, what: str, root: Gtk.Widget = None, idle: bool = True):
    """Fails with the offenders' names when hovering relayouts the window
    or, with idle, when it keeps repainting on its own. Measured on the
    real desktop, where the player's pointer or a window closing can land
    in one pass: only what shows twice in a row counts."""
    problems = []
    for _attempt in range(2):
        problems = []
        offenders = hover_relayouts(window, root)
        if offenders:
            problems.append(f"hover lays the whole {what} out again: " + ", ".join(sorted(set(offenders))[:8]))
        if idle:
            frames = repaints_when_idle(window)
            # a one-off update is fine; something animating paints every frame
            if frames > 10:
                problems.append(f"the {what} keeps repainting when left alone ({frames} frames a second)")
        if not problems:
            return
    assert not problems, "; ".join(problems)

