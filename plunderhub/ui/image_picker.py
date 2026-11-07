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

"""Choosing a cover, hero or icon from the computer: the pictures in the
usual folders as previews at the art's own shape, a drop target for a
file dragged in from anywhere, and the system's file dialog for the rest.
Looks like the SteamGridDB picker (game_page.ArtPicker) next to it."""

import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

import cairo

from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango
from plunderhub.logging_setup import logger
from plunderhub.ui.game_page import _PREVIEW_POOL, _THUMB, _decode_preview
from plunderhub.ui.home_mixin import _accent_rgb, _rounded

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico", ".avif")
MAX_SHOWN = 120             # newest files per place
# What SteamGridDB serves, so what the app is laid out for.
BEST_SIZE = {"cover": "600 × 900", "hero": "1920 × 620", "icon": "256 × 256"}


def _places() -> List[Tuple[Optional[str], Path]]:
    """(label, folder) of the folders worth looking in, in this order -
    the folders' own names are already in the system's language; Home's
    label (None) comes from the app's strings."""
    special = {
        # the usual English names when user-dirs.dirs doesn't say
        name: Path(GLib.get_user_special_dir(kind) or Path.home() / name)
        for kind, name in ((GLib.UserDirectory.DIRECTORY_PICTURES, "Pictures"),
                           (GLib.UserDirectory.DIRECTORY_DOWNLOAD, "Downloads"),
                           (GLib.UserDirectory.DIRECTORY_DESKTOP, "Desktop"),
                           (GLib.UserDirectory.DIRECTORY_DOCUMENTS, "Documents"))
    }
    home = Path.home()
    wanted = [special["Pictures"], special["Downloads"], home, special["Desktop"], special["Documents"]]
    seen, places = set(), []
    for path in wanted:
        # an unset special folder can point at home itself - once is enough
        if path.is_dir() and path not in seen:
            seen.add(path)
            places.append((None if path == home else path.name, path))
    return places


def _images_in(folders: List[Path]) -> List[Path]:
    """The pictures right in these folders, newest first."""
    files = []
    for folder in folders:
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name.lower().endswith(IMAGE_SUFFIXES) and entry.is_file():
                        files.append((entry.stat().st_mtime, Path(entry.path)))
        except OSError as e:
            logger.debug(f"Image picker couldn't list {folder}: {e}")
    files.sort(key=lambda f: -f[0])
    return [path for _mtime, path in files[:MAX_SHOWN]]


class _FileTile(Gtk.Box):
    """A preview at the art's shape, the file name under it."""

    def __init__(self, picker: "ImageFilePicker", path: Path):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.picker, self.path = picker, path
        self.kind = picker.kind
        self.w, self.h = _THUMB[self.kind]
        self.radius = 14 if self.kind == "icon" else 10
        self.surface = None
        self.size: Optional[Tuple[int, int]] = None
        self.loading = True
        self.failed = False
        self.selected = False
        self.hover = False
        self.generation = picker.generation

        self.area = Gtk.DrawingArea()
        self.area.set_size_request(self.w, self.h)
        self.area.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
                             | Gdk.EventMask.BUTTON_PRESS_MASK)
        self.area.connect("draw", self._draw)
        self.area.connect("enter-notify-event", lambda *_: self._set_hover(True))
        self.area.connect("leave-notify-event", lambda *_: self._set_hover(False))
        self.area.connect("button-press-event", self._on_press)
        self.pack_start(self.area, False, False, 0)
        name = Gtk.Label(label=path.name)
        name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name.set_max_width_chars(1)
        name.set_size_request(self.w, -1)
        name.get_style_context().add_class("gp-note")
        name.get_style_context().add_class("up-name")
        self.pack_start(name, False, False, 0)
        self.set_tooltip_text(str(path))
        _PREVIEW_POOL.submit(self._load, self.generation)

    def _load(self, generation: int):
        surface, size = None, None
        try:
            if generation == self.picker.generation:
                _fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(str(self.path))
                size = (w, h) if w and h else None
                frames, _still = _decode_preview(self.path, self.w, self.h, self.kind in ("hero", "cover"), self.radius, False)
                surface = frames[0][0] if frames else None
        except Exception as e:
            logger.debug(f"Image picker couldn't read {self.path}: {e}")
        GLib.idle_add(self._loaded, generation, surface, size)

    def _loaded(self, generation: int, surface, size) -> bool:
        if generation != self.picker.generation or self.get_window() is None:
            return False
        self.loading = False
        self.surface, self.size = surface, size
        self.failed = surface is None
        if self.failed:
            self.set_sensitive(False)
        self.area.queue_draw()
        if self.picker.selected is self:
            self.picker.update_info()
        return False

    def _set_hover(self, on: bool) -> bool:
        self.hover = on
        self.area.queue_draw()
        return False

    def _on_press(self, _w, event) -> bool:
        if self.failed:
            return True
        self.picker.choose(self)
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self.picker.apply()
        return True

    def set_selected(self, on: bool):
        self.selected = on
        self.area.queue_draw()

    def _draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        x, y, tw, th = (w - self.w) / 2, (h - self.h) / 2, self.w, self.h
        r = self.radius
        # the art's own shape behind it, so an image of another shape
        # shows how it sits in the slot
        _rounded(cr, x, y, tw, th, r)
        cr.set_source_rgba(1, 1, 1, 0.05)
        cr.fill()
        if self.loading:
            sweep = (time.monotonic() * 400) % (tw * 2.5) - tw
            g = cairo.LinearGradient(x + sweep, y, x + sweep + tw * 0.6, y + th)
            g.add_color_stop_rgba(0, 1, 1, 1, 0)
            g.add_color_stop_rgba(0.5, 1, 1, 1, 0.07)
            g.add_color_stop_rgba(1, 1, 1, 1, 0)
            _rounded(cr, x, y, tw, th, r)
            cr.set_source(g)
            cr.fill()
        elif self.surface is not None:
            sw, sh = self.surface.get_width(), self.surface.get_height()
            cr.set_source_surface(self.surface, (w - sw) / 2, (h - sh) / 2)
            cr.paint()
        if self.hover and not self.selected and not self.loading:
            _rounded(cr, x, y, tw, th, r)
            cr.set_source_rgba(1, 1, 1, 0.08)
            cr.fill()
        if self.selected:
            _rounded(cr, x + 1.5, y + 1.5, tw - 3, th - 3, r - 1)
            cr.set_source_rgb(*_accent_rgb(widget))
            cr.set_line_width(3)
            cr.stroke()
        return False


