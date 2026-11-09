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


from pathlib import Path
from typing import Dict, Optional, Tuple

from plunderhub.gi_setup import GdkPixbuf, hidpi_pixbuf, ui_scale
from plunderhub.logging_setup import logger

BUNDLED_ICONS_DIR = Path(__file__).resolve().parent / "assets" / "icons"

ICON_MAP: Dict[str, Tuple[str, str]] = {
    "dialog-information-symbolic": ("info.svg", "stroke"),
    "starred-symbolic": ("star.svg", "fill"),
    "github-symbolic": ("github.svg", "fill"),
    "bug-symbolic": ("bug.svg", "stroke"),
    "shield-check-symbolic": ("shield-check.svg", "stroke"),
    "mail-symbolic": ("mail.svg", "stroke"),
    "edit-copy-symbolic": ("copy.svg", "stroke"),
    "hammer-symbolic": ("hammer.svg", "stroke"),
    "zap-symbolic": ("zap.svg", "stroke"),
    "gauge-symbolic": ("gauge.svg", "stroke"),
    "wand-sparkles-symbolic": ("wand-sparkles.svg", "stroke"),
    "wine-symbolic": ("wine.svg", "stroke"),
    "external-link-symbolic": ("external-link.svg", "stroke"),
    "fan-symbolic": ("fan.svg", "stroke"),
    "cpu-symbolic": ("cpu.svg", "stroke"),
    "video-symbolic": ("video.svg", "stroke"),
    "stethoscope-symbolic": ("stethoscope.svg", "stroke"),
    "package-symbolic": ("package.svg", "stroke"),
    "bluetooth-symbolic": ("bluetooth.svg", "stroke"),
    "usb-symbolic": ("usb.svg", "stroke"),
    "non-starred-symbolic": ("star.svg", "stroke"),
    "action-unavailable-symbolic": ("eye-off.svg", "stroke"),
    "window-close-symbolic": ("x.svg", "stroke"),
    "view-refresh-symbolic": ("refresh-cw.svg", "stroke"),
    "view-filter-symbolic": ("sliders-horizontal.svg", "stroke"),
    "view-sort-descending-symbolic": ("arrow-down-wide-narrow.svg", "stroke"),
    "view-grid-symbolic": ("layout-grid.svg", "stroke"),
    "view-list-symbolic": ("list.svg", "stroke"),
    "preferences-system-symbolic": ("settings.svg", "stroke"),
    "dialog-question-symbolic": ("circle-question-mark.svg", "stroke"),
    "open-menu-symbolic": ("menu.svg", "stroke"),
    "office-chart-bar-symbolic": ("chart-column.svg", "stroke"),
    "input-gaming-symbolic": ("gamepad-2.svg", "stroke"),
    "window-minimize-symbolic": ("minus.svg", "stroke"),
    "window-maximize-symbolic": ("square.svg", "stroke"),
    "window-restore-symbolic": ("copy.svg", "stroke"),
    "applications-games-symbolic": ("gamepad.svg", "stroke"),
    "system-run": ("play.svg", "stroke"),
    "media-playback-start-symbolic": ("play.svg", "stroke"),
    "pan-down-symbolic": ("chevron-down.svg", "stroke"),
    "view-more-symbolic": ("ellipsis.svg", "stroke"),
    "media-playback-stop-symbolic": ("square.svg", "fill"),
    "go-up-symbolic": ("arrow-up.svg", "stroke"),
    "go-down-symbolic": ("arrow-down.svg", "stroke"),
    "object-select-symbolic": ("check.svg", "stroke"),
    "go-previous-symbolic": ("arrow-left.svg", "stroke"),
    "go-next-symbolic": ("arrow-right.svg", "stroke"),
    "applications-games": ("gamepad.svg", "stroke"),
    "edit-clear-all-symbolic": ("rotate-ccw.svg", "stroke"),
    "rotate-ccw-symbolic": ("rotate-ccw.svg", "stroke"),
    "drive-multidisk-symbolic": ("hard-drive.svg", "stroke"),
    "view-conceal-symbolic": ("eye-off.svg", "stroke"),
    "web-browser-symbolic": ("globe.svg", "stroke"),
    "emblem-ok-symbolic": ("circle-check-big.svg", "stroke"),
    "document-save-symbolic": ("save.svg", "stroke"),
    "folder-download-symbolic": ("folder-down.svg", "stroke"),
    "image-x-generic-symbolic": ("image.svg", "stroke"),
    "preferences-other-symbolic": ("sliders-horizontal.svg", "stroke"),
    "applications-graphics-symbolic": ("palette.svg", "stroke"),
    "document-save-as-symbolic": ("file-down.svg", "stroke"),
    "document-open-symbolic": ("file-up.svg", "stroke"),
    "folder-symbolic": ("folder.svg", "stroke"),
    "anchor-symbolic": ("anchor.svg", "stroke"),
    "user-trash-symbolic": ("trash-2.svg", "stroke"),
    "drive-harddisk-symbolic": ("database.svg", "stroke"),
    "system-software-install-symbolic": ("download.svg", "stroke"),
    "cloud-symbolic": ("cloud.svg", "stroke"),
    "cloud-download-symbolic": ("cloud-download.svg", "stroke"),
    "cloud-upload-symbolic": ("cloud-upload.svg", "stroke"),
    "edit-delete-symbolic": ("trash-2.svg", "stroke"),
    "input-keyboard-symbolic": ("keyboard.svg", "stroke"),
    "help-about-symbolic": ("info.svg", "stroke"),
    "view-reveal-symbolic": ("eye.svg", "stroke"),
    "input-dpad-symbolic": ("gamepad-directional.svg", "stroke"),
    "input-joystick-symbolic": ("joystick.svg", "stroke"),
    "input-enter-symbolic": ("corner-down-left.svg", "stroke"),
    "store-symbolic": ("store.svg", "stroke"),
    "scroll-text-symbolic": ("scroll-text.svg", "stroke"),
    "circle-dashed-check-symbolic": ("circle-dashed-check.svg", "stroke"),
    "face-slightly-frowning-symbolic": ("face-slightly-frowning.svg", "stroke"),
    "circle-alert-symbolic": ("circle-alert.svg", "stroke"),
    "triangle-alert-symbolic": ("triangle-alert.svg", "stroke"),
    "trash-symbolic": ("trash.svg", "stroke"),
    "key-round-symbolic": ("key-round.svg", "stroke"),
    "sparkles-symbolic": ("sparkles.svg", "stroke"),
    "rocket-symbolic": ("rocket.svg", "stroke"),
    "message-circle-question-symbolic": ("message-circle-question-mark.svg", "stroke"),
    "app-window-symbolic": ("app-window.svg", "stroke"),
    "layout-dashboard-symbolic": ("layout-dashboard.svg", "stroke"),
    "panel-left-close-symbolic": ("panel-left-close.svg", "stroke"),
    "panel-left-open-symbolic": ("panel-left-open.svg", "stroke"),
    "languages-symbolic": ("languages.svg", "stroke"),
    "case-sensitive-symbolic": ("case-sensitive.svg", "stroke"),
    "monitor-symbolic": ("monitor.svg", "stroke"),
    "monitor-dot-symbolic": ("monitor-dot.svg", "stroke"),
    "wind-symbolic": ("wind.svg", "stroke"),
    "arrow-up-to-line-symbolic": ("arrow-up-to-line.svg", "stroke"),
    "circle-question-symbolic": ("circle-question-mark.svg", "stroke"),
    "clock-symbolic": ("clock.svg", "stroke"),
    "type-symbolic": ("type.svg", "stroke"),
    "droplets-symbolic": ("droplets.svg", "stroke"),
    "pause-symbolic": ("pause.svg", "stroke"),
    "moon-symbolic": ("moon.svg", "stroke"),
    "picture-in-picture-symbolic": ("picture-in-picture.svg", "stroke"),
    "power-symbolic": ("power.svg", "stroke"),
    "image-off-symbolic": ("image-off.svg", "stroke"),
    "image-up-symbolic": ("image-up.svg", "stroke"),
    "image-down-symbolic": ("image-down.svg", "stroke"),
    "skull-symbolic": ("skull.svg", "stroke"),
    "scaling-symbolic": ("scaling.svg", "stroke"),
    "wallpaper-symbolic": ("wallpaper.svg", "stroke"),
}

