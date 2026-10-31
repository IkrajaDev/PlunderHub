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
"""Animated art played on a widget that draws it itself: the library's
Continue playing card, Console Mode's poster, and the game page's hero
and cover. (The library's covers have their own player, animated_covers,
which feeds Gtk.Images.)

Every frame plays, at the pace the file sets, at the size it's drawn at.
A loop that fits its share of the budget is decoded once on a worker and
kept - starting to play after its first few frames, the rest following
while it plays - and shared, so the library drawn again, Console Mode or
the game page pick it up at once (the ones nothing shows go first when
the budget's full). One that doesn't - SteamGridDB's animated heroes run
to hundreds of 1920 px frames, half a minute long - is streamed: a
worker keeps a few frames ahead of it, round and round. Its frames are
counted from the file's chunks beforehand, so which one it is is known
up front. The widget's frame clock picks the frame due; nothing moves
while the decorative animations are paused or animated covers are
switched off."""

import bisect
import ctypes
import ctypes.util
import math
import os
import struct
import sys
import threading
import weakref
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import cairo

from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk
from plunderhub.logging_setup import logger
from plunderhub.ui import memory

GUESSED_FRAMES = 200     # when the file doesn't say how many it has
MAX_FRAMES = 600         # kept of one loop at most
MIN_SCALE = 0.75         # a kept loop is drawn from frames at least this big
FIRST_BATCH = 3          # frames before it starts to move
BATCH = 6
LOOKAHEAD = 6            # frames a stream is kept ahead
# a stream's loop kept as JPEGs after its first round... - 85: in colour
# (see _rgb_pixbuf) a full-screen Console Mode hero's round came to 68 MB at
# 90, past the limit (back to decoding the WebP each round); 40 MB at 85,
# no visible difference under the veil
JPEG_QUALITY = 85
JPEG_LIMIT = 64 << 20    # ...if they all fit in this
# quick first rounds (see _Stream) decoded at the same time: a list of rows
# over animated heroes started a dozen 46 MB files at once
_FIRST_ROUNDS = threading.BoundedSemaphore(2)


def _budget() -> int:
    """All the kept loops together: a sixteenth of the RAM, 128-512 MB."""
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        total = 4 << 30
    return int(max(128 << 20, min(total / 16, 512 << 20)))


BUDGET = _budget()
# one loop at a time - a hero's frames go through at full size
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plunderhub-hero")


