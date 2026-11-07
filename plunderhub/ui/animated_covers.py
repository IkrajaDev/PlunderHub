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
"""Playing animated covers (GIF / WebP / APNG) without slowing the app down.

Every animation plays at the pace its own file sets. Its frames are
decoded, sized and rounded on worker threads; the main thread only
picks, once per frame of the monitor, which ready frame each visible
cover shows - by elapsed time, so a busy moment skips a frame instead of
slowing the animation down. The frame is painted over the Gtk.Image's
still by a draw handler: handing the Image each frame (set_from_surface)
re-laid the whole window out, 30 times a second per cover.

Once an animation has gone round once, its whole loop stays in memory
and costs nothing more - kept in 16 bits a pixel, dithered (half the
memory, no visible banding), where cairo can dither. The loops share a
budget (a sixteenth of the RAM, 256 MB to 1 GB); when it's full the
loops of the covers out of sight longest go first, and an animation
whose loop still doesn't fit is streamed instead - a worker keeps a few
frames ahead of it. A loop out of sight for OFFSCREEN_KEEP_S is let go
anyway, and all of them once the animations have been paused for
PAUSED_KEEP_S (a game running, the window in the background): they're
decoded again when they show.

Covers out of sight don't play, and nothing plays while the decorative
animations are paused (window out of focus, a game running).
"""

import bisect
import math
import os
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cairo

from plunderhub.core.apng import open_animation
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib
from plunderhub.logging_setup import logger
from plunderhub.ui import memory

LOOKAHEAD = 4          # frames a streamed animation is kept ahead
FIRST_LOOP_BATCH = 8   # frames per job while a loop is still being built
STREAM_SLACK_MS = 200  # how far playback may run ahead of a stream before it waits
FIRST_LOOPS_AT_ONCE = 2  # loops being built at the same time (see _tick)
OFFSCREEN_KEEP_S = 20  # a loop out of sight this long is let go
PAUSED_KEEP_S = 60     # all loops are let go once paused this long
TRIM_EVERY_MS = 5000

# Frames in 16 bits a pixel, dithered from the decoded 32 - where cairo
# dithers (1.18+); older ones keep 32 bits, as banding would show.
_DITHER = (cairo.cairo_version() >= 11800 and hasattr(cairo, "Dither")
           and hasattr(cairo.SurfacePattern, "set_dither"))


def _memory_budget() -> int:
    try:
        total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError):
        total = 4 << 30
    return int(max(256 << 20, min(total / 16, 1 << 30)))


def _decode_at_size(path: Path, w: int, h: int):
    """The animation decoded straight at the size it's shown at (never
    larger than the file) - APNGs too, see core/apng.py."""
    return open_animation(path, w, h)


def _rounded(cr, w: int, h: int, r: float):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(w - r, r, r, -math.pi / 2, 0)
    cr.arc(w - r, h - r, r, 0, math.pi / 2)
    cr.arc(r, h - r, r, math.pi / 2, math.pi)
    cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class _Track:
    """One animated cover: its ready frames and where playback is."""

    def __init__(self, uid: str, path: Path, w: int, h: int, mtime: float):
        self.uid, self.path, self.w, self.h, self.mtime = uid, path, w, h, mtime
        self.widgets: List = []        # the Gtk.Images showing it, all at this size
        self.frames: List[Tuple[cairo.ImageSurface, int]] = []   # the loop, once complete
        self.starts: List[int] = []    # each frame's start within the loop, ms
        self.loop_ms = 0
        self.complete = False
        self.streaming = False         # the loop didn't fit: frames come through ring
        self.ring: deque = deque()     # (start ms, surface, delay) when streaming
        self.stream_ms = 0.0
        self.decoder = None            # [animation, iter, timeval, first frame] - worker side
        self.busy = False              # a worker job is out
        self.dead = False              # undecodable, or a still image
        self.clock_ms = 0.0
        self.current: Optional[cairo.ImageSurface] = None
        # the frame on show, made 32 bits again with its corners - once a
        # frame, not on each of the many repaints a lift or a scroll asks for
        self.display: Optional[cairo.ImageSurface] = None
        self.visible = False
        self.last_seen = 0.0
        self.bytes = 0
        self.generation = 0            # bumped when the track is reset, so a late job is dropped

    def drop_frames(self):
        self.frames, self.starts, self.loop_ms, self.bytes = [], [], 0, 0
        self.complete = False
        self.decoder = None
        self.ring.clear()
        self.clock_ms = self.stream_ms = 0.0
        self.generation += 1


