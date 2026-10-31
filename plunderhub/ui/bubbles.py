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
"""The bubbles rising behind the library.

Three layers - far, mid and near - rise at their own pace and sway; the
near ones are glassy, the far ones a soft glow. Scrolling moves the
"camera": each layer follows the scroll by its own parallax, stretches
with the speed and settles as the water slows it down. A bubble the
pointer touches gets knocked aside, and some of them pop before reaching
the top.

Each bubble is painted once into a small sprite per theme, and the
window only redraws the spots where bubbles were and are now
(redraw_areas). A calm bubble steps on a steady beat of about 60 fps; one
that's pushed, or still moving from a push or a scroll, steps on every
frame of the monitor, so a knock looks as smooth as the pointer that gave
it. Motion is measured in 60 fps frames of elapsed time, so it moves at
the same pace at any refresh rate.
"""

import math
import random
from typing import Dict, List, Optional, Tuple

import cairo

Rect = Tuple[int, int, int, int]
RGB = Tuple[float, float, float]

# far / mid / near: size, how many, how fast they rise, how much they
# follow the scroll, whether they're just a glow
LAYERS = (
    dict(name="far", rmin=3.0, rmax=7.0, count=40, rise=0.25, parallax=0.12, soft=True),
    dict(name="mid", rmin=9.0, rmax=17.0, count=22, rise=0.45, parallax=0.30, soft=False),
    dict(name="near", rmin=20.0, rmax=36.0, count=9, rise=0.70, parallax=0.55, soft=False),
)
POP_SECONDS = 0.45


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(x + (y - x) * t for x, y in zip(a, b))


