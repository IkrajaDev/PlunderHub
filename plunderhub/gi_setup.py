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


import os

import cairo
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Gio', '2.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, GLib, Gdk, GdkPixbuf, Gio, GObject, Pango

if os.environ.pop("PLUNDERHUB_X11_ON_GNOME", None):
    # GTK is up on XWayland (plunderhub.py): what PlunderHub starts runs
    # the way it would without it
    os.environ.pop("GDK_BACKEND", None)

__all__ = [
    "Gtk", "GLib", "Gdk", "GdkPixbuf", "Gio", "Pango",
    "AppIndicator3", "_INDICATOR_BACKEND", "image_surface",
]


def image_surface(window, fmt: int, width: int, height: int, scale: int = 0) -> cairo.ImageSurface:
    """What Gdk.Window.create_similar_image_surface gives (width and height
    in device pixels, the device scale set), made with pycairo alone:
    PyGObject 3.48 (Ubuntu 24.04, Mint 22) fails on that call with
    "ValueError: invalid enum value: 5" building the cairo.Format enum."""
    if not scale:
        scale = window.get_scale_factor() if window is not None else 1
    surface = cairo.ImageSurface(fmt, max(1, int(width)), max(1, int(height)))
    surface.set_device_scale(scale, scale)
    return surface


def _scroll_passes_through(widget, _event) -> bool:
    """The wheel over a drop-down, slider or number field doesn't change
    it - too easy to alter a setting by accident while scrolling the
    page. The event goes on to the page, which scrolls instead."""
    widget.stop_emission_by_name("scroll-event")
    return False


def _scrollbar_steps(bar, event) -> bool:
    """The wheel over a scrollbar moves the view as it does over the
    content: a step per notch. GTK3's scrollbar glides each step over
    several frames instead, every one repainting the window - ~150
    repaints a second while scrolling there, ~90 % CPU."""
    adj = bar.get_adjustment()
    ok, dx, dy = event.get_scroll_deltas()
    if not ok:
        ok, direction = event.get_scroll_direction()
        steps = {Gdk.ScrollDirection.UP: -1, Gdk.ScrollDirection.LEFT: -1,
                 Gdk.ScrollDirection.DOWN: 1, Gdk.ScrollDirection.RIGHT: 1}
        if not ok or direction not in steps:
            return False
        dx = dy = steps[direction]
    vertical = bar.get_orientation() == Gtk.Orientation.VERTICAL
    delta = (dy or dx) if vertical else (dx or dy)
    # GTK's own step over the content
    step = adj.get_page_size() ** (2 / 3)
    adj.set_value(max(adj.get_lower(), min(adj.get_upper() - adj.get_page_size(), adj.get_value() + delta * step)))
    return True


def _pointer_moved(window, event) -> bool:
    """GTK folds a burst of pointer motion into one event only while a
    frame is being painted. With nothing else moving, a 1000 Hz mouse was
    handled event by event - half a core just for moving it over a window
    (the game page: 52 % -> 21 %). So while the pointer moves, a pixel of
    the window is repainted every frame."""
    window._plunderhub_moved_us = GLib.get_monotonic_time()
    # the pointer taking over from the keyboard (ui/keynav)
    from plunderhub.ui import keynav
    keynav.pointer_moved(window, event.x_root, event.y_root)
    if getattr(window, "_plunderhub_motion_paused", False):
        return False            # perf_guard measuring the window's own repaints
    if not getattr(window, "_plunderhub_motion_tick", 0):
        window._plunderhub_motion_tick = window.add_tick_callback(_motion_frame)
    return False


def _focus_set(window, widget) -> bool:
    from plunderhub.ui import keynav
    keynav.focus_changed(window, widget)
    return False


def _list_arrows(window, event) -> bool:
    if event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
        return False
    direction = {Gdk.KEY_Up: "up", Gdk.KEY_Down: "down"}.get(event.keyval)
    if direction is None:
        return False
    from plunderhub.ui import keynav
    return keynav.escape_list(window, direction)