class AnimatedCoverPlayer:
    def __init__(self, app, corner_radius: float):
        self.app = app
        self.radius = corner_radius
        self.budget = _memory_budget()
        self.used = 0
        # per cover AND size: the same game can show in the Recently
        # sailed row and the grid at once, at two sizes
        self.tracks: Dict[Tuple[str, int, int], _Track] = {}
        self.pool: Optional[ThreadPoolExecutor] = None
        self.tick_id: Optional[int] = None
        self.last_us: Optional[int] = None
        self.paused_since: Optional[float] = None
        GLib.timeout_add(TRIM_EVERY_MS, self._trim)

    # --- what plays ------------------------------------------------------------

    def play(self, uid: str, path: Path, w: int, h: int, widgets: List):
        """Plays uid's cover on widgets (its first frame is already on
        them). The same file at the same size keeps its frames - a
        re-rendered grid carries on where it was."""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return
        for key, other in list(self.tracks.items()):
            if other.uid == uid and (other.path, other.mtime) != (path, mtime):
                self._drop(key)        # a different cover now
        key = (uid, w, h)
        track = self.tracks.get(key)
        if track is None:
            track = self.tracks[key] = _Track(uid, path, w, h, mtime)
        for widget in widgets:
            if widget not in track.widgets:
                track.widgets.append(widget)
            if not getattr(widget, "_plunderhub_frame_hooked", False):
                widget._plunderhub_frame_hooked = True
                widget.connect("draw", self._draw_frame)
        track.current = None           # show the current frame on the new widgets too
        if self.tick_id is None:
            self.last_us = None
            self.tick_id = self.app.add_tick_callback(self._tick)

    @staticmethod
    def _draw_frame(widget, cr) -> bool:
        """The playing frame in place of the Image's still - centred, as
        the Image itself would draw it."""
        surface = getattr(widget, "_plunderhub_frame", None)
        if surface is None:
            return False
        x = (widget.get_allocated_width() - surface.get_width()) / 2
        y = (widget.get_allocated_height() - surface.get_height()) / 2
        cr.set_source_surface(surface, x, y)
        cr.paint()
        return True

    def _displayable(self, track: _Track, surface: cairo.ImageSurface) -> cairo.ImageSurface:
        """A 16-bit frame copied into the track's 32-bit one, corners cut -
        painting 16 bits on every repaint cost half again as much CPU."""
        if surface.get_format() == cairo.FORMAT_ARGB32:
            return surface
        sw, sh = surface.get_width(), surface.get_height()
        out = track.display
        if out is None or (out.get_width(), out.get_height()) != (sw, sh):
            out = track.display = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
        cr = cairo.Context(out)
        # a plain copy (pixman's fast path), then only the four corners cut
        # by a mask made once - masking the whole frame took three times as long
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_surface(surface, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_DEST_IN)
        cr.set_source_rgba(0, 0, 0, 1)
        mask = self._corner_mask(sw, sh)
        r = int(math.ceil(self.radius))
        for x, y in ((0, 0), (sw - r, 0), (0, sh - r), (sw - r, sh - r)):
            cr.save()
            cr.rectangle(x, y, r, r)
            cr.clip()
            cr.mask_surface(mask, 0, 0)
            cr.restore()
        out.flush()
        return out

    def _corner_mask(self, w: int, h: int) -> cairo.ImageSurface:
        key = (w, h, self.radius)
        cached = getattr(self, "_mask", None)
        if cached is None or cached[0] != key:
            mask = cairo.ImageSurface(cairo.FORMAT_A8, w, h)
            mcr = cairo.Context(mask)
            _rounded(mcr, w, h, self.radius)
            mcr.fill()
            cached = self._mask = (key, mask)
        return cached[1]

    @staticmethod
    def _show(widget, surface: Optional[cairo.ImageSurface]):
        widget._plunderhub_frame = surface
        widget.queue_draw()

    def _drop(self, key):
        track = self.tracks.pop(key, None)
        if track is not None:
            self.used -= track.bytes
            track.drop_frames()
            for widget in track.widgets:
                self._show(widget, None)      # back to the still
            memory.release_soon()
        if not self.tracks and self.tick_id is not None:
            self.app.remove_tick_callback(self.tick_id)
            self.tick_id = None

    def stop(self, uid: str):
        for key in [k for k in self.tracks if k[0] == uid]:
            self._drop(key)

    def stop_all(self):
        for key in list(self.tracks):
            self._drop(key)

    def playing(self) -> List[str]:
        return sorted({uid for uid, _w, _h in self.tracks})

    def busy(self) -> bool:
        """A visible cover is still building its first loop."""
        return any(t.visible and not (t.complete or t.streaming or t.dead) for t in self.tracks.values())

    # --- worker side -------------------------------------------------------------

    @staticmethod
    def _opaque(pb) -> bool:
        """No see-through pixel in the frame: laid on black and on white it
        comes out the same (compared in C, like the loop's frames)."""
        if not pb.get_has_alpha():
            return True
        w, h = pb.get_width(), pb.get_height()
        on_black = pb.composite_color_simple(w, h, GdkPixbuf.InterpType.NEAREST, 255, 1 << 16, 0, 0)
        on_white = pb.composite_color_simple(w, h, GdkPixbuf.InterpType.NEAREST, 255, 1 << 16,
                                             0xFFFFFF, 0xFFFFFF)
        return on_black.read_pixel_bytes().equal(on_white.read_pixel_bytes())

    @staticmethod
    def _cut(pb, w: int, h: int):
        """The frame's middle in the card's w:h shape (a view, no copy)."""
        pw, ph = pb.get_width(), pb.get_height()
        cw, ch = min(pw, round(ph * w / h)), min(ph, round(pw * h / w))
        if (cw, ch) == (pw, ph) or cw < 1 or ch < 1:
            return pb
        return pb.new_subpixbuf((pw - cw) // 2, (ph - ch) // 2, cw, ch)

    def _render(self, pb) -> cairo.ImageSurface:
        """A frame, rounded - or, with nothing see-through in it, in 16 bits
        a pixel, dithered, its corners cut when drawn (_draw_frame)."""
        small = _DITHER and self._opaque(pb)
        pw, ph = pb.get_width(), pb.get_height()
        surface = cairo.ImageSurface(cairo.FORMAT_RGB16_565 if small else cairo.FORMAT_ARGB32, pw, ph)
        cr = cairo.Context(surface)
        if small:
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.get_source().set_dither(cairo.Dither.GOOD)
        else:
            _rounded(cr, pw, ph, self.radius)
            cr.clip()
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()
        surface.flush()
        return surface

    def _decode(self, track: _Track, count: int, keep: bool):
        if track.decoder is None:
            # decoded big enough to fill the card, then cut to its shape
            # around the middle (like the stills, covers_mixin._load_cropped_cover)
            bw, bh = track.w, track.h
            info = GdkPixbuf.Pixbuf.get_file_info(str(track.path))
            if info and info[0] is not None and info[1] and info[2]:
                k = max(track.w / info[1], track.h / info[2])
                bw, bh = max(1, math.ceil(info[1] * k)), max(1, math.ceil(info[2] * k))
            animation = _decode_at_size(track.path, bw, bh)
            if animation is None or animation.is_static_image():
                return None, False
            tv = GLib.TimeVal()
            tv.tv_sec, tv.tv_usec = 0, 0
            track.decoder = [animation, animation.get_iter(tv), tv, None]
        _animation, it, tv, first = track.decoder
        out, looped = [], False
        for _ in range(count):
            pb = it.get_pixbuf()
            # Compared in C: a Python copy of every frame's pixels would
            # hold the interpreter lock - and the main thread with it.
            pixels = pb.read_pixel_bytes()
            if first is None:
                track.decoder[3] = first = pixels
            elif keep and pixels.get_size() == first.get_size() and pixels.equal(first):
                looped = True          # back at the first frame: the loop is complete
                break
            delay = it.get_delay_time()
            delay = delay if delay > 0 else 100
            out.append((self._render(self._cut(pb, track.w, track.h)), delay))
            # Stepping a clock of our own, not the real one, so frames come
            # out in order however long the work takes.
            tv.tv_usec += delay * 1000
            tv.tv_sec, tv.tv_usec = tv.tv_sec + tv.tv_usec // 1_000_000, tv.tv_usec % 1_000_000
            it.advance(tv)
        return out, looped

    def _job(self, track: _Track, keep: bool, generation: int):
        try:
            frames, looped = self._decode(track, FIRST_LOOP_BATCH if keep else LOOKAHEAD, keep)
        except Exception as e:
            logger.debug(f"Couldn't decode the animated cover {track.path}: {e}")
            frames, looped = None, False
        GLib.idle_add(self._take, track, frames, looped, keep, generation)

    # --- main thread ---------------------------------------------------------------

    def _take(self, track: _Track, frames, looped: bool, keep: bool, generation: int) -> bool:
        track.busy = False
        if generation != track.generation or track not in self.tracks.values():
            return False               # stopped or reset meanwhile
        if frames is None:
            track.dead = True          # a still, or undecodable: its first frame stays
            return False
        if keep and not track.streaming:
            for surface, delay in frames:
                track.starts.append(track.loop_ms)
                track.frames.append((surface, delay))
                track.loop_ms += delay
            nbytes = sum(s.get_stride() * s.get_height() for s, _d in frames)
            track.bytes += nbytes
            self.used += nbytes
            if looped:
                track.complete = True
                track.decoder = None   # nothing left to decode, ever
                memory.release_soon()  # the decoder's own copies of the frames
        else:
            for surface, delay in frames:
                track.ring.append((track.stream_ms, surface, delay))
                track.stream_ms += delay
        return False

    def _make_room(self, need: int, keep_track: _Track) -> bool:
        """Frees whole loops of the covers out of sight longest."""
        if self.used + need <= self.budget:
            return True
        candidates = sorted((t for t in self.tracks.values()
                             if t.frames and not t.visible and t is not keep_track and not t.busy),
                            key=lambda t: t.last_seen)
        for track in candidates:
            self.used -= track.bytes
            track.drop_frames()
            track.streaming = False
            if self.used + need <= self.budget:
                return True
        return False

    def _frame_now(self, track: _Track, dt: float) -> Optional[cairo.ImageSurface]:
        track.clock_ms += dt
        if not track.streaming and track.frames:
            if track.complete:
                pos = track.clock_ms % track.loop_ms
            elif track.clock_ms < track.loop_ms:
                pos = track.clock_ms
            else:
                # still building the first loop: wait at its edge
                track.clock_ms = pos = track.loop_ms - 1
            return track.frames[bisect.bisect_right(track.starts, pos) - 1][0]
        ring = track.ring
        while len(ring) > 1 and ring[1][0] <= track.clock_ms:
            ring.popleft()             # already past
        if ring:
            if ring[0][0] > track.clock_ms + STREAM_SLACK_MS:
                track.clock_ms = ring[0][0]
            return ring[0][1]
        if track.stream_ms and track.clock_ms > track.stream_ms + STREAM_SLACK_MS:
            track.clock_ms = track.stream_ms    # the worker fell behind: don't run ahead of it
        return None

    def _let_go(self, track: _Track):
        """Frees a loop; it's decoded again when it shows."""
        self.used -= track.bytes
        track.drop_frames()
        track.streaming = False
        track.current = track.display = None
        for widget in track.widgets:
            self._show(widget, None)
        memory.release_soon()

    def _trim(self) -> bool:
        """Every few seconds, frames running or not: lets go of the loops
        out of sight a while, and of all of them after a long pause."""
        mono = time.monotonic()
        paused = self.app._should_pause_decorative_animations() or not self.app.get_mapped()
        if not paused:
            self.paused_since = None
        elif self.paused_since is None:
            self.paused_since = mono
        long_paused = self.paused_since is not None and mono - self.paused_since >= PAUSED_KEEP_S
        for track in list(self.tracks.values()):
            if track.busy or not (track.frames or track.ring):
                continue
            if long_paused or (not track.visible and mono - track.last_seen >= OFFSCREEN_KEEP_S):
                self._let_go(track)
        if long_paused:
            # the heroes too - a long one's stream held some 35 MB
            from plunderhub.ui import animated_hero
            animated_hero.let_go_paused(self.app)
        return True

    def wake(self):
        """Back to playing - after a pause, or with an animated cover
        scrolled into view."""
        if self.tick_id is None and self.tracks:
            self.last_us = None
            self.tick_id = self.app.add_tick_callback(self._tick)

    def _sleep(self) -> bool:
        """Off the frame clock till wake(): nothing to show for now."""
        self.tick_id = None
        self.last_us = None
        return False

    def _tick(self, _widget, frame_clock) -> bool:
        app = self.app
        # On the beat the bubbles and the card lift step on (~60 fps): all
        # of them change in the same repaints - a cover on a frame of its
        # own made the window paint half again as often. The frame shown is
        # still the one due by the clock.
        if self.last_us is not None and frame_clock.get_frame_counter() % app._bubble_beat_frames(frame_clock):
            return True
        now = frame_clock.get_frame_time()
        if app._should_pause_decorative_animations() or not app.get_mapped():
            return self._sleep()
        dt = min(250.0, (now - self.last_us) / 1000) if self.last_us is not None else 0.0
        self.last_us = now
        mono = time.monotonic()
        jobs = []
        for key, track in list(self.tracks.items()):
            # Only the widgets still on show - a re-render replaces them.
            alive = app.cover_widgets.get(track.uid) or []
            track.widgets = [w for w in track.widgets if w in alive]
            widgets = track.widgets
            if not widgets:
                self._drop(key)
                continue
            track.visible = app._cover_is_on_screen(track.uid)
            if not track.visible or track.dead:
                continue
            track.last_seen = mono
            surface = self._frame_now(track, dt)
            if surface is not None and surface is not track.current:
                track.current = surface
                shown = self._displayable(track, surface)
                for widget in widgets:
                    self._show(widget, shown)
            if not track.busy and not track.complete:
                if not track.streaming or len(track.ring) < LOOKAHEAD:
                    jobs.append(track)
        if jobs:
            if self.pool is None:
                # The main thread gets the interpreter back sooner while
                # workers decode.
                sys.setswitchinterval(0.001)
                self.pool = ThreadPoolExecutor(max_workers=max(1, min(3, (os.cpu_count() or 4) // 4)),
                                               thread_name_prefix="plunderhub-anim")
            # from the top of the view down
            jobs.sort(key=lambda t: t.widgets[0].get_allocation().y)
            # A decoder holds every frame it has made till the loop is done -
            # fifteen covers starting at once went past 2.5 GB for a moment.
            # (a job out counts too: its decoder only shows once the file's read)
            building = sum(1 for t in self.tracks.values()
                           if (t.decoder is not None or t.busy) and not t.streaming and not t.complete)
            for track in jobs:
                if track.decoder is None and not track.streaming:
                    if building >= FIRST_LOOPS_AT_ONCE:
                        continue       # its turn comes when one of them is done
                    building += 1
                keep = not track.streaming
                if keep and not self._make_room(track.w * track.h * (2 if _DITHER else 4) * FIRST_LOOP_BATCH,
                                                track):
                    # The whole loop won't fit: stream it instead.
                    self.used -= track.bytes
                    position = track.loop_ms
                    track.drop_frames()
                    track.streaming = True
                    track.stream_ms = track.clock_ms = position
                    keep = False
                track.busy = True
                self.pool.submit(self._job, track, keep, track.generation)
        elif not any(t.visible and not t.dead for t in self.tracks.values()):
            # none of them on screen: nothing to step till a scroll
            return self._sleep()
        return True

    def shutdown(self):
        self.stop_all()
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
