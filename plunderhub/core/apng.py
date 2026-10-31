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
"""Animated PNGs, played without GdkPixbuf's help.

Many of SteamGridDB's animated covers are APNGs, and GdkPixbuf - glycin-
backed since 2.44 - refuses them outright. Each APNG frame is a little
PNG of its own, though: its chunks re-wrapped as one (a few ms for
GdkPixbuf to read), then laid over the frames before it with cairo, the
way the frame's dispose and blend settings say.

ApngAnimation offers the part of GdkPixbuf.PixbufAnimation the app uses
(is_static_image, get_static_image, get_iter -> get_pixbuf,
get_delay_time, advance), so the players take either."""

import struct
import zlib
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union

import cairo

from plunderhub.gi_setup import Gdk, GdkPixbuf

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# chunks every frame's PNG needs from the file's header
_SHARED = (b"PLTE", b"tRNS", b"gAMA", b"cHRM", b"sRGB", b"iCCP", b"sBIT")


def _chunks(data: bytes) -> Iterator[Tuple[bytes, bytes]]:
    pos = len(PNG_SIGNATURE)
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        yield kind, data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IEND":
            return


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


def is_apng(data: bytes) -> bool:
    """An animated PNG: a PNG with an acTL chunk before its image data."""
    if not data.startswith(PNG_SIGNATURE):
        return False
    for kind, _body in _chunks(data):
        if kind == b"acTL":
            return True
        if kind == b"IDAT":
            return False
    return False


def is_apng_file(path: Union[str, Path]) -> bool:
    """Walks the chunk headers up to the image data: acTL may come after
    a big ICC profile or text chunks, past any fixed number of bytes."""
    try:
        with open(path, "rb") as f:
            if f.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
                return False
            while True:
                head = f.read(8)
                if len(head) < 8:
                    return False
                kind = head[4:8]
                if kind == b"acTL":
                    return True
                if kind in (b"IDAT", b"IEND"):
                    return False
                f.seek(int.from_bytes(head[:4], "big") + 4, 1)     # the body and its CRC
    except OSError:
        return False


class _Frame:
    __slots__ = ("x", "y", "w", "h", "delay", "dispose", "blend", "data")

    def __init__(self, fctl: bytes):
        self.w, self.h, self.x, self.y, num, den, self.dispose, self.blend = struct.unpack(">IIIIHHBB", fctl[4:26])
        delay = num * 1000 // (den or 100)
        # as browsers do: a frame of 10 ms or less shows for 100
        self.delay = delay if delay > 10 else 100
        self.data: List[bytes] = []