def frame_count(path: Path) -> Optional[int]:
    """How many frames the file holds, read from its chunks - WebP's ANMF,
    APNG's acTL, GIF's image descriptors - without decoding any."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                count = 0
                while True:
                    chunk = f.read(8)
                    if len(chunk) < 8:
                        return count or None
                    fourcc, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
                    count += fourcc == b"ANMF"
                    f.seek(size + (size & 1), 1)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                f.seek(8)
                while True:
                    chunk = f.read(8)
                    if len(chunk) < 8:
                        return None
                    size, kind = struct.unpack(">I", chunk[:4])[0], chunk[4:]
                    if kind == b"acTL":
                        return struct.unpack(">I", f.read(4))[0]
                    if kind == b"IDAT":
                        return None    # acTL comes before the image data
                    f.seek(size + 4, 1)
            if head[:3] == b"GIF":
                return _gif_frames(f)
    except (OSError, struct.error, IndexError):     # a file cut short
        pass
    return None


def _gif_frames(f) -> Optional[int]:
    f.seek(10)
    flags = f.read(3)[0]
    if flags & 0x80:
        f.seek(3 * (2 << (flags & 7)), 1)       # the global colour table
    count = 0
    while True:
        kind = f.read(1)
        if not kind or kind == b"\x3b":
            return count or None
        if kind == b"\x21":
            f.read(1)                           # the extension's label
        elif kind == b"\x2c":
            count += 1
            desc = f.read(9)
            if desc[8] & 0x80:
                f.seek(3 * (2 << (desc[8] & 7)), 1)
            f.read(1)                           # LZW minimum code size
        else:
            return None
        while True:                             # its data sub-blocks
            size = f.read(1)
            if not size or size[0] == 0:
                break
            f.seek(size[0], 1)


class _WebPInfo(ctypes.Structure):
    _fields_ = [("canvas_width", ctypes.c_uint32), ("canvas_height", ctypes.c_uint32),
                ("loop_count", ctypes.c_uint32), ("bgcolor", ctypes.c_uint32),
                ("frame_count", ctypes.c_uint32), ("pad", ctypes.c_uint32 * 4)]


class _WebPOptions(ctypes.Structure):
    _fields_ = [("color_mode", ctypes.c_int), ("use_threads", ctypes.c_int), ("padding", ctypes.c_uint32 * 7)]


class _WebPData(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_void_p), ("size", ctypes.c_size_t)]


_WEBP_DEMUX_ABI = 0x0107        # libwebp 1.x
# premultiplied, in the byte order cairo's ARGB32 has here
_MODE_CAIRO = 8 if sys.byteorder == "little" else 9     # MODE_bgrA / MODE_Argb
_webp = None


def _webp_lib():
    """libwebpdemux, which decodes a WebP animation a frame at a time on
    one canvas - GdkPixbuf's loader keeps every frame it has decoded, and
    a long hero went past a gigabyte. None where it's missing."""
    global _webp
    if _webp is None:
        _webp = False
        try:
            lib = ctypes.CDLL(ctypes.util.find_library("webpdemux") or "libwebpdemux.so.2")
            lib.WebPAnimDecoderOptionsInitInternal.argtypes = [ctypes.POINTER(_WebPOptions), ctypes.c_int]
            lib.WebPAnimDecoderNewInternal.restype = ctypes.c_void_p
            lib.WebPAnimDecoderNewInternal.argtypes = [ctypes.POINTER(_WebPData), ctypes.POINTER(_WebPOptions),
                                                       ctypes.c_int]
            lib.WebPAnimDecoderGetInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WebPInfo)]
            lib.WebPAnimDecoderGetNext.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                                   ctypes.POINTER(ctypes.c_int)]
            lib.WebPAnimDecoderHasMoreFrames.argtypes = [ctypes.c_void_p]
            lib.WebPAnimDecoderReset.argtypes = [ctypes.c_void_p]
            lib.WebPAnimDecoderDelete.argtypes = [ctypes.c_void_p]
            _webp = lib
        except (OSError, AttributeError) as e:
            logger.debug(f"No libwebpdemux - long WebP animations are kept smaller instead: {e}")
    return _webp or None


def _fill(src: cairo.ImageSurface, w: int, h: int, crop: bool) -> cairo.ImageSurface:
    """src scaled to w x h - cut to fill (as the stills are) with crop."""
    sw, sh = src.get_width(), src.get_height()
    scale = max(w / sw, h / sh) if crop else min(w / sw, h / sh)
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(out)
    cr.translate((w - sw * scale) / 2, (h - sh * scale) * (0.3 if crop else 0.5))
    cr.scale(scale, scale)
    cr.set_source_surface(src, 0, 0)
    # GOOD only where it shows: down to half size bilinear looks the same
    # and takes a fifteenth of the time (6 ms -> 0.4 ms a 1920 px frame)
    cr.get_source().set_filter(cairo.FILTER_GOOD if scale < 0.5 else cairo.FILTER_BILINEAR)
    cr.paint()
    out.flush()
    return out