_PIXBUF_CACHE: Dict[Tuple[str, int, str], Optional[GdkPixbuf.Pixbuf]] = {}

_SVG_TEXT_CACHE: Dict[str, Optional[str]] = {}


def _read_svg_text(filename: str) -> Optional[str]:
    cached = _SVG_TEXT_CACHE.get(filename)
    if cached is not None:
        return cached
    path = BUNDLED_ICONS_DIR / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning(f"Couldn't read bundled icon {path}: {e}")
        return None
    _SVG_TEXT_CACHE[filename] = text
    return text


def get_icon_pixbuf(name: str, px_size: int, color_hex: str) -> Optional[GdkPixbuf.Pixbuf]:
    px_size = max(1, int(px_size))
    entry = ICON_MAP.get(name)
    if entry is None:
        return None
    filename, mode = entry

    # px_size is logical: on a 2x screen the icon has twice the pixels
    # (see gi_setup.hidpi_pixbuf) and is drawn at px_size all the same
    scale = ui_scale()
    cache_key = (name, px_size, color_hex, scale)
    if cache_key in _PIXBUF_CACHE:
        return _PIXBUF_CACHE[cache_key]

    svg_text = _read_svg_text(filename)
    if svg_text is None:
        _PIXBUF_CACHE[cache_key] = None
        return None

    if mode == "fill":
        svg_text = svg_text.replace('fill="none"', f'fill="{color_hex}"')
        svg_text = svg_text.replace('stroke="currentColor"', 'stroke="none"')
    else:
        svg_text = svg_text.replace('stroke="currentColor"', f'stroke="{color_hex}"')

    pixbuf: Optional[GdkPixbuf.Pixbuf]
    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")

        def _on_size_prepared(ldr, width, height, _target=px_size * scale):
            ldr.set_size(_target, _target)

        loader.connect("size-prepared", _on_size_prepared)
        loader.write(svg_text.encode("utf-8"))
        loader.close()
        pixbuf = hidpi_pixbuf(loader.get_pixbuf(), scale)
    except Exception as e:
        logger.warning(f"Failed to render bundled icon {filename!r} at {px_size}px: {e}")
        pixbuf = None

    _PIXBUF_CACHE[cache_key] = pixbuf
    return pixbuf