class BubbleField:
    def __init__(self):
        self.rng = random.Random()
        self.layers = [dict(spec=spec, vel=0.0, bubbles=[self._spawn(spec, anywhere=True)
                                                         for _ in range(spec["count"])])
                       for spec in LAYERS]
        self.pops: List[Dict] = []
        self.sprites: Dict[Tuple[str, float], cairo.ImageSurface] = {}
        self.colours = self._colours((0x22 / 255, 0xd3 / 255, 0xac / 255), (1, 1, 1), True)
        self.mouse: Optional[Tuple[float, float]] = None
        self._last_mouse: Optional[Tuple[float, float]] = None
        # where the pointer was a step ago: a quick flick jumps tens of
        # pixels between steps, right over a small bubble
        self._sweep_from: Optional[Tuple[float, float]] = None
        # something moves faster than the calm rise: the pointer, a pushed
        # bubble, a scroll - the field then steps on every frame
        self.lively = True
        self._drawn_pops: List[Rect] = []

    def _spawn(self, spec: Dict, anywhere: bool = False) -> Dict:
        rng = self.rng
        r = rng.uniform(spec["rmin"], spec["rmax"])
        return dict(x=rng.random(), y=rng.random() if anywhere else 1.0 + rng.random() * 0.15,
                    r=r, speed=rng.uniform(0.75, 1.3), sway=rng.uniform(6, 18) * (r / 20 + 0.4),
                    freq=rng.uniform(0.25, 0.6), phase=rng.uniform(0, 6.3),
                    ox=0.0, oy=0.0, vx=0.0, vy=0.0, hit=-9.0, touching=False,
                    pop_at=rng.uniform(0.04, 0.45))

    # --- colours ---------------------------------------------------------------

    @staticmethod
    def _colours(accent: RGB, fg: RGB, dark: bool) -> Dict:
        # Dark themes: a pale glass, faintly tinted by the accent. Light
        # ones: the accent itself - white bubbles vanish on a white page.
        if dark:
            return dict(body=_mix((1, 1, 1), accent, 0.35), rim=_mix((1, 1, 1), accent, 0.2), k=1.0)
        tint = _mix(accent, fg, 0.25)
        return dict(body=tint, rim=tint, k=1.15)

    def set_theme(self, accent: RGB, fg: RGB, dark: bool):
        colours = self._colours(accent, fg, dark)
        if colours != self.colours:
            self.colours = colours
            self.sprites.clear()

    def _sprite(self, layer: str, r: float) -> cairo.ImageSurface:
        key = (layer, round(r * 2) / 2)
        sprite = self.sprites.get(key)
        if sprite is not None:
            return sprite
        c = self.colours
        soft = next(s["soft"] for s in LAYERS if s["name"] == layer)
        size = int(math.ceil(r * 2 + 6))
        sprite = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(sprite)
        cx = cy = size / 2
        a = c["k"]
        if soft:
            # far away: just a glow, no edge
            g = cairo.RadialGradient(cx, cy, 0, cx, cy, r + 2)
            g.add_color_stop_rgba(0, *c["body"], 0.45 * a)
            g.add_color_stop_rgba(1, *c["body"], 0)
            cr.set_source(g)
            cr.arc(cx, cy, r + 2, 0, 2 * math.pi)
            cr.fill()
        else:
            # glass: clear in the middle, denser towards the rim
            g = cairo.RadialGradient(cx - r * 0.25, cy - r * 0.3, r * 0.1, cx, cy, r)
            g.add_color_stop_rgba(0, *c["body"], 0.05 * a)
            g.add_color_stop_rgba(0.7, *c["body"], 0.14 * a)
            g.add_color_stop_rgba(1, *c["body"], 0.40 * a)
            cr.set_source(g)
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(*c["rim"], 0.65 * a)
            cr.set_line_width(1.4)
            cr.arc(cx, cy, r - 0.6, 0, 2 * math.pi)
            cr.stroke()
            # a crescent of light on the upper left, a faint one lower right
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_source_rgba(1, 1, 1, 0.85 * a)
            cr.set_line_width(max(1.2, r * 0.12))
            cr.arc(cx, cy, r * 0.68, math.radians(195), math.radians(250))
            cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.35 * a)
            cr.set_line_width(max(1.0, r * 0.07))
            cr.arc(cx, cy, r * 0.72, math.radians(20), math.radians(55))
            cr.stroke()
        self.sprites[key] = sprite
        return sprite

    # --- motion ----------------------------------------------------------------

    def on_scroll(self, delta: float):
        """The view scrolled by delta pixels: the bubbles live in the water
        behind the cards, nearer layers move more."""
        for layer in self.layers:
            layer["vel"] -= delta * layer["spec"]["parallax"] * 0.22

    def tick(self, frames: float, t: float, w: int, h: int):
        """Advances by `frames` 60 fps frames; t is the time in seconds."""
        if w <= 0 or h <= 0:
            return
        if self.mouse is not None and self._last_mouse is not None:
            mvx = (self.mouse[0] - self._last_mouse[0]) / max(frames, 0.5)
            mvy = (self.mouse[1] - self._last_mouse[1]) / max(frames, 0.5)
        else:
            mvx = mvy = 0.0
        self._sweep_from = self._last_mouse
        self._last_mouse = self.mouse
        drag = 0.87 ** frames
        for layer in self.layers:
            spec = layer["spec"]
            layer["vel"] *= 0.86 ** frames      # the water slows the scroll's push down again
            shift = layer["vel"] * frames
            solid = spec["name"] != "far"
            for b in layer["bubbles"]:
                b["y"] += (shift - spec["rise"] * b["speed"] * frames) / h
                if solid:
                    self._touch(b, t, w, h, mvx, mvy, frames)
                b["ox"] += b["vx"] * frames
                b["oy"] += b["vy"] * frames
                b["vx"] *= drag
                b["vy"] *= drag
                if solid and b["y"] + b["oy"] / h < b["pop_at"] and layer["vel"] > -2:
                    x, y = self._position(b, t, w, h)
                    self.pops.append(dict(x=x, y=y, r=b["r"], t0=t))
                    b.update(self._spawn(spec))
                elif b["y"] + b["oy"] / h < -0.08:
                    b.update(self._spawn(spec))
                elif b["y"] + b["oy"] / h > 1.12:      # pushed below the bottom by a fast scroll up
                    b.update(self._spawn(spec))
                    b["y"] = -0.05
        self.pops = [p for p in self.pops if t - p["t0"] < POP_SECONDS]

    def _touch(self, b: Dict, t: float, w: int, h: int, mvx: float, mvy: float, frames: float):
        """Touched by the pointer, a bubble gets one knock as the pointer
        arrives - away from it, harder the faster it came - and slides
        out from under it while it stays. It stays where it was pushed."""
        if self.mouse is None:
            b["touching"] = False
            return
        x, y = self._position(b, t, w, h)
        # the nearest point of the pointer's path since the last step
        px, py = self.mouse
        if self._sweep_from is not None:
            sx, sy = self._sweep_from
            ex, ey = px - sx, py - sy
            length2 = ex * ex + ey * ey
            if length2 > 0:
                k = max(0.0, min(1.0, ((x - sx) * ex + (y - sy) * ey) / length2))
                px, py = sx + ex * k, sy + ey * k
        dx, dy = x - px, y - py
        d = math.hypot(dx, dy) or 0.001
        if d >= b["r"] + 3:
            b["touching"] = False
            return
        nx, ny = dx / d, dy / d
        if not b["touching"]:
            along = max(0.0, mvx * nx + mvy * ny)
            kick = min(9.0, 1.9 + along * 0.45)
            b["vx"] += nx * kick
            b["vy"] += ny * kick
            b["hit"] = t
        overlap = b["r"] + 3 - d
        share = 1 - 0.8 ** frames       # a fifth of it per 60 fps frame, whatever the step
        b["ox"] += nx * overlap * share
        b["oy"] += ny * overlap * share
        b["touching"] = True

    @staticmethod
    def _position(b: Dict, t: float, w: int, h: int) -> Tuple[float, float]:
        return (b["x"] * w + b["ox"] + math.sin(t * b["freq"] + b["phase"]) * b["sway"],
                b["y"] * h + b["oy"])

    # --- drawing ---------------------------------------------------------------

    def pointer_moved(self, mouse: Optional[Tuple[float, float]]) -> bool:
        """Whether the pointer is somewhere else than at the last step."""
        last = self._last_mouse
        return mouse is not None and (last is None or abs(mouse[0] - last[0]) + abs(mouse[1] - last[1]) >= 1)

    @staticmethod
    def _look(layer: Dict, b: Dict, t: float, w: int, h: int) -> Tuple[float, float, float, float]:
        """Where a bubble is painted and how it's squashed: x, y, sx, sy."""
        x, y = BubbleField._position(b, t, w, h)
        # a fast scroll stretches the bubbles along it, like motion blur
        stretch = min(0.6, abs(layer["vel"]) / 26)
        wobble = 1 + 0.04 * math.sin(t * 2.1 + b["phase"])
        since = t - b["hit"]
        if since < 0.5:      # a touch makes it jiggle for a moment
            wobble += 0.12 * math.exp(-since * 7) * math.sin(since * 40)
        return (x, y, wobble * (1 - stretch * 0.35), (1 + stretch) / wobble)

    @staticmethod
    def _rect(b: Dict, look: Tuple[float, float, float, float]) -> Rect:
        x, y, sx, sy = look
        rx, ry = (b["r"] + 3) * max(sx, 1) + 3, (b["r"] + 3) * max(sy, 1) + 3
        return (int(x - rx), int(y - ry), int(2 * rx) + 2, int(2 * ry) + 2)

    def _pop_rects(self) -> List[Rect]:
        rects = []
        for p in self.pops:
            reach = p["r"] * 2.7 + 6
            rects.append((int(p["x"] - reach), int(p["y"] - reach), int(2 * reach) + 2, int(2 * reach) + 2))
        return rects

    def redraw_areas(self, t: float, w: int, h: int, beat: bool, full: bool = False) -> List[Rect]:
        """After a step: the spots to repaint. Calm bubbles move on only on
        the beat, the lively ones on every step; each is painted where it
        was last moved to (draw). Sets `lively` for the next frame."""
        pops = self._pop_rects()
        areas = self._drawn_pops + pops
        self._drawn_pops = pops
        lively = self.mouse is not None and self.mouse != self._sweep_from
        for layer in self.layers:
            scrolling = abs(layer["vel"]) > 0.3
            for b in layer["bubbles"]:
                active = (scrolling or b["touching"] or abs(b["vx"]) + abs(b["vy"]) > 0.25
                          or t - b["hit"] < 0.5)
                lively |= active
                if not (beat or active or full):
                    continue
                look = self._look(layer, b, t, w, h)
                old = b.get("drawn")
                if old is not None:
                    areas.append(self._rect(b, old))
                areas.append(self._rect(b, look))
                b["drawn"] = look
        self.lively = lively
        return areas

    def draw(self, cr, t: float, w: int, h: int):
        x1, y1, x2, y2 = cr.clip_extents()
        for layer in self.layers:
            name = layer["spec"]["name"]
            for b in layer["bubbles"]:
                look = b.get("drawn") or self._look(layer, b, t, w, h)
                bx, by, bw, bh = self._rect(b, look)
                if bx > x2 or by > y2 or bx + bw < x1 or by + bh < y1:
                    continue
                sprite = self._sprite(name, b["r"])
                s = sprite.get_width()
                cr.save()
                cr.translate(look[0], look[1])
                cr.scale(look[2], look[3])
                cr.set_source_surface(sprite, -s / 2, -s / 2)
                cr.paint()
                cr.restore()
        c = self.colours
        for p in self.pops:
            k = (t - p["t0"]) / POP_SECONDS
            cr.set_source_rgba(*c["rim"], (1 - k) * 0.5 * c["k"])
            cr.new_path()
            cr.set_line_width(1.4 * (1 - k) + 0.3)
            cr.arc(p["x"], p["y"], p["r"] * (1 + k * 0.9), 0, 2 * math.pi)
            cr.stroke()
            for i in range(5):        # a few droplets flying off
                ang = i * 1.2566 + p["r"]
                d = p["r"] * (1 + k * 1.6)
                cr.new_path()
                cr.arc(p["x"] + math.cos(ang) * d, p["y"] + math.sin(ang) * d, 1.4 * (1 - k), 0, 2 * math.pi)
                cr.fill()