def _webp_frames(path: Path, w: int, h: int, crop: bool, stopped: Callable[[], bool], once: bool = False):
    """(surface, delay ms) for each frame, round and round until stopped -
    or just the once."""
    lib = _webp_lib()
    # read straight into the decoder's buffer - the file once in memory
    size = path.stat().st_size
    buf = ctypes.create_string_buffer(size)
    with open(path, "rb") as f:
        if f.readinto(buf) != size:
            raise ValueError("the file changed while it was read")
    # its address, not ctypes.cast(): cast ties the buffer into a reference
    # cycle, and the whole file stayed in memory till a full garbage collection
    data = _WebPData(ctypes.c_void_p(ctypes.addressof(buf)), size)
    opts = _WebPOptions()
    if not lib.WebPAnimDecoderOptionsInitInternal(ctypes.byref(opts), _WEBP_DEMUX_ABI):
        raise ValueError("libwebpdemux too old")
    opts.color_mode, opts.use_threads = _MODE_CAIRO, 0
    dec = lib.WebPAnimDecoderNewInternal(ctypes.byref(data), ctypes.byref(opts), _WEBP_DEMUX_ABI)
    if not dec:
        raise ValueError("not a WebP animation libwebp reads")
    try:
        info = _WebPInfo()
        lib.WebPAnimDecoderGetInfo(dec, ctypes.byref(info))
        cw, ch = info.canvas_width, info.canvas_height
        stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, cw)
        # Never made bigger than the file: drawn larger (a hero across a
        # 3440 px Console Mode) the frames are scaled up as they're drawn -
        # scaled up here, each was over twice the pixels to keep and pass on.
        grow = max(w / cw, h / ch) if crop else min(w / cw, h / ch)
        if grow > 1:
            w, h = max(1, round(w / grow)), max(1, round(h / grow))
        while not stopped():
            lib.WebPAnimDecoderReset(dec)
            last = 0
            while lib.WebPAnimDecoderHasMoreFrames(dec) and not stopped():
                pixels, stamp = ctypes.c_void_p(), ctypes.c_int()
                if not lib.WebPAnimDecoderGetNext(dec, ctypes.byref(pixels), ctypes.byref(stamp)):
                    raise ValueError("a broken WebP frame")
                if stride != cw * 4:
                    raise ValueError("an unexpected WebP stride")
                # the decoder's own canvas, not a copy: _fill is done
                # with it before the next frame overwrites it
                canvas = (ctypes.c_char * (cw * ch * 4)).from_address(pixels.value)
                src = cairo.ImageSurface.create_for_data(canvas, cairo.FORMAT_ARGB32, cw, ch, stride)
                yield _fill(src, w, h, crop), max(20, stamp.value - last)
                last = stamp.value
            if once:
                break
    finally:
        lib.WebPAnimDecoderDelete(dec)


def streamable(path: Path) -> bool:
    """Whether a long animation can be streamed without piling up its
    frames: WebP through libwebpdemux, APNG through apng's own reader."""
    from plunderhub.core.apng import is_apng_file
    return (path.suffix.lower() == ".webp" and _webp_lib() is not None) or is_apng_file(path)


def _opaque(surface: cairo.ImageSurface) -> Optional[GdkPixbuf.Pixbuf]:
    """The frame as a pixbuf if nothing in it is see-through (laid on black
    and on white it comes out the same, compared in C) - else None."""
    w, h = surface.get_width(), surface.get_height()
    pb = Gdk.pixbuf_get_from_surface(surface, 0, 0, w, h)
    if pb is None:
        return None
    if pb.get_has_alpha():
        on_black = pb.composite_color_simple(w, h, GdkPixbuf.InterpType.NEAREST, 255, 1 << 16, 0, 0)
        on_white = pb.composite_color_simple(w, h, GdkPixbuf.InterpType.NEAREST, 255, 1 << 16,
                                             0xFFFFFF, 0xFFFFFF)
        if not on_black.read_pixel_bytes().equal(on_white.read_pixel_bytes()):
            return None
    return pb


def _rgb_pixbuf(surface: cairo.ImageSurface) -> Optional[GdkPixbuf.Pixbuf]:
    """An opaque frame as a pixbuf without alpha, for JPEG: saved from one
    with alpha, gdk-pixbuf 2.44 (glycin) writes a grey JPEG - the hero
    turned black and white from its second round on."""
    w, h = surface.get_width(), surface.get_height()
    rgb = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
    cr = cairo.Context(rgb)
    cr.set_source_surface(surface, 0, 0)
    cr.paint()
    rgb.flush()
    return Gdk.pixbuf_get_from_surface(rgb, 0, 0, w, h)