def _motion_frame(window, clock) -> bool:
    if (clock.get_frame_time() - window._plunderhub_moved_us > 150_000
            or getattr(window, "_plunderhub_motion_paused", False)):
        window._plunderhub_motion_tick = 0
        return False
    window.queue_draw_area(0, 0, 1, 1)
    return True


class _PictureFade:
    """A Gtk.Stack's crossfade drawn from two pictures of its pages. GTK3's
    own draws the whole new page again on every frame of the fade - about
    14 ms a frame even for a page that draws in 0.3 ms: paging through
    Settings took 37 % CPU, the game page's tabs 56 %. Put on every stack
    set to CROSSFADE, when it's realized (see _on_realize); its page
    changes from Python fade this way, at ~60 fps."""

    LENGTH_US = 150_000

    def __init__(self, stack: Gtk.Stack):
        self.stack = stack
        self.fade = None
        self.beat = 1
        stack.set_transition_type(Gtk.StackTransitionType.NONE)
        stack.connect("draw", self._draw)
        # the instance's own methods: what the app's code calls
        stack.set_visible_child = self.set_visible_child
        stack.set_visible_child_name = self.set_visible_child_name

    def set_visible_child_name(self, name: str):
        child = self.stack.get_child_by_name(name)
        if child is None:
            Gtk.Stack.set_visible_child_name(self.stack, name)
        else:
            self.set_visible_child(child)

    def set_visible_child(self, child: Gtk.Widget):
        stack = self.stack
        old = stack.get_visible_child()
        settings = Gtk.Settings.get_default()
        if (child is old or old is None or not child.get_visible() or not stack.get_mapped()
                or stack.get_window() is None or not (settings and settings.props.gtk_enable_animations)):
            Gtk.Stack.set_visible_child(stack, child)
            return
        first = self.fade is None
        self.fade = {"old": self._picture(old), "new": None, "start": None, "t": 0.0}
        Gtk.Stack.set_visible_child(stack, child)
        if first:
            self.beat = self._beat_frames()
            stack.add_tick_callback(self._step)

    def _beat_frames(self) -> int:
        """Every how many frames a step comes: the whole number nearest
        60 fps, as the app's other animations (App._bubble_beat_frames)."""
        window = self.stack.get_window()
        monitor = window.get_display().get_monitor_at_window(window) if window is not None else None
        rate = monitor.get_refresh_rate() / 1000 if monitor is not None else 0
        return max(1, round(rate / 60)) if rate > 0 else 1

    def _picture(self, page: Gtk.Widget) -> cairo.ImageSurface:
        """The page as the stack shows it, at the screen's own pixels."""
        stack = self.stack
        scale = stack.get_scale_factor()
        surface = image_surface(
            stack.get_window(), cairo.FORMAT_ARGB32, max(1, stack.get_allocated_width()) * scale,
            max(1, stack.get_allocated_height()) * scale, scale)
        stack.propagate_draw(page, cairo.Context(surface))
        return surface

    def _step(self, stack: Gtk.Stack, clock) -> bool:
        fade = self.fade
        if fade is None:
            return False
        now = clock.get_frame_time()
        if fade["start"] is None:
            fade["start"] = now
        fade["t"] = min(1.0, (now - fade["start"]) / self.LENGTH_US)
        if fade["t"] >= 1.0:
            self.fade = None
        elif clock.get_frame_counter() % self.beat:
            return True
        stack.queue_draw()
        return self.fade is not None

    def _draw(self, stack: Gtk.Stack, cr) -> bool:
        fade = self.fade
        if fade is None or stack.get_visible_child() is None:
            return False
        if fade["new"] is None:
            # laid out by now: drawing comes after the layout of the frame
            fade["new"] = self._picture(stack.get_visible_child())
        cr.set_source_surface(fade["old"], 0, 0)
        cr.paint_with_alpha(1.0 - fade["t"])
        cr.set_source_surface(fade["new"], 0, 0)
        cr.paint_with_alpha(fade["t"])
        return True


