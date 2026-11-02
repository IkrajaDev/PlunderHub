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


import json
import os
import re
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import time
import urllib.parse
import colorsys
import math
import zlib

# pycairo - used only for _round_pixbuf_corners's per-pixel corner mask,
# which GdkPixbuf has no built-in way to do. Guarded: every packaging
# path now declares this as a dependency (see install.sh/PKGBUILD), but
# an existing install that hasn't picked up the update yet shouldn't
# hard-crash the whole app over rounded corners specifically.
try:
    import cairo
except ImportError:
    cairo = None
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from plunderhub.core import apng
from plunderhub.core.models import GameItem
from plunderhub.core.persistence import save_cover_map, save_game_icon_map, save_hero_map, save_settings
from plunderhub.core.utils import HEADERS, http_get_json
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango, hidpi_pixbuf, pixbuf_scale, raw_set_source_pixbuf, ui_scale
import gi
gi.require_version("PangoCairo", "1.0")
from gi.repository import PangoCairo  # noqa: E402
from plunderhub.icons import get_icon_pixbuf
from plunderhub.logging_setup import logger


_BULK_DOWNLOAD_WORKERS = 5

_SAFE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}")


def _safe_image_ext(ext: Optional[str], default: str = ".png") -> str:
    """A cache file extension that can only ever be a plain ".png"-style
    suffix. The extension ends up in a file path, and some of them come
    from outside data - a URL, or a cover_map entry restored from a
    backup file - where something like "/../../.bashrc" would otherwise
    write outside the cache folder."""
    if isinstance(ext, str) and _SAFE_EXT_RE.fullmatch(ext):
        return ext
    return default


def _ext_from_url(url: str, default: str = ".png") -> str:
    return _safe_image_ext(os.path.splitext(urllib.parse.urlparse(url).path)[1], default)


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_APNG_CHUNKS = (b"acTL", b"fcTL", b"fdAT")


def _apng_still_frame(data: bytes) -> Optional[bytes]:
    """The APNG's default image as a plain, static PNG (the animation
    chunks dropped), or None if `data` isn't an animated PNG at all.
    Every APNG carries a regular IDAT image that non-APNG decoders show,
    so what's left is always a complete, valid PNG."""
    if not data.startswith(_PNG_SIGNATURE):
        return None
    out = [_PNG_SIGNATURE]
    pos = len(_PNG_SIGNATURE)
    animated = False
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        end = pos + 12 + length
        if end > len(data):
            return None
        chunk_type = data[pos + 4:pos + 8]
        if chunk_type in _APNG_CHUNKS:
            animated = True
        else:
            out.append(data[pos:end])
        pos = end
        if chunk_type == b"IEND":
            break
    return b"".join(out) if animated else None


def _pixbuf_can_decode(data: bytes) -> bool:
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(data)
        loader.close()
        return loader.get_animation() is not None
    except Exception:
        try:
            loader.close()
        except Exception:
            pass
        return False


def _make_decodable(data: bytes) -> bytes:
    """Some SteamGridDB "png" grids are really animated PNGs (often tens
    of MB, hundreds of frames) that the system's GdkPixbuf - glycin-
    backed since 2.44 - refuses outright ("Unrecognized image file
    format"), which left both the picker tile ("Failed to load") and the
    game card blank. Those get flattened to their first frame; anything
    that already decodes, animated or not, is kept as-is."""
    still = _apng_still_frame(data)
    if still is None or _pixbuf_can_decode(data):
        return data
    logger.info("Animated PNG can't be decoded on this system - using its first frame.")
    return still


def animation_sidecar(path: Path) -> Path:
    """Where a cover flattened by _make_decodable keeps its animation:
    the original APNG, for core/apng.py to play."""
    return path.parent / ".animated" / f"{path.stem}.apng"


def _keep_animation(path: Path, original: bytes) -> None:
    """Saves the APNG next to its flattened still - written after it, so
    a still replaced later is newer and the sidecar counts as stale (see
    CoversMixin._art_animation)."""
    sidecar = animation_sidecar(path)
    try:
        sidecar.parent.mkdir(exist_ok=True)
        _write_atomically(sidecar, lambda tmp: tmp.write_bytes(original))
    except OSError as e:
        logger.debug(f"Couldn't keep the animation of {path.name}: {e}")


def _flatten_undecodable_apng(path: Path, keep_animation: bool = False) -> bool:
    """_make_decodable() for art already on disk (cached before this
    fix existed, or a picker preview copied in as it was); True if the
    file was rewritten."""
    try:
        data = path.read_bytes()
        fixed = _make_decodable(data)
        if fixed is data:
            return False
        _write_atomically(path, lambda tmp: tmp.write_bytes(fixed))
        if keep_animation:
            _keep_animation(path, data)
        return True
    except Exception as e:
        logger.debug(f"Couldn't flatten {path}: {e}")
        return False


def _write_atomically(target: Path, write) -> None:
    """Runs write(tmp_path), then moves the finished file into place, so
    nothing - the UI loading the image, or a failed copy/download - ever
    sees a half-written file or loses the previous one."""
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part", dir=str(target.parent))
    os.close(fd)
    try:
        write(Path(tmp_name))
        os.replace(tmp_name, str(target))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _copy_art_file(src: str, target: Path) -> Optional[Exception]:
    """Copies a user-picked image into the cache; returns the error (and
    leaves the current art untouched) if it couldn't be copied - or
    if no image loader here reads it (a .heic / .jxl without its loader):
    the working art would make way for a placeholder."""
    # WebP (libwebp) and PNG / APNG (core/apng.py) are read by the app itself
    if not src.lower().endswith((".webp", ".png", ".apng")):
        try:
            readable = GdkPixbuf.Pixbuf.get_file_info(src)[0] is not None
        except Exception:
            readable = False
        if not readable:
            logger.warning(f"Couldn't use {src} as artwork: no image loader reads it")
            return ValueError("no image loader reads this file")
    try:
        _write_atomically(target, lambda tmp: shutil.copyfile(src, str(tmp)))
        return None
    except Exception as e:
        logger.warning(f"Couldn't use {src} as artwork: {e}")
        return e


_art_index_lock = threading.Lock()
_art_index: Dict[str, Tuple[int, float, Dict[str, List[Path]]]] = {}


def art_files_for(directory: Path, uid: str) -> List[Path]:
    """This game's files in an art folder, matched by name (a uid holding
    * ? [ ] or a dot is no pattern that reaches other games' files), from
    a listing kept while the folder is unchanged: one walk per change,
    not one per card."""
    try:
        mtime = directory.stat().st_mtime_ns
    except OSError:
        return []
    key = str(directory)
    with _art_index_lock:
        hit = _art_index.get(key)
        # a listing made in the same timestamp tick as a change may miss it
        if hit is None or hit[0] != mtime or hit[1] - mtime / 1e9 < 0.05:
            stems: Dict[str, List[Path]] = {}
            built = time.time()
            try:
                for f in sorted(directory.iterdir()):
                    if f.is_file():
                        stems.setdefault(f.stem, []).append(f)
            except OSError:
                return []
            hit = (mtime, built, stems)
            _art_index[key] = hit
        return list(hit[2].get(uid, []))


def _steam_asset_path(full) -> Optional[str]:
    """The file of a Steam store asset from SteamGridDB's platform data
    ({"image2x": {"english": "<hash>/library_600x900_2x.jpg"}, ...}):
    the sharpest, in English if there's one."""
    if isinstance(full, str):
        try:
            full = json.loads(full)
        except ValueError:
            return None
    if not isinstance(full, dict):
        return None
    for size in ("image2x", "image"):
        langs = full.get(size)
        if isinstance(langs, dict) and langs:
            path = langs.get("english") or next(iter(langs.values()))
            if isinstance(path, str) and re.fullmatch(r"[\w./-]+", path) and ".." not in path:
                return path
    return None


def _remove_other_art(directory: Path, uid: str, keep: Path) -> None:
    """Deletes this game's cached art in every OTHER format (a .png
    replaced by a .gif, say) - only ever after the new file is in place.
    Matched by name, not glob(), so a uid holding * ? [ ] can't reach
    other games' files."""
    for f in directory.iterdir():
        if f.is_file() and f.stem == uid and f.name != keep.name:
            f.unlink(missing_ok=True)



def _art_is_opaque(path: Optional[Path]) -> bool:
    """No see-through pixels: a JPEG, a PNG without alpha - or no file
    (the drawn stand-in). Anything else might let what's behind show."""
    if path is None:
        return True
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return True
    if suffix != ".png":
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(26)
    except OSError:
        return False
    # IHDR's colour type: 0 grey, 2 RGB - no alpha (3, palette, may have tRNS)
    return len(head) == 26 and head[1:4] == b"PNG" and head[25] in (0, 2)