def argb_surface(pb: GdkPixbuf.Pixbuf) -> cairo.ImageSurface:
    """pb as a 32-bit surface with alpha, whatever it has. One without
    alpha (a JPEG, an opaque PNG) comes out of Gdk.cairo_surface_create_
    from_pixbuf as RGB24, and pixman scales RGB24 the slow way - a 1920 px
    hero drawn at 3440 took 20 ms a frame instead of 2.5."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, pb.get_width(), pb.get_height())
    cr = cairo.Context(surface)
    cr.set_operator(cairo.OPERATOR_SOURCE)
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.paint()
    surface.flush()
    return surface


def _from_jpeg(data: bytes) -> cairo.ImageSurface:
    loader = GdkPixbuf.PixbufLoader.new_with_type("jpeg")
    loader.write(data)
    loader.close()
    return argb_surface(loader.get_pixbuf())


class _Stream:
    """A loop too big to keep: a worker decodes it round and round, a few
    frames ahead of what's shown, for one widget.

    A WebP's first round is also kept as JPEGs at the size it's shown at
    (some 20 MB for a half-minute hero): from the second round on, the
    worker unpacks those instead - a quarter of the CPU of decoding the
    WebP again, and the file's own 46 MB let go."""

    def __init__(self, path: Path, w: int, h: int, crop: bool, on_frame: Callable[[], None], quick: bool = False):
        """quick: the WebP's first round is turned into JPEGs as fast as it
        decodes, two streams at a time, before anything plays (the still
        shows meanwhile) - for a list's many rows. Else it plays as it's
        decoded."""
        self.path, self.w, self.h, self.crop, self.on_frame = path, w, h, crop, on_frame
        self.quick = quick
        self.queue: deque = deque()
        self.cond = threading.Condition()
        self.stopped = False
        threading.Thread(target=self._run, name="plunderhub-hero-stream", daemon=True).start()

    def _put(self, surface: cairo.ImageSurface, delay: int):
        with self.cond:
            while len(self.queue) >= LOOKAHEAD and not self.stopped:
                self.cond.wait(0.5)
            self.queue.append((surface, delay))
            first = len(self.queue) == 1
        if first:
            GLib.idle_add(self._first)

    def _run(self):
        from plunderhub.core.apng import open_animation
        from plunderhub.ui.game_page import _fitted, _surface
        try:
            if self.path.suffix.lower() == ".webp" and _webp_lib() is not None:
                if self.quick:
                    while not _FIRST_ROUNDS.acquire(timeout=0.5):
                        if self.stopped:
                            return
                    try:
                        kept = self._first_round(play=False)
                    finally:
                        _FIRST_ROUNDS.release()
                else:
                    kept = self._first_round(play=True)
                if self.stopped:
                    return
                if kept:
                    memory.release_soon()     # the WebP file and its decoder
                    while not self.stopped:
                        for data, delay in kept:
                            if self.stopped:
                                return
                            self._put(_from_jpeg(data), delay)
                    return
                for surface, delay in _webp_frames(self.path, self.w, self.h, self.crop, lambda: self.stopped):
                    self._put(surface, delay)
                return
            while not self.stopped:
                animation = open_animation(self.path)
                if animation.is_static_image():
                    return
                tv = GLib.TimeVal()
                tv.tv_sec, tv.tv_usec = 0, 0
                it = animation.get_iter(tv)
                while not self.stopped:
                    delay = it.get_delay_time()
                    self._put(_surface(_fitted(it.get_pixbuf(), self.w, self.h, self.crop), 0),
                              delay if delay > 0 else 100)
                    if delay < 0:
                        break                 # played its count out: from the top
                    tv.tv_usec += delay * 1000
                    tv.tv_sec, tv.tv_usec = tv.tv_sec + tv.tv_usec // 1_000_000, tv.tv_usec % 1_000_000
                    it.advance(tv)
        except Exception as e:
            logger.debug(f"Couldn't stream the animation {self.path}: {e}")

    def _first_round(self, play: bool) -> Optional[List[Tuple[bytes, int]]]:
        """Decodes the WebP through once - played as it goes with play -
        keeping its frames as JPEGs. None when they can't be kept
        (see-through, or past JPEG_LIMIT)."""
        kept: Optional[List[Tuple[bytes, int]]] = [] if self.crop else None
        size = 0
        for surface, delay in _webp_frames(self.path, self.w, self.h, self.crop, lambda: self.stopped, once=True):
            if play:
                self._put(surface, delay)
            elif kept is None:
                break                         # nothing to keep: streamed as usual instead
            if kept is not None:
                # the first frame says whether it's see-through (a JPEG can't be)
                pb = _rgb_pixbuf(surface) if kept or _opaque(surface) is not None else None
                ok, data = (pb.save_to_bufferv("jpeg", ["quality"], [str(JPEG_QUALITY)])
                            if pb is not None else (False, b""))
                size += len(data)
                if ok and size <= JPEG_LIMIT:
                    kept.append((data, delay))
                else:
                    kept = None
        return kept

    def _first(self) -> bool:
        if not self.stopped:
            self.on_frame()
        return False

    def take(self) -> Optional[Tuple[cairo.ImageSurface, int]]:
        with self.cond:
            item = self.queue.popleft() if self.queue else None
            self.cond.notify()
        return item

    def stop(self):
        with self.cond:
            self.stopped = True
            self.queue.clear()
            self.cond.notify()