def _drop_double_close(window: Gtk.Window) -> bool:
    """A window the window manager frames (KDE, GNOME, Cinnamon...: a title
    bar with its own close button) doesn't show the app's top-right X as
    well. Frameless card windows keep theirs - it's their only one - and so
    do tiling window managers, which frame nothing."""
    from plunderhub.core.desktop import is_tiling_window_manager
    if not window.get_decorated() or window.get_titlebar() is not None or is_tiling_window_manager():
        return False

    def walk(widget):
        if isinstance(widget, Gtk.Button) and widget.get_style_context().has_class("about-close"):
            widget.set_no_show_all(True)
            widget.hide()
        elif isinstance(widget, Gtk.Container):
            widget.forall(walk)
    walk(window)
    return False


def _on_realize(widget, *_args) -> bool:
    # On realize, however the widget was made (a constructor like
    # Scale.new_with_range never runs a Python __init__).
    if getattr(widget, "_plunderhub_hooked", False):
        return True
    if isinstance(widget, Gtk.Window):
        widget._plunderhub_hooked = True
        widget.add_events(Gdk.EventMask.POINTER_MOTION_MASK)
        widget.connect("motion-notify-event", _pointer_moved)
        widget.connect("set-focus", _focus_set)
        # when it's shown, not now: a window realized early gets its
        # content (and its X) only afterwards
        widget.connect("map", _drop_double_close)
        if isinstance(widget, Gtk.Dialog):
            # out of a list at its ends with the arrows (dialogs of the
            # app's own with their own keys handle them first)
            widget.connect("key-press-event", _list_arrows)
    elif isinstance(widget, Gtk.Stack):
        widget._plunderhub_hooked = True
        if widget.get_transition_type() == Gtk.StackTransitionType.CROSSFADE:
            widget._plunderhub_fade = _PictureFade(widget)
    elif isinstance(widget, Gtk.Scrollbar):
        widget._plunderhub_hooked = True
        widget.connect("scroll-event", _scrollbar_steps)
    elif isinstance(widget, (Gtk.ComboBox, Gtk.Range, Gtk.SpinButton)):
        widget._plunderhub_hooked = True
        widget.connect("scroll-event", _scroll_passes_through)
    return True


# A label given the same text again still lays its window out again - and
# a status line refreshed every second is easily written that way. Every
# label in the app skips an unchanged text.
_label_set_text = Gtk.Label.set_text
_label_set_markup = Gtk.Label.set_markup


def _set_text(label, text):
    if label.get_use_markup() or label.get_use_underline() or label.get_label() != text:
        _label_set_text(label, text)


def _set_markup(label, markup):
    if not label.get_use_markup() or label.get_label() != markup:
        _label_set_markup(label, markup)


Gtk.Label.set_text = _set_text
Gtk.Label.set_markup = _set_markup


# A frameless window (a card: Report a bug, a task, Shipwright) still got a
# title bar on KDE Plasma (Wayland): KWin decorates a GTK3 window that
# doesn't announce client-side decorations, set_decorated(False) or not -
# two close buttons, the card's own and KWin's. An empty title bar of its
# own (as the main window has) is that announcement: frameless on every
# desktop. Wayland only - X11 window managers keep to set_decorated.
_window_set_decorated = Gtk.Window.set_decorated


def _set_decorated(window, setting):
    _window_set_decorated(window, setting)
    if (not setting and window.get_titlebar() is None and not window.get_realized()
            and Gdk.Display.get_default().__gtype__.name == "GdkWaylandDisplay"):
        stub = Gtk.Box()
        stub.get_style_context().add_class("home-titlebar-stub")
        window.set_titlebar(stub)


Gtk.Window.set_decorated = _set_decorated


# Without a display (a TTY, SSH, `plunderhub --version`) GTK isn't set up
# yet and Gtk.Widget's class - its signals - isn't made till something asks.
GObject.type_class_ref(Gtk.Widget.__gtype__)
GObject.add_emission_hook(Gtk.Widget, "realize", _on_realize)

AppIndicator3 = None
_INDICATOR_BACKEND = None
try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
    _INDICATOR_BACKEND = "ayatana"
except (ValueError, ImportError):
    try:
        gi.require_version('AppIndicator3', '0.1')
        from gi.repository import AppIndicator3
        _INDICATOR_BACKEND = "appindicator3"
    except (ValueError, ImportError):
        AppIndicator3 = None
        _INDICATOR_BACKEND = None