class ApngAnimation:
    def __init__(self, source: Union[str, Path, bytes], max_w: int = 0, max_h: int = 0):
        """max_w / max_h: the frames come out fitted inside this size
        (never larger than the file); 0 keeps the file's own size."""
        data = source if isinstance(source, bytes) else Path(source).read_bytes()
        if not is_apng(data):
            raise ValueError("not an animated PNG")
        self.ihdr = b""
        self.shared: List[bytes] = []
        self.frames: List[_Frame] = []
        frame: Optional[_Frame] = None
        for kind, body in _chunks(data):
            if kind == b"IHDR":
                self.ihdr = body
            elif kind in _SHARED:
                self.shared.append(_chunk(kind, body))
            elif kind == b"fcTL":
                frame = _Frame(body)
                self.frames.append(frame)
            elif kind == b"IDAT" and frame is not None:
                # the default image is the first frame only when an fcTL
                # came before it - else it's left out of the animation
                frame.data.append(body)
            elif kind == b"fdAT" and frame is not None:
                frame.data.append(body[4:])      # its sequence number off
        self.frames = [f for f in self.frames if f.data and f.w and f.h]
        if not self.ihdr or not self.frames:
            raise ValueError("an animated PNG without frames")
        self.width, self.height = struct.unpack(">II", self.ihdr[:8])
        scale = 1.0
        if max_w and max_h:
            scale = min(max_w / self.width, max_h / self.height, 1.0)
        self.out_w, self.out_h = max(1, int(self.width * scale)), max(1, int(self.height * scale))

    # --- the PixbufAnimation part ---------------------------------------------------

    def is_static_image(self) -> bool:
        return len(self.frames) < 2

    def get_static_image(self) -> GdkPixbuf.Pixbuf:
        return next(self._render())[0]

    def get_width(self) -> int:
        return self.out_w

    def get_height(self) -> int:
        return self.out_h

    def get_iter(self, _start_time=None) -> "_Iter":
        return _Iter(self)

    # --- decoding ---------------------------------------------------------------------

    def _decode(self, frame: _Frame) -> Optional[GdkPixbuf.Pixbuf]:
        png = (PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">II", frame.w, frame.h) + self.ihdr[8:])
               + b"".join(self.shared) + b"".join(_chunk(b"IDAT", part) for part in frame.data)
               + _chunk(b"IEND", b""))
        loader = GdkPixbuf.PixbufLoader()
        try:
            loader.write(png)
        finally:
            loader.close()
        return loader.get_pixbuf()

    def _render(self) -> Iterator[Tuple[GdkPixbuf.Pixbuf, int]]:
        """Every frame once, composed, at the output size."""
        canvas = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.width, self.height)
        cr = cairo.Context(canvas)
        for index, frame in enumerate(self.frames):
            dispose = frame.dispose
            if index == 0 and dispose == 2:
                dispose = 1                      # nothing before it to go back to
            saved = None
            if dispose == 2:
                saved = cairo.ImageSurface(cairo.FORMAT_ARGB32, frame.w, frame.h)
                scr = cairo.Context(saved)
                scr.set_source_surface(canvas, -frame.x, -frame.y)
                scr.set_operator(cairo.OPERATOR_SOURCE)
                scr.paint()
            pb = self._decode(frame)
            cr.save()
            cr.rectangle(frame.x, frame.y, frame.w, frame.h)
            cr.clip()
            if frame.blend == 0 or index == 0:
                cr.set_operator(cairo.OPERATOR_SOURCE)
                cr.set_source_rgba(0, 0, 0, 0)
                cr.paint()
                cr.set_operator(cairo.OPERATOR_OVER)
            if pb is not None:
                Gdk.cairo_set_source_pixbuf(cr, pb, frame.x, frame.y)
                cr.paint()
            cr.restore()
            yield self._output(canvas), frame.delay
            # what's left for the next frame
            cr.save()
            cr.rectangle(frame.x, frame.y, frame.w, frame.h)
            cr.clip()
            cr.set_operator(cairo.OPERATOR_SOURCE)
            if dispose == 1:
                cr.set_source_rgba(0, 0, 0, 0)
                cr.paint()
            elif dispose == 2 and saved is not None:
                cr.set_source_surface(saved, frame.x, frame.y)
                cr.paint()
            cr.restore()

    def _output(self, canvas: cairo.ImageSurface) -> GdkPixbuf.Pixbuf:
        canvas.flush()
        if (self.out_w, self.out_h) == (self.width, self.height):
            return Gdk.pixbuf_get_from_surface(canvas, 0, 0, self.width, self.height)
        out = cairo.ImageSurface(cairo.FORMAT_ARGB32, self.out_w, self.out_h)
        cr = cairo.Context(out)
        cr.scale(self.out_w / self.width, self.out_h / self.height)
        cr.set_source_surface(canvas, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        out.flush()
        return Gdk.pixbuf_get_from_surface(out, 0, 0, self.out_w, self.out_h)


class _Iter:
    """Steps through the frames one at a time and starts over at the end -
    the time handed to advance() isn't needed: the callers step by each
    frame's own delay."""

    def __init__(self, animation: ApngAnimation):
        self.animation = animation
        self._frames = animation._render()
        self._current = next(self._frames)

    def get_pixbuf(self) -> GdkPixbuf.Pixbuf:
        return self._current[0]

    def get_delay_time(self) -> int:
        return self._current[1]

    def advance(self, _current_time=None) -> bool:
        try:
            self._current = next(self._frames)
        except StopIteration:
            self._frames = self.animation._render()
            self._current = next(self._frames)
        return True


def open_animation(path: Union[str, Path], max_w: int = 0, max_h: int = 0):
    """The file as an animation: an APNG always through ApngAnimation
    (GdkPixbuf either refuses one or shows only its first frame),
    anything else through GdkPixbuf - fitted inside max_w x max_h when
    given, never larger than the file."""
    if is_apng_file(path):
        return ApngAnimation(path, max_w, max_h)
    if not (max_w and max_h):
        return GdkPixbuf.PixbufAnimation.new_from_file(str(path))
    loader = GdkPixbuf.PixbufLoader()

    def fit(ldr, sw, sh):
        s = min(max_w / sw, max_h / sh, 1.0)
        ldr.set_size(max(1, int(sw * s)), max(1, int(sh * s)))
    loader.connect("size-prepared", fit)
    try:
        loader.write(Path(path).read_bytes())
    finally:
        loader.close()
    return loader.get_animation()