class _Loop:
    """One file's frames at one size, filled in by the worker."""

    def __init__(self, key: Tuple, path: Path, w: int, h: int, crop: bool):
        self.key, self.path, self.w, self.h, self.crop = key, path, w, h, crop
        self.frames: List[Tuple[cairo.ImageSurface, int]] = []
        self.starts: List[int] = []
        self.loop_ms = 0           # of the frames there so far
        self.complete = False
        self.failed = False
        self.users: set = set()
        self.cancelled = False

    @property
    def bytes(self) -> int:
        return sum(s.get_stride() * s.get_height() for s, _d in self.frames)

    def add(self, frames: List[Tuple[cairo.ImageSurface, int]]):
        for surface, delay in frames:
            self.starts.append(self.loop_ms)
            self.loop_ms += delay
            self.frames.append((surface, delay))


_LOOPS: "OrderedDict[Tuple, _Loop]" = OrderedDict()


def _evict():
    """Down to the budget, dropping loops nothing shows, oldest first."""
    total = sum(loop.bytes for loop in _LOOPS.values())
    for key in list(_LOOPS):
        if total <= BUDGET:
            break
        loop = _LOOPS[key]
        if loop.users:
            continue
        loop.cancelled = True
        total -= loop.bytes
        del _LOOPS[key]
        memory.release_soon()


# a loop nothing shows any more is let go this long after (sooner, by the
# budget): a closed Set sail dialog kept its hero's frames - 220 MB - for as
# long as the app ran. Opened again meanwhile, it's still there.
UNUSED_LOOP_S = 20
_trim_id = 0


def _trim_soon():
    global _trim_id
    if _trim_id:
        GLib.source_remove(_trim_id)
    _trim_id = GLib.timeout_add_seconds(UNUSED_LOOP_S, _trim_unused)


def _trim_unused() -> bool:
    global _trim_id
    _trim_id = 0
    freed = False
    for key, loop in list(_LOOPS.items()):
        if not loop.users:
            loop.cancelled = True
            del _LOOPS[key]
            freed = True
    if freed:
        memory.release_soon()
    return False


def _loop_for(path: Path, w: int, h: int, crop: bool) -> Optional[_Loop]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = (str(path), mtime, w, h, crop)
    loop = _LOOPS.get(key)
    if loop is not None:
        _LOOPS.move_to_end(key)
        return loop
    loop = _LOOPS[key] = _Loop(key, path, w, h, crop)
    _POOL.submit(_decode, loop)
    return loop