class ImageFilePicker(Gtk.Dialog):
    """Returns the chosen file's path from .path, None when cancelled."""

    def __init__(self, app, kind: str, subtitle: str, parent: Gtk.Window):
        super().__init__(transient_for=parent, modal=True)
        self.app, self.kind = app, kind
        self._ = _ = app._
        self.path: Optional[str] = None
        self.selected: Optional[_FileTile] = None
        self.generation = 0
        self.set_decorated(False)
        pw, ph = parent.get_size()
        # a size request, not a default: a window manager remembering
        # dialogs by title would bring back an old, small one
        self.set_size_request(max(720, min(1100, pw - 40)), max(520, min(760, ph - 40)))
        self.get_style_context().add_class("gp-window")
        action_area = self.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()

        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.root.get_style_context().add_class("up-root")
        head = Gtk.Box(spacing=14)
        head.set_border_width(20)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title = Gtk.Label(label=_("up_title_" + kind), xalign=0)
        title.get_style_context().add_class("home-dialog-title")
        sub = Gtk.Label(label=subtitle, xalign=0)
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        sub.get_style_context().add_class("gp-note")
        titles.pack_start(title, False, False, 0)
        titles.pack_start(sub, False, False, 0)
        head.pack_start(titles, True, True, 0)
        browse = app._dialog_button(_("up_browse"), "ghost")
        browse.set_valign(Gtk.Align.CENTER)
        browse.connect("clicked", lambda *_: self._browse())
        head.pack_end(browse, False, False, 0)
        self.root.pack_start(head, False, False, 0)

        self.places = _places()
        chips = Gtk.Box(spacing=8)
        chips.set_margin_start(20)
        chips.set_margin_bottom(10)
        first = None
        for label, folder in self.places:
            rb = Gtk.RadioButton.new_with_label_from_widget(first, label or _("up_home"))
            rb.set_mode(False)
            rb.get_style_context().add_class("gp-chip")
            first = first or rb
            rb.connect("toggled", lambda b, f=folder: b.get_active() and self._show([f]))
            chips.pack_start(rb, False, False, 0)
        self.root.pack_start(chips, False, False, 0)

        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_homogeneous(True)
        self.flow.set_max_children_per_line(12)
        self.flow.set_column_spacing(14)
        self.flow.set_row_spacing(14)
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_border_width(20)
        self.empty = Gtk.Label(label=_("up_empty"))
        self.empty.set_line_wrap(True)
        self.empty.set_justify(Gtk.Justification.CENTER)
        self.empty.get_style_context().add_class("gp-note")
        self.empty.set_no_show_all(True)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.pack_start(self.flow, False, False, 0)
        body.pack_start(self.empty, True, True, 40)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_overlay_scrolling(False)
        scroller.get_style_context().add_class("settings-scroll")
        scroller.add(body)
        self.root.pack_start(scroller, True, True, 0)

        foot = Gtk.Box(spacing=10)
        foot.get_style_context().add_class("gp-footer")
        self.info = Gtk.Label(xalign=0)
        self.info.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.info.set_max_width_chars(1)
        self.info.get_style_context().add_class("gp-note")
        foot.pack_start(self.info, True, True, 0)
        cancel = app._dialog_button(_("dialog_cancel"), "ghost")
        cancel.connect("clicked", lambda *_: self.response(Gtk.ResponseType.CANCEL))
        self.apply_btn = app._dialog_button(_("up_use"), "accent")
        self.apply_btn.connect("clicked", lambda *_: self.apply())
        foot.pack_end(self.apply_btn, False, False, 0)
        foot.pack_end(cancel, False, False, 0)
        self.root.pack_start(foot, False, False, 0)
        self.get_content_area().set_border_width(0)
        self.get_content_area().pack_start(self.root, True, True, 0)

        # a file dragged in from a file manager or a browser
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._on_drop)
        self.connect("drag-motion", lambda *_: self._drop_hint(True) or False)
        self.connect("drag-leave", lambda *_: self._drop_hint(False))
        self.connect("key-press-event", self._on_key)

        self.show_all()
        self.update_info()       # hides Use image until a picture is chosen
        self.set_focus(None)     # else Browse files would open looking pressed
        self.tick_id = GLib.timeout_add(40, self._tick)
        self._show([self.places[0][1]] if self.places else [])
        try:
            self.run()
        finally:
            self.generation += 1
            GLib.source_remove(self.tick_id)
            self.destroy()

    # --- the grid -----------------------------------------------------------------

    def _show(self, folders: List[Path]):
        self.generation += 1
        self.selected = None
        for child in self.flow.get_children():
            child.destroy()
        generation = self.generation
        _PREVIEW_POOL.submit(lambda: GLib.idle_add(self._fill, generation, _images_in(folders)))
        self.update_info()

    def _fill(self, generation: int, paths: List[Path]) -> bool:
        if generation != self.generation:
            return False
        for path in paths:
            self.flow.add(_FileTile(self, path))
        self.flow.show_all()
        self.empty.set_visible(not paths)
        return False

    def _tick(self) -> bool:
        """Keeps the loading shimmer moving."""
        for child in self.flow.get_children():
            tile = child.get_child()
            if tile.loading:
                tile.area.queue_draw()
        return True

    def choose(self, tile: _FileTile):
        if self.selected is not None:
            self.selected.set_selected(False)
        self.selected = tile
        tile.set_selected(True)
        self.update_info()

    def update_info(self):
        tile = self.selected
        if tile is None:
            self.info.set_text(self._("up_hint").format(size=BEST_SIZE[self.kind]))
        elif tile.size:
            self.info.set_text(f"{tile.path.name} · {tile.size[0]} × {tile.size[1]}")
        else:
            self.info.set_text(tile.path.name)
        self.apply_btn.set_visible(tile is not None)
        self.apply_btn.set_sensitive(tile is not None and not tile.loading and not tile.failed)

    def apply(self):
        tile = self.selected
        if tile is not None and not tile.loading and not tile.failed:
            self.path = str(tile.path)
            self.response(Gtk.ResponseType.OK)

    # --- other ways in -------------------------------------------------------------

    def _browse(self):
        native = Gtk.FileChooserNative.new(self._("up_title_" + self.kind), self, Gtk.FileChooserAction.OPEN,
                                           None, None)
        images = Gtk.FileFilter()
        images.set_name(self._("image_filter"))
        images.add_mime_type("image/*")
        native.add_filter(images)
        if self.places:
            native.set_current_folder(str(self.places[0][1]))
        if native.run() == Gtk.ResponseType.ACCEPT and native.get_filename():
            self.path = native.get_filename()
            self.response(Gtk.ResponseType.OK)

    def _drop_hint(self, on: bool):
        ctx = self.root.get_style_context()
        (ctx.add_class if on else ctx.remove_class)("up-drop")

    def _on_drop(self, _w, _ctx, _x, _y, data, _info, _time):
        self._drop_hint(False)
        for uri in data.get_uris() or []:
            try:
                path, _host = GLib.filename_from_uri(uri)
            except GLib.Error:
                continue
            if path.lower().endswith(IMAGE_SUFFIXES) and os.path.isfile(path):
                tile = _FileTile(self, Path(path))
                self.flow.insert(tile, 0)
                self.flow.show_all()
                self.empty.hide()
                self.choose(tile)
                return

    def _on_key(self, _w, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self.response(Gtk.ResponseType.CANCEL)
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.selected is not None:
            self.apply()
            return True
        return False


def pick_image_file(app, kind: str, subtitle: str) -> Optional[str]:
    """kind: cover, hero or icon - the shape the previews take."""
    return ImageFilePicker(app, kind, subtitle, app._dialog_transient_parent()).path