class CoversMixin:

    def _build_cover_hint_bar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.get_style_context().add_class("cover-hint-bar")

        self.cover_hint_info_icon = self._icon_image_at_size("image-x-generic-symbolic", 18)
        pb = get_icon_pixbuf("image-x-generic-symbolic", 18, self._home_accent_hex())
        if pb is not None:
            self.cover_hint_info_icon.set_from_pixbuf(pb)
        bar.pack_start(self.cover_hint_info_icon, False, False, 6)

        self.cover_hint_label = Gtk.Label(label=self._("cover_hint_text"))
        self.cover_hint_label.set_xalign(0)
        self.cover_hint_label.set_line_wrap(True)
        self.cover_hint_label.set_hexpand(True)
        bar.pack_start(self.cover_hint_label, True, True, 0)

        self.cover_hint_open_btn = Gtk.Button(label=self._("cover_hint_btn"))
        self._mark_suggested_action(self.cover_hint_open_btn)
        self.cover_hint_open_btn.set_valign(Gtk.Align.CENTER)
        self.cover_hint_open_btn.connect(
            "clicked", lambda *_: self.show_options(initial_tab="covers"))
        bar.pack_start(self.cover_hint_open_btn, False, False, 0)

        # for good: the launchers' own art is there without a key
        self.cover_hint_never_btn = Gtk.Button(label=self._("cover_hint_never"))
        self.cover_hint_never_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.cover_hint_never_btn.set_valign(Gtk.Align.CENTER)
        self.cover_hint_never_btn.connect("clicked", lambda *_: self._never_show_cover_hint_bar())
        bar.pack_start(self.cover_hint_never_btn, False, False, 0)

        self.cover_hint_close_btn = Gtk.Button()
        self.cover_hint_close_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.cover_hint_close_btn.set_image(self._icon_image("window-close-symbolic", Gtk.IconSize.BUTTON))
        self.cover_hint_close_btn.set_tooltip_text(self._("dismiss_tooltip"))
        self.cover_hint_close_btn.get_accessible().set_name(self._("dismiss_tooltip"))
        self.cover_hint_close_btn.connect("clicked", lambda *_: self._dismiss_cover_hint_bar())
        bar.pack_end(self.cover_hint_close_btn, False, False, 0)

        return bar

    def _dismiss_cover_hint_bar(self):
        self._cover_hint_dismissed = True
        self.cover_hint_bar.hide()

    def _never_show_cover_hint_bar(self):
        self.settings["cover_hint_hidden"] = True
        save_settings(self.settings)
        self._dismiss_cover_hint_bar()

    def _update_cover_hint_bar_visibility(self):
        if not hasattr(self, "cover_hint_bar"):
            return
        should_show = (self.show_covers and not self.api_key and not self._cover_hint_dismissed
                       and not self.settings.get("cover_hint_hidden"))
        self._set_bar_visible(self.cover_hint_bar, should_show)

    def _make_black_cover(self, width: int, height: int) -> GdkPixbuf.Pixbuf:
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, width, height)
        pixbuf.fill(0x000000ff)
        return pixbuf

    def _make_no_cover_placeholder(self, width: int, height: int, show_text: bool = True,
                                     game_title: Optional[str] = None) -> GdkPixbuf.Pixbuf:
        """Shown instead of a flat black square when no cover file
        exists on disk (no SteamGridDB API key configured yet, a
        search came back with nothing, or the file failed to decode) -
        a themed card with an icon and (usually) a short label, so it
        reads as "nothing found here" rather than "something's broken".

        Two different messages depending on WHY there's no art: if no
        API key is configured at all, nothing was ever attempted, so
        the label says so ("no_cover_no_api_key_text") rather than
        implying a search came up empty ("no_cover_placeholder_text").
        game_title overrides both of those with the game's own name
        instead, but ONLY when show_game_titles is off - with titles
        showing normally underneath every card, repeating the name
        here too would be redundant; with them off, this placeholder
        is the only place left to see which game a blank cover
        actually belongs to, so it takes priority over the generic
        "no cover found" message rather than sitting alongside it.

        show_text=False drops the label entirely and just centers the
        icon - used for icon-sized placeholders (list view rows, the
        small per-game icon badge) where the text has no room to
        render and previously got cut off mid-word. This is also why
        game_title is ignored whenever show_text is False: those
        callers never want a label at all, regardless of the
        show_game_titles setting.

        Built via a Gtk.OffscreenWindow (render a real widget tree,
        then snapshot it to a pixbuf) rather than drawing directly with
        cairo - see _round_pixbuf_corners's own docstring for why cairo
        IS used there despite this: pixel-level corner clipping has no
        real Gtk-only alternative, but this card doesn't need it.

        The label ellipsizes instead of wrapping - a wrapping label can
        end up wanting MORE horizontal space than set_size_request()
        actually caps (that's only a minimum hint, not a hard limit).
        The explicit scale_simple() at the end is a second,
        unconditional safety net for the same problem - whatever GTK
        decided this widget's natural size should be, the returned
        pixbuf is forced back to exactly width x height regardless.
        The game-title variant is the one exception: it wraps (up to
        3 lines) rather than ellipsizing after a single character, so
        an actual title has a chance to stay legible instead of being
        cut down to one initial - the scale_simple() safety net still
        guarantees the final pixbuf is exactly width x height either
        way, so this can't overflow the card even if the wrapped text
        wanted more vertical space than height allows.

        Falls back to the plain black square if the offscreen render
        comes back empty for any reason - a slightly wrong-colored
        placeholder box beats no cover image loading at all."""
        if cairo is not None:
            return self._draw_no_cover(width, height, show_text, game_title)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.get_style_context().add_class("no-cover-placeholder")
        box.set_size_request(width, height)
        icon_px = max(48, min(200, min(width, height) // 2))
        # image-off-symbolic is a bundled Lucide SVG (see ICON_MAP in
        # icons.py) - swapped in from image-x-generic-symbolic per
        # request, since a crossed-out image reads more clearly as
        # "nothing here" than a plain picture-frame glyph does.
        icon = self._icon_image_at_size("image-off-symbolic", icon_px)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        box.pack_start(icon, True, True, 0)
        if show_text:
            if game_title and not self.show_game_titles:
                label = Gtk.Label(label=game_title)
                label.get_style_context().add_class("no-cover-placeholder-title-label")
                label.set_line_wrap(True)
                label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                label.set_lines(3)
                label.set_ellipsize(Pango.EllipsizeMode.END)
                # set_size_request() below is only a MINIMUM hint, not
                # a hard cap - a wrapping label left free to pick its
                # own natural width can still ask to be wider than
                # that, and the unconditional scale_simple() safety
                # net at the end of this method would then squash the
                # WHOLE rendered box (icon included) down to fit,
                # distorting it rather than just the text. Capping the
                # character width here to what actually fits this
                # box's own pixel width keeps the label from ever
                # requesting more than it has - roughly 8px/character
                # is a safe (slightly conservative) average glyph
                # width for this label's font size.
                label.set_max_width_chars(max(4, (width - 12) // 8))
                label.set_margin_start(6)
                label.set_margin_end(6)
            else:
                text_key = "no_cover_no_api_key_text" if not self.api_key else "no_cover_placeholder_text"
                label = Gtk.Label(label=self._(text_key))
                label.get_style_context().add_class("no-cover-placeholder-label")
                label.set_line_wrap(False)
                label.set_ellipsize(Pango.EllipsizeMode.END)
                label.set_max_width_chars(1)
            label.set_justify(Gtk.Justification.CENTER)
            box.pack_start(label, False, False, 0)

        offscreen = Gtk.OffscreenWindow()
        offscreen.add(box)
        offscreen.show_all()
        offscreen.resize(width, height)
        deadline = time.monotonic() + 0.5
        pixbuf = None
        while time.monotonic() < deadline:
            while Gtk.events_pending():
                Gtk.main_iteration()
            pixbuf = offscreen.get_pixbuf()
            if pixbuf is not None:
                break
        offscreen.destroy()
        if pixbuf is None:
            return self._make_black_cover(width, height)
        if pixbuf.get_width() != width or pixbuf.get_height() != height:
            pixbuf = pixbuf.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
        return pixbuf

    def _draw_no_cover(self, width: int, height: int, show_text: bool,
                       game_title: Optional[str]) -> GdkPixbuf.Pixbuf:
        """A game without art gets a cover of its own: a gradient in a
        colour taken from its title (the same one Console Mode uses), the
        title large in the middle and a small "no cover" note under it.
        Icon-sized, just its initials."""
        title = (game_title or "").strip()
        # the same colour as Console Mode's stand-in (home_mixin._title_hue)
        hue = (zlib.crc32(title.encode()) % 1000) / 1000 if title else 0.47
        hidpi = ui_scale()          # drawn at width x height, with the screen's own pixels
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width * hidpi, height * hidpi)
        cr = cairo.Context(surface)
        cr.scale(hidpi, hidpi)
        grad = cairo.LinearGradient(0, 0, width, height)
        grad.add_color_stop_rgb(0, *colorsys.hsv_to_rgb(hue, 0.55, 0.55))
        grad.add_color_stop_rgb(1, *colorsys.hsv_to_rgb((hue + 0.12) % 1, 0.70, 0.22))
        cr.set_source(grad)
        cr.paint()
        # a soft light from the top left, so it reads as a lit surface
        light = cairo.RadialGradient(width * 0.2, height * 0.12, 0, width * 0.2, height * 0.12, max(width, height))
        light.add_color_stop_rgba(0, 1, 1, 1, 0.16)
        light.add_color_stop_rgba(0.6, 1, 1, 1, 0)
        cr.set_source(light)
        cr.paint()
        base = self.get_pango_context().get_font_description().copy()

        def layout_for(text: str, size: float, bold: bool, max_w: float, lines: int):
            layout = PangoCairo.create_layout(cr)
            desc = base.copy()
            desc.set_absolute_size(max(6.0, size) * Pango.SCALE)
            desc.set_weight(Pango.Weight.HEAVY if bold else Pango.Weight.SEMIBOLD)
            layout.set_font_description(desc)
            layout.set_width(int(max_w * Pango.SCALE))
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
            layout.set_alignment(Pango.Alignment.CENTER)
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            layout.set_height(-lines)
            layout.set_text(text, -1)
            return layout

        if not show_text or width < 90:
            # icon-sized: the initials
            # the first letter or digit of the first two words ("[Prototype]" -> "P")
            firsts = [next(c for c in w if c.isalnum()) for w in title.split() if any(c.isalnum() for c in w)]
            initials = "".join(firsts[:2]).upper() or "?"
            layout = layout_for(initials, min(width, height) * 0.38, True, width, 1)
            tw, th = layout.get_pixel_size()
            cr.set_source_rgba(1, 1, 1, 0.92)
            cr.move_to(0, (height - th) / 2)
            PangoCairo.show_layout(cr, layout)
        else:
            pad = width * 0.1
            layout = layout_for(title or "?", width / 9.5, True, width - 2 * pad, 4)
            tw, th = layout.get_pixel_size()
            top = height * 0.40 - th / 2
            cr.set_source_rgba(0, 0, 0, 0.35)          # a shadow under the title
            cr.move_to(pad + 1, top + 2)
            PangoCairo.show_layout(cr, layout)
            cr.set_source_rgba(1, 1, 1, 0.96)
            cr.move_to(pad, top)
            PangoCairo.show_layout(cr, layout)
            # the note, in a small pill well above the card's own caption
            note = self._("no_cover_no_api_key_text" if not self.api_key else "no_cover_placeholder_text")
            small = layout_for(note, max(9.0, width / 19), False, width - 2 * pad, 1)
            nw, nh = small.get_pixel_size()
            px, py = (width - nw) / 2 - 10, top + th + height * 0.06
            r = (nh + 8) / 2
            cr.new_sub_path()
            cr.arc(px + r, py + r, r, math.pi / 2, 3 * math.pi / 2)
            cr.arc(px + nw + 20 - r, py + r, r, -math.pi / 2, math.pi / 2)
            cr.close_path()
            cr.set_source_rgba(0, 0, 0, 0.28)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.78)
            cr.move_to(pad, py + 4)
            PangoCairo.show_layout(cr, small)
        surface.flush()
        pixbuf = hidpi_pixbuf(Gdk.pixbuf_get_from_surface(surface, 0, 0, width * hidpi, height * hidpi), hidpi)
        return pixbuf if pixbuf is not None else self._make_black_cover(width, height)

    _COVER_CORNER_RADIUS = 8  # matches .game-cover's own border-radius in app.py

    _TYPICAL_COVER_ASPECT = 2 / 3  # SteamGridDB's portrait grid covers (600x900)

    def _fit_cover_aspect(self, w: int, h: int) -> Tuple[int, int]:
        """Scales a (w, h) box down to a typical 2:3 cover aspect ratio,
        the same way GdkPixbuf.Pixbuf.new_from_file_at_size() shrinks a
        REAL cover image to fit within (w, h) while keeping ITS OWN
        aspect ratio, rather than stretching it to exactly w x h.

        _make_no_cover_placeholder needs to be pre-shrunk to match this
        before being generated - it has no "original image" of its own
        to preserve the aspect of, so left at the raw w x h it always
        came out wider than every real cover sharing the same (w, h)
        box (cover_w, cover_h is a 3:4 box, not 2:3 - most real covers
        end up narrower than that box's own width once their aspect is
        preserved). Reported with a screenshot: a game with no
        downloadable cover had a visibly wider card than its neighbors,
        throwing off the whole row's FlowBox column widths - this was
        it, not the placeholder's own pixbuf coming out oversized (that
        part was already correct)."""
        if h <= 0:
            return w, h
        if w / h > self._TYPICAL_COVER_ASPECT:
            return max(1, round(h * self._TYPICAL_COVER_ASPECT)), h
        return w, h

    # --- shared artwork-resolution helpers ---------------------------
    #
    # update_cover_image/_create_cover_image and update_game_icon_image/
    # _create_game_icon_image each used to carry their own near-
    # identical copy of "find the cached file, try to load it, fall
    # back to the placeholder if that fails" - every fix to this logic
    # (the aspect-fit shrink, corner rounding, the oversized
    # placeholder, icon-vs-cover text) had to be applied in up to 4
    # separate places. These four methods are the single place that
    # logic lives now; the four render paths above just call into them.

    def _find_cached_artwork(self, directory: Path, uid: str) -> Optional[Path]:
        """The first file matching {uid}.* in `directory` (covers_dir
        or game_icons_dir), or None if nothing's cached yet."""
        found = art_files_for(directory, uid)
        return found[0] if found else None

    def _resolve_cover_pixbuf(self, w: int, h: int, cached: Optional[Path],
                               log_context: Optional[str] = None,
                               game_title: Optional[str] = None) -> GdkPixbuf.Pixbuf:
        """A ready-to-display, rounded STATIC cover pixbuf loaded from
        `cached`, or the (also rounded, aspect-fit) "no cover"
        placeholder if `cached` is None or fails to decode.

        Does NOT handle animated (.gif/.apng/.webp) covers - the
        caller checks the extension and routes those to
        _show_first_frame/_queue_animation before ever reaching this;
        animation itself stays out of this shared path since it's the
        most timing-sensitive part of the whole cover system.

        log_context, if given, is logged (at debug level) on a decode
        failure - used by _show_first_frame, which wants that logged,
        unlike the plain update/create paths, which stay quiet about
        an ordinary "no art yet" case.

        game_title, if given, is passed straight through to the "no
        cover" placeholder - see _make_no_cover_placeholder's own
        docstring for why it only actually shows up there when
        show_game_titles is off."""
        if cached is not None:
            for attempt in range(2):
                try:
                    # the screen's own pixels (see gi_setup.hidpi_pixbuf)
                    scale = ui_scale()
                    pb = hidpi_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_size(str(cached), w * scale, h * scale),
                                      scale)
                    return self._round_pixbuf_corners(pb)
                except Exception as e:
                    if log_context:
                        logger.debug(f"{log_context}: {e}")
                    if attempt or not _flatten_undecodable_apng(cached, cached.parent == self.covers_dir):
                        break
        ph_w, ph_h = self._fit_cover_aspect(w, h)
        return self._round_pixbuf_corners(self._make_no_cover_placeholder(ph_w, ph_h, game_title=game_title))

    def _load_static_icon_source(self, cached: Optional[Path]) -> Optional[GdkPixbuf.Pixbuf]:
        """The raw, un-scaled source image for an icon - always via
        get_static_image() (icons never animate, even from a .gif/
        .apng/.webp source file). Loaded once and handed to
        _resolve_icon_pixbuf for each differently-sized widget a UID
        might be registered against (e.g. a big card badge and a small
        list-view row sharing one game) - re-decoding the same file
        from disk once per widget would undo the point of sharing this
        logic at all. None if there's no cached file or it fails to
        decode - the caller falls back to the placeholder."""
        if cached is None:
            return None
        try:
            animation = GdkPixbuf.PixbufAnimation.new_from_file(str(cached))
            return animation.get_static_image()
        except Exception:
            return None

    def _resolve_icon_pixbuf(self, resolved_size: int,
                              src_pixbuf: Optional[GdkPixbuf.Pixbuf],
                              game_title: Optional[str] = None) -> GdkPixbuf.Pixbuf:
        """Scales an already-loaded source image (from
        _load_static_icon_source) to fit resolved_size and rounds its
        corners, or builds/rounds the "no cover" placeholder (without
        its label - icon-sized placeholders never show text, see
        _make_no_cover_placeholder's own docstring) if src_pixbuf is
        None. game_title gives the placeholder its own colour and
        initials; without it every game would get the same "?" square."""
        if src_pixbuf is not None:
            pw, ph = src_pixbuf.get_width(), src_pixbuf.get_height()
            frame = src_pixbuf
            hidpi = ui_scale()
            target = resolved_size * hidpi       # the screen's own pixels
            if pw > 0 and ph > 0:
                scale = min(target / pw, target / ph)
                new_w = max(1, int(pw * scale))
                new_h = max(1, int(ph * scale))
                if (new_w, new_h) != (pw, ph):
                    frame = src_pixbuf.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)
            if hidpi > 1:
                # the shared source isn't marked, only this size of it
                frame = hidpi_pixbuf(frame.copy() if frame is src_pixbuf else frame, hidpi)
            return self._round_pixbuf_corners(frame)
        pb = self._make_no_cover_placeholder(resolved_size, resolved_size, show_text=False,
                                             game_title=game_title)
        return self._round_pixbuf_corners(pb)

    def _round_pixbuf_corners(self, pixbuf: GdkPixbuf.Pixbuf, radius: Optional[int] = None) -> GdkPixbuf.Pixbuf:
        """Returns a copy of pixbuf with its four corners clipped to a
        rounded rectangle - GTK3's own CSS border-radius only rounds a
        widget's OWN background/border, not a child Gtk.Image's actual
        pixel content, so without this a cover/icon's square corners
        always poked out past its button's rounded edge (barely visible
        on a real cover, since the button background is transparent and
        the image just fills the whole space regardless - but glaringly
        visible as solid black corners on the flat-colored fallback
        card from _make_no_cover_placeholder).

        Falls back to the plain, unrounded pixbuf if pycairo isn't
        available (see this module's own guarded `import cairo` at the
        top) - unrounded corners beat a hard crash."""
        if cairo is None:
            return pixbuf
        width, height = pixbuf.get_width(), pixbuf.get_height()
        if width <= 0 or height <= 0:
            return pixbuf
        hidpi = pixbuf_scale(pixbuf)            # works on pixels: the radius in them
        r = (radius if radius is not None else self._COVER_CORNER_RADIUS) * hidpi
        r = max(0, min(r, width // 2, height // 2))
        if r == 0:
            return pixbuf
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        cr = cairo.Context(surface)
        cr.move_to(r, 0)
        cr.line_to(width - r, 0)
        cr.arc(width - r, r, r, -math.pi / 2, 0)
        cr.line_to(width, height - r)
        cr.arc(width - r, height - r, r, 0, math.pi / 2)
        cr.line_to(r, height)
        cr.arc(r, height - r, r, math.pi / 2, math.pi)
        cr.line_to(0, r)
        cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.clip()
        raw_set_source_pixbuf(cr, pixbuf, 0, 0)
        cr.paint()
        return hidpi_pixbuf(Gdk.pixbuf_get_from_surface(surface, 0, 0, width, height), hidpi)

    def _steamgrid_search(self, title: str) -> Optional[int]:
        safe_title = urllib.parse.quote(title, safe="")
        url = f"https://www.steamgriddb.com/api/v2/search/autocomplete/{safe_title}"
        data = http_get_json(url, {"Authorization": f"Bearer {self.api_key}", **HEADERS})
        if not data or not data.get("success") or not data.get("data"):
            return None
        results = data["data"]
        lowered = title.strip().lower()
        for entry in results:
            if str(entry.get("name", "")).strip().lower() == lowered:
                return entry["id"]
        return results[0]["id"]

    _SGDB_PATHS = {"cover": "grids", "hero": "heroes", "icon": "icons"}

    def _steamgrid_page(self, kind: str, game_id: int, page: int = 0) -> Tuple[List[Dict], int]:
        """One page (50) of a game's covers, heroes or icons, and how many
        SteamGridDB has in all."""
        extra = "&dimensions=600x900" if kind == "cover" else ""
        url = (f"https://www.steamgriddb.com/api/v2/{self._SGDB_PATHS[kind]}/game/{game_id}"
               f"?types=static,animated{extra}&page={page}")
        data = http_get_json(url, {"Authorization": f"Bearer {self.api_key}", **HEADERS}, timeout=15)
        if not data or not data.get("success") or not data.get("data"):
            return [], 0
        items = []
        for art in data["data"]:
            url_str = art.get("url", "")
            thumb_str = art.get("thumb") or url_str
            # Animated PNGs come back as plain "image/png" with no
            # "animated" flag - only their .webm preview gives them away.
            is_animated = (url_str.lower().endswith(('.gif', '.apng', '.webp'))
                           or thumb_str.lower().endswith('.webm')
                           or art.get("animated") is True)
            items.append({
                "url": url_str,
                "width": art.get("width") or 0,
                "height": art.get("height") or 0,
                "thumb": thumb_str,
                "style": art.get("style", ""),
                "animated": is_animated,
                "author": (art.get("author") or {}).get("name", ""),
                "id": art.get("id") if isinstance(art.get("id"), int) else 0,     # newer uploads, higher ids
            })
        try:
            total = int(data.get("total") or len(items))
        except (TypeError, ValueError):
            total = len(items)
        return items, total

    def _steam_official_art(self, kind: str, game: Optional[GameItem], game_id: Optional[int]) -> List[Dict]:
        """Steam's own art for the game - its store's capsule, hero or
        icon. SteamGridDB has no "official" style, but it knows which
        Steam app a game is (GOG / Epic copies included). [] when the
        game isn't on Steam or nobody answers. Blocks: a worker thread."""
        api = "https://www.steamgriddb.com/api/v2/games"
        appid = game.uid.split(":", 1)[1] if game is not None and game.uid.startswith("steam:") else ""
        if appid.isdigit():
            url = f"{api}/steam/{appid}?platformdata=steam"
        elif game_id is not None:
            url = f"{api}/id/{int(game_id)}?platformdata=steam"
        else:
            return []
        cache = self.__dict__.setdefault("_steam_platform_cache", {})
        if url not in cache:
            data = http_get_json(url, {"Authorization": f"Bearer {self.api_key}", **HEADERS}, timeout=15)
            if not isinstance(data, dict):
                return []           # not kept: asked again next time
            found = ((data.get("data") or {}).get("external_platform_data") or {}).get("steam") or []
            cache[url] = found[0] if found and isinstance(found[0], dict) else None
        entry = cache[url]
        if not entry or not str(entry.get("id", "")).isdigit():
            return []
        appid, meta = str(entry["id"]), entry.get("metadata") or {}
        if kind != "icon":
            # the art the Steam client itself shows now: SteamGridDB's copy
            # of the store data can be years old
            art_url = self._steam_store_asset(appid, "library_capsule" if kind == "cover" else "library_hero")
            if art_url:
                return [{"url": art_url, "thumb": art_url, "width": 0, "height": 0, "style": "official",
                         "animated": False, "author": "Steam", "id": 0}]
        if kind == "icon":
            icon = str(meta.get("clienticon") or "")
            if not re.fullmatch(r"[0-9a-f]{40}", icon):
                return []
            art_url = f"https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon}.ico"
        else:
            path = _steam_asset_path(meta.get("library_capsule_full" if kind == "cover" else "library_hero_full"))
            if path is None:
                return []
            art_url = f"https://shared.steamstatic.com/store_item_assets/steam/apps/{appid}/{path}"
        return [{"url": art_url, "thumb": art_url, "width": 0, "height": 0, "style": "official",
                 "animated": False, "author": "Steam", "id": 0}]

    def _steam_store_asset(self, appid: str, name: str) -> Optional[str]:
        """The current URL of a Steam store asset (library_capsule,
        library_hero - the 2x one when there is one), from Steam's own
        store API; None when it doesn't answer."""
        cache = self.__dict__.setdefault("_steam_store_assets", {})
        if appid not in cache:
            query = json.dumps({"ids": [{"appid": int(appid)}],
                                "context": {"language": "english", "country_code": "US"},
                                "data_request": {"include_assets": True}}, separators=(",", ":"))
            data = http_get_json("https://api.steampowered.com/IStoreBrowseService/GetItems/v1?input_json="
                                 + urllib.parse.quote(query), HEADERS, timeout=15)
            if not isinstance(data, dict):
                return None         # not kept: asked again next time
            items = (data.get("response") or {}).get("store_items") or []
            assets = items[0].get("assets") if items and isinstance(items[0], dict) else None
            cache[appid] = assets if isinstance(assets, dict) else {}
        assets = cache[appid]
        fmt = assets.get("asset_url_format")
        path = assets.get(f"{name}_2x") or assets.get(name)
        if not isinstance(fmt, str) or "${FILENAME}" not in fmt or not isinstance(path, str) \
                or not re.fullmatch(r"[\w./-]+", path) or ".." in path:
            return None
        url = "https://shared.steamstatic.com/store_item_assets/" + fmt.replace("${FILENAME}", path)
        return url if re.fullmatch(r"https://shared\.steamstatic\.com/store_item_assets/steam/apps/\d+/[\w./?=-]+", url) \
            else None

    def download_cover_from_url(self, url: str, target: Path, retries: int = 3,
                                still_wanted: Optional[Callable[[], bool]] = None) -> bool:
        # Only web URLs - a cover_map restored from a backup could
        # otherwise point at file:// (or any other urllib scheme).
        if urllib.parse.urlparse(url).scheme.lower() not in ("http", "https"):
            logger.warning(f"Refusing to download art from a non-web URL: {url!r}")
            return False
        delay = 0.5
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                still = _make_decodable(data)
                if still_wanted is not None and not still_wanted():
                    return False    # another choice was made meanwhile
                _write_atomically(target, lambda tmp: tmp.write_bytes(still))
                if target.parent in (self.covers_dir, self.heroes_dir) and still is not data:
                    _keep_animation(target, data)
                return True
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(delay)
                    delay *= 2
                else:
                    logger.warning(f"Failed to download cover ({url}): {e}")
        return False

    def _games_missing_art(self, games: List[GameItem]) -> List[GameItem]:
        """Installed games with no cover and/or no icon file on disk yet -
        a single directory listing per folder rather than a glob() per
        game, so this stays cheap even with a large library. Catches
        games whose art went missing some OTHER way too (an external
        cache-cleaning tool wiping the old ~/.cache/plunderhub/ art
        location, or a first download attempt that failed) - not just
        ones first_seen doesn't already know about. Relying on "is this
        UID new" alone missed exactly that case: first_seen still
        remembered the game as already-seen, so a re-download never got
        triggered even though the file was gone."""
        existing_covers = {p.stem for p in self.covers_dir.iterdir() if p.is_file()}
        existing_icons = {p.stem for p in self.game_icons_dir.iterdir() if p.is_file()}
        existing_heroes = {p.stem for p in self.heroes_dir.iterdir() if p.is_file()}
        return [g for g in games if g.uid not in existing_covers or g.uid not in existing_icons
                or g.uid not in existing_heroes]

    def _auto_download_missing_covers(self, games: List[GameItem]):
        # without a SteamGridDB key, the art the launchers themselves show
        if not games or not getattr(self, "show_covers", True):
            return
        # once a run per game: what SteamGridDB hasn't got (many games have
        # no hero) was looked up again after every reload ("Download
        # covers" still tries them all)
        tried = self.__dict__.setdefault("_auto_art_tried", set())
        games = [g for g in games if g.uid not in tried]
        running = getattr(self, "_auto_art_thread", None)
        if not games or (running is not None and running.is_alive()):
            return
        tried.update(g.uid for g in games)
        thread = self._auto_art_thread = threading.Thread(
            target=self._auto_download_covers_thread,
            args=(games,),
            daemon=True,
        )
        thread.start()

    def _auto_download_covers_thread(self, new_games: List[GameItem]):
        """Whatever art a game lacks: its launcher's own first, then, with a
        SteamGridDB key, the official one there, else its most popular still."""
        launcher = sgdb = 0
        for game in new_games:
            missing = self._missing_art_kinds(game)
            if not missing:
                continue
            got = self._fetch_launcher_art(game, missing)
            launcher += bool(got)
            missing -= got
            if missing and self.api_key:
                try:
                    if self._fetch_sgdb_art(game, missing):
                        sgdb += 1
                except Exception as e:
                    logger.info(f"SteamGridDB art skipped for '{game.title}': {e}")
            GLib.idle_add(self.update_cover_image, game.uid)
            GLib.idle_add(self.update_game_icon_image, game.uid)
            GLib.idle_add(self.update_hero_image, game.uid)
        if launcher or sgdb:
            logger.info(f"Missing art filled: {launcher} game(s) from their launcher, {sgdb} from SteamGridDB.")
        return launcher, sgdb

    def _missing_art_kinds(self, game: GameItem) -> set:
        return {kind for kind, folder in (("cover", self.covers_dir), ("icon", self.game_icons_dir),
                                          ("hero", self.heroes_dir))
                if not art_files_for(folder, game.uid)}

    def _fetch_sgdb_art(self, game: GameItem, kinds: set) -> set:
        """The kinds given, from SteamGridDB: the official art first (Steam's
        own, an icon of SteamGridDB's official style), else the first still
        one in its own (most popular first) order. The kinds that came.
        Blocks: a worker thread."""
        game_id = self._steamgrid_search(game.title)
        if game_id is None:
            return set()
        got = set()
        for kind in kinds:
            picks = self._steam_official_art(kind, game, game_id)
            if not picks:
                page, _total = self._steamgrid_page(kind, game_id, 0)
                stills = [i for i in page if not i.get("animated")]
                picks = [i for i in stills if i.get("style") == "official"] + stills
            if not picks:
                continue
            url = picks[0]["url"]
            ext = _ext_from_url(url)
            folder = {"cover": self.covers_dir, "hero": self.heroes_dir, "icon": self.game_icons_dir}[kind]
            target = folder / f"{game.uid}{ext}"
            if not self.download_cover_from_url(url, target):
                continue
            _remove_other_art(folder, game.uid, keep=target)
            if kind == "cover":
                self._remember_cover_source(game.uid, url, ext, picks[0].get("style", ""), False)
            elif kind == "icon":
                self._remember_game_icon_source(game.uid, url, ext, False)
            else:
                self._remember_hero_source(game.uid, url, ext, False)
            got.add(kind)
        return got

    def _fill_missing_art_clicked(self, button: Gtk.Button):
        """Settings > Covers: every game's missing cover, hero and icon -
        the launcher's own, then (with a key) SteamGridDB's."""
        games = [g for section in getattr(self, "_cached_launchers", None) or [] for g in section["games"]]
        lacking = self._games_missing_art(games) if games else []
        parent = getattr(self, "_options_dialog", None)
        if not lacking:
            self._show_info_dialog(self._("art_fill_none"), icon_name="emblem-ok-symbolic", transient_for=parent)
            return
        self._start_art_fill(lacking, parent, button)

    def _start_art_fill(self, lacking, parent, button: Optional[Gtk.Button] = None):
        """Fetches the art the given games lack in the background: the
        Covers page's button says so while it runs, and a note tells how
        it went - started from that button or from the offer after a key
        is saved alike."""
        if getattr(self, "_art_fill_running", False):
            return
        self._art_fill_running = True
        # the button is an icon and a label: the label's text is swapped
        text_label = None
        if button is not None:
            found = []

            def find(w):
                if isinstance(w, Gtk.Label):
                    found.append(w)
                elif isinstance(w, Gtk.Container):
                    w.foreach(find)
            find(button)
            text_label = found[0] if found else None
        label = text_label.get_text() if text_label is not None else None
        if button is not None:
            button.set_sensitive(False)
            if text_label is not None:
                text_label.set_text(self._("art_fill_running"))

        def work():
            try:
                self._auto_download_covers_thread(lacking)
            finally:
                GLib.idle_add(done)

        def done():
            self._art_fill_running = False
            # the Settings window may have been closed meanwhile
            if button is not None and button.get_toplevel().get_visible():
                button.set_sensitive(True)
                if text_label is not None and label is not None:
                    text_label.set_text(label)
            if parent is not None and not parent.get_visible():
                parent_now = None
            else:
                parent_now = parent
            left = len(self._games_missing_art(lacking))
            self._art_fill_result(len(lacking) - left, left, parent_now)
            return False
        threading.Thread(target=work, daemon=True, name="plunderhub-art-fill").start()

    def _art_fill_result(self, filled: int, left: int, parent=None):
        """How a missing-art fetch went: two numbers on tiles (no "1 games"
        to word around), and without a key the hint that one brings the rest."""
        from plunderhub.ui.home_mixin import WARNING_RGB, _accent_rgb
        _ = self._
        hexed = lambda rgb: "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        tiles = Gtk.Box(spacing=10, homogeneous=True)
        for number, caption, colour in ((filled, _("art_fill_filled"), hexed(_accent_rgb(self))),
                                        (left, _("art_fill_missing"), hexed(WARNING_RGB) if left else None)):
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            tile.get_style_context().add_class("art-result-tile")
            num = Gtk.Label(xalign=0)
            if colour:
                num.set_markup(f'<span foreground="{colour}">{number}</span>')
            else:
                num.set_text(str(number))
                num.set_opacity(0.55)
            num.get_style_context().add_class("art-result-num")
            cap = Gtk.Label(label=caption, xalign=0)
            cap.get_style_context().add_class("art-result-cap")
            tile.pack_start(num, False, False, 0)
            tile.pack_start(cap, False, False, 0)
            tiles.pack_start(tile, True, True, 0)
        col.pack_start(tiles, False, False, 0)
        if left and not self.api_key:
            hint = Gtk.Box(spacing=8)
            hint.pack_start(Gtk.Image.new_from_icon_name("dialog-password-symbolic", Gtk.IconSize.MENU),
                            False, False, 0)
            text = Gtk.Label(label=_("art_fill_need_key"), xalign=0)
            text.set_line_wrap(True)
            text.get_style_context().add_class("art-result-hint")
            hint.pack_start(text, True, True, 0)
            col.pack_start(hint, False, False, 0)
        col.show_all()
        heading = _("art_fill_result_title") if filled else _("art_fill_result_none")
        body = _("art_fill_result_all") if not left else None
        self._card_dialog(heading, body, [(_("dialog_ok"), Gtk.ResponseType.OK, "accent")],
                          icon_name="image-x-generic-symbolic", transient_for=parent, extra=col)

    def _offer_missing_art_download(self, parent=None):
        """After a key is saved: the games still lacking a cover, hero or
        icon - asked once, then fetched in the background."""
        games = [g for section in getattr(self, "_cached_launchers", None) or [] for g in section["games"]]
        lacking = self._games_missing_art(games) if games else []
        if not lacking:
            return
        _ = self._
        if self._card_dialog(_("art_fill_title"), _("art_fill_text"),
                             [(_("art_fill_no"), Gtk.ResponseType.CANCEL, "ghost"),
                              (_("art_fill_yes"), Gtk.ResponseType.OK, "accent")],
                             icon_name="image-x-generic-symbolic", transient_for=parent) != Gtk.ResponseType.OK:
            return
        # the Covers page's button shows it's running, the note how it went
        button = getattr(self, "_art_fill_btn", None)
        if button is not None and not button.get_toplevel().get_visible():
            button = None
        self._start_art_fill(lacking, parent, button)

    def _flush_deferred_map_saves(self):
        with self._cover_map_lock:
            pending_cover = self._pending_cover_map_save
            pending_icon = self._pending_game_icon_map_save
            self._pending_cover_map_save = False
            self._pending_game_icon_map_save = False
            if pending_cover:
                save_cover_map(self.cover_map)
            if pending_icon:
                save_game_icon_map(self.game_icon_map)
            if self._pending_hero_map_save:
                self._pending_hero_map_save = False
                save_hero_map(self.hero_map)

    def _remember_cover_source(self, uid: str, url: str, ext: str, style: str = "", animated: bool = False):
        with self._cover_map_lock:
            self.cover_map[uid] = {
                "url": url,
                "ext": ext,
                "style": style,
                "animated": bool(animated),
            }
            self._covers_restore_failed.discard(uid)
            if self._defer_map_saves:
                self._pending_cover_map_save = True
                return
            save_cover_map(self.cover_map)

    def _remember_game_icon_source(self, uid: str, url: str, ext: str, animated: bool = False):
        with self._cover_map_lock:
            self.game_icon_map[uid] = {
                "url": url,
                "ext": ext,
                "animated": bool(animated),
            }
            self._game_icons_restore_failed.discard(uid)
            if self._defer_map_saves:
                self._pending_game_icon_map_save = True
                return
            save_game_icon_map(self.game_icon_map)

    def _fetch_launcher_art(self, game: GameItem, kinds: set) -> set:
        """The kinds of art (cover / hero / icon) the game's own launcher
        has, put in place where they're missing; the kinds that came.
        Blocks: a worker thread."""
        from plunderhub.core.launcher_art import art_for
        found = art_for(game)
        appid = game.uid.split(":", 1)[1] if game.uid.startswith("steam:") else ""
        got = set()
        for kind in kinds:
            src = found.get(kind)
            if src is None and appid.isdigit() and kind != "icon":
                # not in the Steam client's cache (yet): Steam's store has it
                src = self._steam_store_asset(appid, "library_capsule" if kind == "cover" else "library_hero")
            if not src:
                continue
            folder, art_map, save = {"cover": (self.covers_dir, "cover_map", save_cover_map),
                                     "hero": (self.heroes_dir, "hero_map", save_hero_map),
                                     "icon": (self.game_icons_dir, "game_icon_map", save_game_icon_map)}[kind]
            web = src.startswith(("https://", "http://"))
            ext = _ext_from_url(src, ".jpg") if web else _safe_image_ext(Path(src).suffix, ".jpg")
            target = folder / f"{game.uid}{ext}"
            if web:
                if not self.download_cover_from_url(src, target):
                    continue
                entry = {"url": src, "ext": ext, "style": "launcher", "animated": False}
            else:
                if _copy_art_file(src, target) is not None:
                    continue
                entry = {"launcher": True}      # a local file: nothing to fetch again
            _remove_other_art(folder, game.uid, keep=target)
            with self._cover_map_lock:
                getattr(self, art_map)[game.uid] = entry
                save(getattr(self, art_map))
            got.add(kind)
        if got:
            logger.info(f"Art from {game.launcher} itself for '{game.title}': {', '.join(sorted(got))}")
        return got

    def _remember_hero_source(self, uid: str, url: str, ext: str, animated: bool = False):
        with self._cover_map_lock:
            self.hero_map[uid] = {"url": url, "ext": ext, "animated": bool(animated)}
            self._heroes_restore_failed.discard(uid)
            if self._defer_map_saves:
                self._pending_hero_map_save = True
                return
            save_hero_map(self.hero_map)

    def _maybe_restore_hero(self, uid: str):
        """A hero whose file is gone (but whose source is known) comes back
        on its own, like covers do."""
        if uid in self._heroes_restoring or uid in self._heroes_restore_failed:
            return
        entry = self.hero_map.get(uid)
        if not entry or not entry.get("url"):
            return
        self._heroes_restoring.add(uid)

        def work():
            url = entry.get("url", "")
            target = self.heroes_dir / f"{uid}{_safe_image_ext(entry.get('ext'), default=_ext_from_url(url))}"
            try:
                ok = self.download_cover_from_url(url, target)
            except Exception as e:
                logger.warning(f"Automatic restore of hero failed ({uid}): {e}")
                ok = False
            GLib.idle_add(self._finish_restore_hero, uid, ok)
        threading.Thread(target=work, daemon=True).start()

    def _finish_restore_hero(self, uid: str, ok: bool) -> bool:
        self._heroes_restoring.discard(uid)
        if not ok:
            self._heroes_restore_failed.add(uid)
            return False
        self.update_hero_image(uid)
        return False

    def hero_path(self, uid: str) -> Optional[Path]:
        """uid's hero banner on disk - restored in the background when it's
        known but missing."""
        cached = self._find_cached_artwork(self.heroes_dir, uid)
        if cached is None:
            self._maybe_restore_hero(uid)
        elif apng.is_apng_file(cached):
            # copied in as it came (a picker preview): a still everything
            # can read, the animation kept beside it (_art_animation)
            _flatten_undecodable_apng(cached, keep_animation=True)
        return cached

    def update_hero_image(self, uid: str) -> bool:
        self._home_hero_wanted = None       # a still being made from the old file is dropped
        cache = getattr(self, "_home_hero_cache", None)
        if cache:
            cache.clear()
        rows = getattr(self, "_list_hero_cache", None)
        if rows:
            rows.pop(uid, None)
        getattr(self, "_list_hero_surfaces", {}).pop(uid, None)
        if hasattr(self, "console_view"):
            self.console_view.forget_art(uid)
        hero_area = getattr(self, "_home_hero_area", None)
        if hero_area is not None and hero_area.get_window() is not None:
            hero_area.queue_draw()
        self._notify_game_page(uid, "hero")
        return False

    def _notify_game_page(self, uid: str, kind: str):
        page = getattr(self, "_game_page", None)
        if page is not None and page.game.uid == uid:
            page.art_changed(kind)

    def _maybe_restore_cover(self, uid: str):
        if uid in self._covers_restoring or uid in self._covers_restore_failed:
            return
        entry = self.cover_map.get(uid)
        if not entry or not entry.get("url"):
            return
        self._covers_restoring.add(uid)
        thread = threading.Thread(target=self._restore_cover_thread, args=(uid, entry), daemon=True)
        thread.start()

    def _restore_cover_thread(self, uid: str, entry: Dict):
        url = entry.get("url", "")
        # "ext" may come from a restored backup - never trusted as-is.
        ext = _safe_image_ext(entry.get("ext"), default=_ext_from_url(url))
        target = self.covers_dir / f"{uid}{ext}"
        ok = False
        try:
            ok = self.download_cover_from_url(url, target)
        except Exception as e:
            logger.warning(f"Automatic restore of cover failed ({uid}): {e}")
        GLib.idle_add(self._finish_restore_cover, uid, ok)

    def _finish_restore_cover(self, uid: str, ok: bool = True):
        self._covers_restoring.discard(uid)
        if not ok:
            self._covers_restore_failed.add(uid)
            return
        self.update_cover_image(uid)

    def _maybe_restore_game_icon(self, uid: str):
        if uid in self._game_icons_restoring or uid in self._game_icons_restore_failed:
            return
        entry = self.game_icon_map.get(uid)
        if not entry or not entry.get("url"):
            return
        self._game_icons_restoring.add(uid)
        thread = threading.Thread(target=self._restore_game_icon_thread, args=(uid, entry), daemon=True)
        thread.start()

    def _restore_game_icon_thread(self, uid: str, entry: Dict):
        url = entry.get("url", "")
        # "ext" may come from a restored backup - never trusted as-is.
        ext = _safe_image_ext(entry.get("ext"), default=_ext_from_url(url))
        target = self.game_icons_dir / f"{uid}{ext}"
        ok = False
        try:
            ok = self.download_cover_from_url(url, target)
        except Exception as e:
            logger.warning(f"Automatic restore of game icon failed ({uid}): {e}")
        GLib.idle_add(self._finish_restore_game_icon, uid, ok)

    def _finish_restore_game_icon(self, uid: str, ok: bool = True):
        self._game_icons_restoring.discard(uid)
        if not ok:
            self._game_icons_restore_failed.add(uid)
            return
        self.update_game_icon_image(uid)

    def _update_cover_widgets(self, uid: str):
        if hasattr(self, "console_view"):
            self.console_view.forget_art(uid)
        widgets = self.cover_widgets.get(uid)
        if not widgets:
            return
        cached = self._find_cached_artwork(self.covers_dir, uid)
        if cached is None:
            self._maybe_restore_cover(uid)
        w, h = self.cover_size, int(self.cover_size * 4/3)
        game = self.game_index.get(uid)
        game_title = game.title if game is not None else None
        moving = self._art_animation(cached)
        if moving is not None:
            self._show_first_frame(widgets, cached, w, h, game_title=game_title)
            self._stop_animation(uid)          # at the one size this sets them all to
            self._queue_animation(uid, moving, w, h, widgets)
            return
        self._stop_animation(uid)
        pb = self._resolve_cover_pixbuf(w, h, cached, game_title=game_title)
        for widget in widgets:
            widget.set_from_pixbuf(pb)

    def update_cover_image(self, uid: str):
        self._update_cover_widgets(uid)
        self._notify_game_page(uid, "cover")
        self._home_hero_wanted = None
        cache = getattr(self, "_home_hero_cache", None)
        if cache:
            cache.clear()       # the hero falls back to the cover

    def update_game_icon_image(self, uid: str):
        self._notify_game_page(uid, "icon")
        widgets = self.game_icon_widgets.get(uid)
        if not widgets:
            return
        cached = self._find_cached_artwork(self.game_icons_dir, uid)
        if cached is None:
            self._maybe_restore_game_icon(uid)
        src_pixbuf = self._load_static_icon_source(cached)
        for widget, size in widgets:
            resolved_size = size if size is not None else self.game_icon_size
            widget.set_from_pixbuf(self._resolve_icon_pixbuf(
                resolved_size, src_pixbuf, getattr(widget, "plunderhub_title", None)))

    def _set_cover_from_file(self, game: GameItem):
        from plunderhub.ui.image_picker import pick_image_file
        src = pick_image_file(self, "cover", game.title)
        if not src:
            return
        self._art_turn("cover", game.uid)     # a full download still coming in mustn't land over it
        target = self.covers_dir / f"{game.uid}{_safe_image_ext(os.path.splitext(src)[1])}"
        error = _copy_art_file(src, target)
        if error is not None:
            self._show_info_dialog(f"{src}\n\n{error}", message_type=Gtk.MessageType.ERROR)
            return
        _remove_other_art(self.covers_dir, game.uid, keep=target)
        self.cover_map[game.uid] = {"custom": True}
        save_cover_map(self.cover_map)
        self._covers_restore_failed.discard(game.uid)
        self.update_cover_image(game.uid)

    def _set_game_icon_from_file(self, game: GameItem):
        from plunderhub.ui.image_picker import pick_image_file
        src = pick_image_file(self, "icon", game.title)
        if not src:
            return
        self._art_turn("icon", game.uid)     # a full download still coming in mustn't land over it
        target = self.game_icons_dir / f"{game.uid}{_safe_image_ext(os.path.splitext(src)[1])}"
        error = _copy_art_file(src, target)
        if error is not None:
            self._show_info_dialog(f"{src}\n\n{error}", message_type=Gtk.MessageType.ERROR)
            return
        _remove_other_art(self.game_icons_dir, game.uid, keep=target)
        self.game_icon_map[game.uid] = {"custom": True}
        save_game_icon_map(self.game_icon_map)
        self._game_icons_restore_failed.discard(game.uid)
        self.update_game_icon_image(game.uid)

    # --- the game page's art: from a file, from SteamGridDB, back to automatic

    def _art_slot(self, kind: str):
        """(folder, source map, its save function, the refresh for it)."""
        if kind == "cover":
            return self.covers_dir, self.cover_map, save_cover_map, self.update_cover_image
        if kind == "icon":
            return self.game_icons_dir, self.game_icon_map, save_game_icon_map, self.update_game_icon_image
        return self.heroes_dir, self.hero_map, save_hero_map, self.update_hero_image

    def _set_art_from_file(self, kind: str, game: GameItem):
        if kind == "cover":
            self._set_cover_from_file(game)
        elif kind == "icon":
            self._set_game_icon_from_file(game)
        else:
            self._set_hero_from_file(game)

    def _set_hero_from_file(self, game: GameItem):
        from plunderhub.ui.image_picker import pick_image_file
        src = pick_image_file(self, "hero", game.title)
        if not src:
            return
        self._art_turn("hero", game.uid)     # a full download still coming in mustn't land over it
        target = self.heroes_dir / f"{game.uid}{_safe_image_ext(os.path.splitext(src)[1])}"
        error = _copy_art_file(src, target)
        if error is not None:
            self._show_info_dialog(f"{src}\n\n{error}", message_type=Gtk.MessageType.ERROR)
            return
        _remove_other_art(self.heroes_dir, game.uid, keep=target)
        self.hero_map[game.uid] = {"custom": True}
        save_hero_map(self.hero_map)
        self._heroes_restore_failed.discard(game.uid)
        self.update_hero_image(game.uid)

    def _art_turn(self, kind: str, uid: str) -> Callable[[], bool]:
        """A new choice of a game's cover / icon / hero: a full-size download
        started for an earlier one then mustn't land over it (it asks the
        returned check before writing)."""
        turns = self.__dict__.setdefault("_art_turns", {})
        turn = turns[(kind, uid)] = turns.get((kind, uid), 0) + 1
        return lambda: turns.get((kind, uid)) == turn

    def _apply_sgdb_art(self, kind: str, game: GameItem, item: Dict, preview: Path):
        """What the picker chose: its preview straight away, the full file
        once it's downloaded."""
        img_data = {"file_path": preview, "url": item.get("url", ""), "style": item.get("style", ""),
                    "animated": item.get("animated", False)}
        if kind == "cover":
            self._on_cover_selected(img_data, game, None)
            return
        if kind == "icon":
            self._on_game_icon_selected(img_data, game, None)
            return
        wanted = self._art_turn("hero", game.uid)
        fallback = self.heroes_dir / f"{game.uid}{_safe_image_ext(preview.suffix)}"
        if _copy_art_file(str(preview), fallback) is None:
            _remove_other_art(self.heroes_dir, game.uid, keep=fallback)
        self.update_hero_image(game.uid)
        url = img_data["url"]
        if not url:
            return
        ext = _ext_from_url(url, default=fallback.suffix)
        self._remember_hero_source(game.uid, url, ext, img_data["animated"])

        def full():
            target = self.heroes_dir / f"{game.uid}{ext}"
            try:
                ok = self.download_cover_from_url(url, target, still_wanted=wanted)
            except Exception:
                ok = False
            if ok and wanted():
                _remove_other_art(self.heroes_dir, game.uid, keep=target)
                GLib.idle_add(self.update_hero_image, game.uid)
        threading.Thread(target=full, daemon=True).start()

    def _reset_art(self, kind: str, game: GameItem):
        """Drops a hand-picked cover, hero or icon and puts back the one
        the game's own launcher shows (none there: the placeholder)."""
        folder, art_map, save, refresh = self._art_slot(kind)
        self._art_turn(kind, game.uid)
        for old in art_files_for(folder, game.uid):
            try:
                old.unlink()
            except OSError as e:
                logger.debug(f"Couldn't remove {old}: {e}")
        with self._cover_map_lock:
            art_map.pop(game.uid, None)
            save(art_map)
        refresh(game.uid)

        def work():
            try:
                self._fetch_launcher_art(game, {kind})
            except Exception as e:
                logger.info(f"Couldn't fetch a new {kind} for {game.title!r}: {e}")
            GLib.idle_add(lambda: refresh(game.uid) and False)
        threading.Thread(target=work, daemon=True).start()

    def _register_cover_widget(self, uid: str, image: Gtk.Image) -> None:
        widgets = self.cover_widgets.setdefault(uid, [])
        widgets.append(image)
        # whether the bubbles behind it can't show through (see
        # App._bubble_occluders)
        self._cover_opaque[uid] = _art_is_opaque(self._find_cached_artwork(self.covers_dir, uid))

        def forget(widget):
            remaining = self.cover_widgets.get(uid)
            if not remaining:
                return
            try:
                remaining.remove(widget)
            except ValueError:
                return
            if not remaining:
                self.cover_widgets.pop(uid, None)
                self._stop_animation(uid)

        image.connect("destroy", forget)

    def _show_first_frame(self, widgets, path: Path, width: int, height: int,
                           game_title: Optional[str] = None) -> None:
        pixbuf = self._resolve_cover_pixbuf(width, height, path, log_context=f"Couldn't read first frame of {path}",
                                             game_title=game_title)
        for widget in widgets:
            widget.set_from_pixbuf(pixbuf)

    def _resync_animated_cover_sizes(self) -> None:
        for uid in self._anim_player.playing():
            try:
                self.update_cover_image(uid)
            except Exception as e:
                logger.debug(f"Couldn't resync animated cover {uid}: {e}")

    def _art_animation(self, cached: Optional[Path]) -> Optional[Path]:
        """The file to animate a cover or hero from, if it moves: a GIF /
        APNG / WebP itself, or the APNG kept beside a flattened one - as
        long as nothing replaced the still after it."""
        if cached is None:
            return None
        if cached.suffix.lower() == '.webp':
            # a still WebP has no ANMF frames: played, its one frame was
            # decoded and drawn again every 20 ms for good
            from plunderhub.ui.animated_hero import frame_count
            return cached if (frame_count(cached) or 0) > 1 else None
        if cached.suffix.lower() in ('.gif', '.apng'):
            return cached
        if apng.is_apng_file(cached):
            # copied in as it came (a picker preview): flattened now where
            # GdkPixbuf can't read it, the original kept beside it
            if not _flatten_undecodable_apng(cached, keep_animation=True):
                return cached
        sidecar = animation_sidecar(cached)
        try:
            if sidecar.stat().st_mtime >= cached.stat().st_mtime:
                return sidecar
            sidecar.unlink()                 # stale: the cover changed since
        except OSError:
            pass
        return None

    def _queue_animation(self, uid: str, path: Path, width: int, height: int,
                         widgets: Optional[List[Gtk.Image]] = None) -> None:
        """Plays uid's animated cover on widgets (all of uid's covers by
        default) over the first frame already shown - unless animated
        covers are switched off, when that frame stays."""
        if self.animated_covers:
            self._anim_player.play(uid, path, width, height, widgets or self.cover_widgets.get(uid, []))
        else:
            self._anim_player.stop(uid)

    def _animation_work_pending(self) -> bool:
        return self._anim_player.busy()

    def _on_cover_selected(self, img_data: Dict, game: GameItem, dialog: Gtk.Dialog):
        thumb_path: Path = img_data["file_path"]
        full_url = img_data.get("url") or ""

        fallback_ext = _safe_image_ext(thumb_path.suffix)
        wanted = self._art_turn("cover", game.uid)
        fallback_target = self.covers_dir / f"{game.uid}{fallback_ext}"
        # The current cover is only replaced once the preview is in place.
        if _copy_art_file(str(thumb_path), fallback_target) is None:
            _remove_other_art(self.covers_dir, game.uid, keep=fallback_target)
        self.update_cover_image(game.uid)

        if full_url:
            ext = _ext_from_url(full_url, default=fallback_ext)
            self._remember_cover_source(game.uid, full_url, ext, img_data.get("style", ""), img_data.get("animated", False))
            thread = threading.Thread(
                target=self._download_full_cover_async,
                args=(full_url, game.uid, fallback_target, wanted),
                daemon=True,
            )
            thread.start()

    def _download_full_cover_async(self, url: str, uid: str, fallback_target: Path,
                                          wanted: Optional[Callable[[], bool]] = None):
        target = self.covers_dir / f"{uid}{_ext_from_url(url, default=fallback_target.suffix or '.png')}"
        try:
            ok = self.download_cover_from_url(url, target, still_wanted=wanted)
        except Exception:
            ok = False
        if ok and (wanted is None or wanted()):
            _remove_other_art(self.covers_dir, uid, keep=target)
            GLib.idle_add(self.update_cover_image, uid)

    def _on_game_icon_selected(self, img_data: Dict, game: GameItem, dialog: Gtk.Dialog):
        thumb_path: Path = img_data["file_path"]
        full_url = img_data.get("url") or ""

        fallback_ext = _safe_image_ext(thumb_path.suffix)
        wanted = self._art_turn("icon", game.uid)
        fallback_target = self.game_icons_dir / f"{game.uid}{fallback_ext}"
        # The current icon is only replaced once the preview is in place.
        if _copy_art_file(str(thumb_path), fallback_target) is None:
            _remove_other_art(self.game_icons_dir, game.uid, keep=fallback_target)
        self.update_game_icon_image(game.uid)

        if full_url:
            ext = _ext_from_url(full_url, default=fallback_ext)
            self._remember_game_icon_source(game.uid, full_url, ext, img_data.get("animated", False))
            thread = threading.Thread(
                target=self._download_full_game_icon_async,
                args=(full_url, game.uid, fallback_target, wanted),
                daemon=True,
            )
            thread.start()

    def _download_full_game_icon_async(self, url: str, uid: str, fallback_target: Path,
                                              wanted: Optional[Callable[[], bool]] = None):
        target = self.game_icons_dir / f"{uid}{_ext_from_url(url, default=fallback_target.suffix or '.png')}"
        try:
            ok = self.download_cover_from_url(url, target, still_wanted=wanted)
        except Exception:
            ok = False
        if ok and (wanted is None or wanted()):
            _remove_other_art(self.game_icons_dir, uid, keep=target)
            GLib.idle_add(self.update_game_icon_image, uid)

    def _create_cover_image(self, game: GameItem, size: Tuple[int, int]) -> Gtk.Image:
        img = Gtk.Image()
        w, h = size
        self._register_cover_widget(game.uid, img)
        cached = self._find_cached_artwork(self.covers_dir, game.uid)
        if cached is None:
            self._maybe_restore_cover(game.uid)
        moving = self._art_animation(cached)
        if moving is not None:
            self._show_first_frame([img], cached, w, h, game_title=game.title)
            self._queue_animation(game.uid, moving, w, h, [img])
            return img
        self._stop_animation(game.uid)
        img.set_from_pixbuf(self._resolve_cover_pixbuf(w, h, cached, game_title=game.title))
        return img

    def _create_game_icon_image(self, game: GameItem, size: Optional[int] = None) -> Gtk.Image:
        img = Gtk.Image()
        img.plunderhub_title = game.title   # for the placeholder, see update_game_icon_image
        self.game_icon_widgets.setdefault(game.uid, []).append((img, size))
        cached = self._find_cached_artwork(self.game_icons_dir, game.uid)
        if cached is None:
            self._maybe_restore_game_icon(game.uid)
        resolved_size = size if size is not None else self.game_icon_size
        src_pixbuf = self._load_static_icon_source(cached)
        img.set_from_pixbuf(self._resolve_icon_pixbuf(resolved_size, src_pixbuf, game.title))
        return img

    def _prune_uninstalled_covers(self, installed_uids: set, prunable_prefixes: set):
        removed = 0
        with self._cover_map_lock:
            entries = list(self.cover_map.items())    # download threads add to it
        for uid, entry in entries:
            if uid in installed_uids:
                continue
            if uid.split(":", 1)[0] not in prunable_prefixes:
                continue
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            animation_sidecar(self.covers_dir / f"{uid}.png").unlink(missing_ok=True)
            for f in art_files_for(self.covers_dir, uid):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        if removed:
            logger.info(
                f"Deleted cached covers for {removed} uninstalled game(s) "
                f"(source metadata was kept, will be restored automatically on reinstall)."
            )

    def _prune_uninstalled_game_icons(self, installed_uids: set, prunable_prefixes: set):
        removed = 0
        with self._cover_map_lock:
            entries = list(self.game_icon_map.items())
        for uid, entry in entries:
            if uid in installed_uids:
                continue
            if uid.split(":", 1)[0] not in prunable_prefixes:
                continue
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            for f in art_files_for(self.game_icons_dir, uid):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
        if removed:
            logger.info(
                f"Deleted cached game icons for {removed} uninstalled game(s) "
                f"(source metadata was kept, will be restored automatically on reinstall)."
            )