def _decode(loop: _Loop):
    """The loop's frames, posted to the GTK thread a few at a time."""
    from plunderhub.core.apng import open_animation
    from plunderhub.ui.game_page import _fitted, _surface
    batch: List[Tuple[cairo.ImageSurface, int]] = []
    sent = 0
    if loop.path.suffix.lower() == ".webp" and _webp_lib() is not None:
        # one pass through libwebp: GdkPixbuf's loader would hold every
        # frame at full size on top of the ones kept
        try:
            for surface, delay in _webp_frames(loop.path, loop.w, loop.h, loop.crop,
                                               lambda: loop.cancelled or (not loop.users and sent > 0), once=True):
                batch.append((surface, delay))
                if len(batch) >= (FIRST_BATCH if not sent else BATCH) or len(batch) + sent >= MAX_FRAMES:
                    GLib.idle_add(_more, loop, batch)
                    sent += len(batch)
                    batch = []
                if sent >= MAX_FRAMES:
                    break
        except Exception as e:
            logger.debug(f"Couldn't decode the animation {loop.path}: {e}")
            GLib.idle_add(_finish, loop, batch, True)
            return
        if loop.cancelled or (not loop.users and sent > 0):
            GLib.idle_add(_abandon, loop)
        else:
            GLib.idle_add(_finish, loop, batch, False)
        return
    try:
        animation = open_animation(loop.path)
        if animation.is_static_image():
            GLib.idle_add(_finish, loop, [], True)
            return
        tv = GLib.TimeVal()
        tv.tv_sec, tv.tv_usec = 0, 0
        it = animation.get_iter(tv)
        first = None
        for _i in range(MAX_FRAMES):
            if loop.cancelled or (not loop.users and sent):
                # nothing shows it any more: dropped unfinished
                GLib.idle_add(_abandon, loop)
                return
            pb = it.get_pixbuf()
            # compared in C - a Python copy of every frame's pixels would
            # hold the interpreter lock, and the window with it
            pixels = pb.read_pixel_bytes()
            if first is None:
                first = pixels
            elif pixels.get_size() == first.get_size() and pixels.equal(first):
                break                  # round to the first frame: that's the loop
            delay = it.get_delay_time()
            delay = delay if delay > 0 else 100
            batch.append((_surface(_fitted(pb, loop.w, loop.h, loop.crop), 0), delay))
            if len(batch) >= (FIRST_BATCH if not sent else BATCH):
                GLib.idle_add(_more, loop, batch)
                sent += len(batch)
                batch = []
            tv.tv_usec += delay * 1000
            tv.tv_sec, tv.tv_usec = tv.tv_sec + tv.tv_usec // 1_000_000, tv.tv_usec % 1_000_000
            it.advance(tv)
    except Exception as e:
        logger.debug(f"Couldn't decode the animation {loop.path}: {e}")
        GLib.idle_add(_finish, loop, batch, True)
        return
    GLib.idle_add(_finish, loop, batch, False)


def _more(loop: _Loop, frames) -> bool:
    loop.add(frames)
    for anim in list(loop.users):
        anim.refresh()
    return False


def _finish(loop: _Loop, frames, failed: bool) -> bool:
    loop.add(frames)
    loop.complete = True
    loop.failed = failed or len(loop.frames) < 2
    _evict()
    for anim in list(loop.users):
        anim.refresh()
    return False


def _abandon(loop: _Loop) -> bool:
    if _LOOPS.get(loop.key) is loop:
        del _LOOPS[loop.key]
    return False


# Every live one, so a pause's end can start them up again (wake_all).
_LIVE: "weakref.WeakSet[HeroAnimation]" = weakref.WeakSet()


def wake_all():
    for anim in list(_LIVE):
        anim._wake()


def let_go_paused(app):
    """Frees the streams and loops of every hero that isn't playing now -
    out of sight, or paused with its window (a game running, the app in
    the background a while). One showing again starts over: the next draw
    sets it up (HeroAnimation.set)."""
    freed = False
    for anim in list(_LIVE):
        widget = anim.widget
        if not widget.get_mapped() or app._should_pause_decorative_animations_for(widget.get_toplevel()):
            freed |= anim.release()
    for key, loop in list(_LOOPS.items()):
        if not loop.users:
            loop.cancelled = True
            del _LOOPS[key]
            freed = True
    if freed:
        memory.release_soon()


class HeroAnimation:
    def __init__(self, app, widget: Gtk.Widget, region: Optional[Callable[[], Tuple[int, int, int, int]]] = None,
                 crop: bool = True, visible: Optional[Callable[[], bool]] = None, stream: bool = False):
        """region: the part of widget the art covers, when it's not all of
        it - only that is redrawn for a new frame. crop: cut to fill, as
        the stills are. visible: whether it's on screen now (it sleeps
        while it isn't - wake_all() after a scroll). stream: streamed even
        when its loop would fit the budget - for the many small ones, a
        list's rows, which together wouldn't."""
        self.app, self.widget, self.region, self.crop = app, widget, region, crop
        self.visible, self.prefer_stream = visible, stream
        self.key: Optional[Tuple] = None
        self.loop: Optional[_Loop] = None
        self.stream: Optional[_Stream] = None
        self.stream_frame: Optional[cairo.ImageSurface] = None
        self.stream_left_ms = 0.0
        self.clock_ms = 0.0
        self.current = 0
        self.last_us: Optional[int] = None
        # on the frame clock only while there's something moving to step
        # (_wake / _tick) - a still hero mustn't wake the app every frame
        self.tick_id = 0
        # a new size of the same art, waiting to settle (see set)
        self._resize_id = 0
        self._pending: Optional[Tuple[Path, int, int]] = None
        # asked for while paused: started when it plays again (see set)
        self._deferred: Optional[Tuple[Optional[Path], int, int]] = None
        _LIVE.add(self)
        self._watched_top = None
        self._watched_id = 0
        widget.connect("map", self._on_map)
        widget.connect("destroy", lambda *_: (self.stop(), self._unwatch_top()))

    def _on_map(self, widget):
        # its own window coming back to the front plays it again - the
        # game page is a window of its own, the main one's focus says
        # nothing about it
        top = widget.get_toplevel()
        if top is not self._watched_top and isinstance(top, Gtk.Window):
            self._unwatch_top()
            self._watched_top = top
            self._watched_id = top.connect("notify::is-active", lambda w, _p: w.is_active() and self._wake())
        self._wake()

    def _unwatch_top(self):
        # the window outlives the rows re-made at every render - its
        # handler would keep this (and the frames) alive with it
        if self._watched_id and self._watched_top is not None:
            self._watched_top.disconnect(self._watched_id)
        self._watched_top, self._watched_id = None, 0

    def _paused(self) -> bool:
        widget = self.widget
        return (not widget.get_mapped()
                or self.app._should_pause_decorative_animations_for(widget.get_toplevel()))

    def _wake(self):
        if self._deferred is not None and not self._paused():
            path, w, h = self._deferred
            self._deferred = None
            self.set(path, w, h)
        if not self.tick_id and (self.loop is not None or self.stream is not None) and self.widget.get_mapped():
            self.last_us = None
            self.tick_id = self.widget.add_tick_callback(self._tick)

    def _sleep(self) -> bool:
        self.tick_id = 0
        self.last_us = None
        return False

    def set(self, path: Optional[Path], w: int, h: int):
        """Plays path filling w x h - or nothing, for None or a still.

        The same art at a new size goes on playing, drawn to the new size,
        till the size has stayed put a moment: started again on every
        pixel of a window being resized, a long hero read its whole file
        and decoded from the top each time."""
        key = (str(path), w, h) if path is not None and w > 0 and h > 0 else None
        if key == self.key:
            self._cancel_resize()
            self._deferred = None
            return
        if key is not None and self.loop is None and self.stream is None and self._paused():
            # Nothing playing, and paused: drawn now and then in the
            # background, it started a stream - a whole file read, a round
            # decoded - for nobody. It starts when it plays again (_wake).
            self._deferred = (path, w, h)
            return
        self._deferred = None
        if (key is not None and self.key is not None and key[0] == self.key[0]
                and (self.loop is not None or self.stream is not None)):
            self._pending = (Path(path), w, h)
            if self._resize_id:
                GLib.source_remove(self._resize_id)
            self._resize_id = GLib.timeout_add(800, self._settle)
            return
        self._cancel_resize()
        self._start(path, w, h, key)

    def release(self):
        """Lets go of what it plays (see let_go_paused) - True if it had any."""
        if self.loop is None and self.stream is None:
            return False
        self._cancel_resize()
        self.key = None
        self._detach()
        return True

    def _cancel_resize(self):
        if self._resize_id:
            GLib.source_remove(self._resize_id)
            self._resize_id = 0
        self._pending = None

    def _settle(self) -> bool:
        self._resize_id = 0
        pending, self._pending = self._pending, None
        if pending is not None:
            path, w, h = pending
            self._start(path, w, h, (str(path), w, h))
        return False

    def _start(self, path: Optional[Path], w: int, h: int, key: Optional[Tuple]):
        self.key = key
        self._detach()
        if key is None or not self.app.animated_covers:
            return
        path = Path(path)
        frames = min(MAX_FRAMES, frame_count(path) or GUESSED_FRAMES)
        # kept if it fits its share of the budget at a size it still looks
        # sharp at (scaled back up when drawn), else streamed at full size
        scale = min(1.0, math.sqrt(BUDGET / 2 / (w * h * 4 * frames)))
        if (scale >= MIN_SCALE and not self.prefer_stream) or not streamable(path):
            # (one that can't be streamed is kept smaller rather)
            self.loop = _loop_for(path, max(1, int(w * scale)), max(1, int(h * scale)), self.crop)
            if self.loop is not None:
                self.loop.users.add(self)
        else:
            self.stream = _Stream(path, w, h, self.crop, self._redraw, quick=self.prefer_stream)
        self.clock_ms, self.current = 0.0, 0
        self._wake()

    def _detach(self):
        if self.loop is not None:
            self.loop.users.discard(self)
            self.loop = None
            _evict()
            _trim_soon()
        if self.stream is not None:
            self.stream.stop()
            self.stream, self.stream_frame, self.stream_left_ms = None, None, 0.0
            memory.release_soon(3000)     # once its worker has let go of the file

    def refresh(self):
        """New frames came in."""
        self._redraw()
        self._wake()

    def _redraw(self):
        if self.region is not None:
            self.widget.queue_draw_area(*self.region())
        else:
            self.widget.queue_draw()

    def frame(self) -> Optional[cairo.ImageSurface]:
        """The frame due now, or None while there's none (the caller
        draws its still)."""
        if not self.app.animated_covers:
            return None
        if self.stream is not None:
            return self.stream_frame
        loop = self.loop
        if loop is None or not loop.frames or loop.failed:
            return None
        return loop.frames[min(self.current, len(loop.frames) - 1)][0]

    def paint(self, cr, w: int, h: int) -> bool:
        """Draws the frame due, filling w x h - False with none."""
        surface = self.frame()
        if surface is None:
            return False
        cr.save()
        sw, sh = surface.get_width(), surface.get_height()
        if (sw, sh) != (w, h):
            cr.scale(w / sw, h / sh)
        cr.set_source_surface(surface, 0, 0)
        if (sw, sh) != (w, h):
            cr.get_source().set_filter(cairo.FILTER_BILINEAR)
        cr.paint()
        cr.restore()
        return True

    def _tick(self, widget, frame_clock) -> bool:
        app, loop = self.app, self.loop
        # on the common beat, as the covers (see AnimatedCoverPlayer._tick)
        if self.last_us is not None and frame_clock.get_frame_counter() % app._bubble_beat_frames(frame_clock):
            return True
        now = frame_clock.get_frame_time()
        # paused as the decorative animations are - its own window being
        # the one in front counts (the game page is a dialog of its own)
        if (not widget.get_mapped() or not app.animated_covers
                or app._should_pause_decorative_animations_for(widget.get_toplevel())
                or (self.visible is not None and not self.visible())):
            return self._sleep()
        dt = min(250.0, (now - self.last_us) / 1000) if self.last_us is not None else 0.0
        self.last_us = now
        if self.stream is not None:
            self._tick_stream(dt)
            return True
        if self.stream is None and (loop is None or loop.failed or (loop.complete and len(loop.frames) < 2)):
            return self._sleep()        # a still: nothing to step
        if loop is None or len(loop.frames) < 2:
            return True
        if loop.complete:
            self.clock_ms = (self.clock_ms + dt) % loop.loop_ms
        else:
            # still coming in: it waits on the last frame there is
            self.clock_ms = min(self.clock_ms + dt, loop.starts[-1])
        index = bisect.bisect_right(loop.starts, self.clock_ms) - 1
        if index != self.current:
            self.current = index
            self._redraw()
        return True

    def _tick_stream(self, dt: float):
        """The next frames off the stream as they fall due - waiting on the
        one shown when the worker's behind, rather than running ahead."""
        self.stream_left_ms -= dt
        changed = False
        while self.stream_left_ms <= 0:
            item = self.stream.take()
            if item is None:
                self.stream_left_ms = 0.0
                break
            self.stream_frame = item[0]
            self.stream_left_ms += item[1]
            changed = True
        if changed:
            self._redraw()

    def stop(self):
        self.key = None
        self._cancel_resize()
        self._detach()
        if self.tick_id:
            try:
                self.widget.remove_tick_callback(self.tick_id)
            except Exception:
                pass
            self.tick_id = 0
