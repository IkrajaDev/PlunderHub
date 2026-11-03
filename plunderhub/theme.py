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


import ctypes
import ctypes.util
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from plunderhub.constants import APP_ICON_NAME
from plunderhub.gi_setup import Gtk
from plunderhub.logging_setup import logger


_APP_ICON_NAME_CACHE: Optional[str] = None


def _app_icon_name() -> str:
    global _APP_ICON_NAME_CACHE
    if _APP_ICON_NAME_CACHE is not None:
        return _APP_ICON_NAME_CACHE
    try:
        icon_theme = Gtk.IconTheme.get_default()
        if icon_theme is not None and icon_theme.has_icon(APP_ICON_NAME):
            _APP_ICON_NAME_CACHE = APP_ICON_NAME
            return _APP_ICON_NAME_CACHE
    except Exception as e:
        logger.debug(f"Icon theme lookup for {APP_ICON_NAME!r} failed, falling back: {e}")
    _APP_ICON_NAME_CACHE = "applications-games"
    return _APP_ICON_NAME_CACHE


# The small app icon (skull + hat, transparent background), bundled with
# the package so the window and tray still get the real icon when it isn't
# registered in the system icon theme - e.g. when run straight from the
# source tree.
APP_ICON_FILE: Path = Path(__file__).resolve().parent / "assets" / "PlunderHub_ikon.svg"


def _app_icon_file() -> Optional[Path]:
    try:
        if APP_ICON_FILE.is_file():
            return APP_ICON_FILE
    except OSError:
        pass
    return None


def _app_icon_spec() -> str:
    """Icon name or absolute file path for the tray (AppIndicator takes either)."""
    name = _app_icon_name()
    if name == APP_ICON_NAME:
        return name
    icon_file = _app_icon_file()
    return str(icon_file) if icon_file is not None else name


_ORIGINAL_PREFER_DARK: Optional[bool] = None
_ORIGINAL_GTK_THEME_NAME: Optional[str] = None


def _capture_original_theme_preference() -> Tuple[bool, Optional[str]]:
    global _ORIGINAL_PREFER_DARK, _ORIGINAL_GTK_THEME_NAME
    if _ORIGINAL_PREFER_DARK is None:
        try:
            gtk_settings = Gtk.Settings.get_default()
            if gtk_settings is not None:
                _ORIGINAL_PREFER_DARK = bool(gtk_settings.get_property("gtk-application-prefer-dark-theme"))
                _ORIGINAL_GTK_THEME_NAME = gtk_settings.get_property("gtk-theme-name")
            else:
                _ORIGINAL_PREFER_DARK = False
        except Exception:
            _ORIGINAL_PREFER_DARK = False
    return _ORIGINAL_PREFER_DARK, _ORIGINAL_GTK_THEME_NAME


_SYSTEM_SWATCH: Optional[Dict[str, Tuple[float, float, float]]] = None


def system_swatch_colors() -> Optional[Dict[str, Tuple[float, float, float]]]:
    """The desktop's own GTK theme (the one the System default theme
    brings back) as the swatch's colours, read from that theme's CSS -
    the app's own theme may be on screen meanwhile. None when the theme
    names no colours."""
    global _SYSTEM_SWATCH
    if _SYSTEM_SWATCH is not None:
        return _SYSTEM_SWATCH
    dark, name = _capture_original_theme_preference()
    try:
        provider = Gtk.CssProvider.get_named(name or "Adwaita", "dark" if dark else None)
        ctx = Gtk.StyleContext()
        path = Gtk.WidgetPath()
        path.append_type(Gtk.Window)
        ctx.set_path(path)
        ctx.add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 100)

        def color(*names):
            for n in names:
                found, c = ctx.lookup_color(n)
                if found:
                    return (c.red, c.green, c.blue)
            return None
        bg = color("theme_bg_color", "window_bg_color")
        fg = color("theme_fg_color", "window_fg_color")
        if bg is None or fg is None:
            return None
        side = color("sidebar_bg_color", "theme_unfocused_bg_color", "headerbar_bg_color")
        if side is None or side == bg:
            side = tuple(b + (f - b) * 0.08 for b, f in zip(bg, fg))
        accent = tuple(v / 255 for v in _hex_to_rgb(DEFAULT_ACCENT))     # the System theme's accent
        _SYSTEM_SWATCH = {"bg": bg, "bg_raised": side, "fg": fg,
                          "fg_dim": tuple(b + (f - b) * 0.6 for b, f in zip(bg, fg)), "accent": accent}
    except Exception as e:
        logger.debug(f"Couldn't read the system GTK theme's colours: {e}")
        return None
    return _SYSTEM_SWATCH


THEME_PALETTES: Dict[str, Dict] = {
    "plunderhub": {
        "bg": "#071a1b", "bg_alt": "#0b2426", "bg_raised": "#102f31",
        "accent": "#22d3ac", "accent_rgb": "34, 211, 172",
        "accent_hover": "#1cb897", "accent_active": "#159c80",
        "fg": "#eaf6f3", "fg_dim": "#7fa39c", "on_accent": "#04140f",
        "dark": True,
    },
    "catppuccin_mocha": {
        "bg": "#1e1e2e", "bg_alt": "#181825", "bg_raised": "#313244",
        "accent": "#cba6f7", "accent_rgb": "203, 166, 247",
        "accent_hover": "#b795de", "accent_active": "#a285c6",
        "fg": "#cdd6f4", "fg_dim": "#a6adc8", "on_accent": "#11111b",
        "dark": True,
    },
    "rose_pine": {
        "bg": "#191724", "bg_alt": "#1f1d2e", "bg_raised": "#26233a",
        "accent": "#ebbcba", "accent_rgb": "235, 188, 186",
        "accent_hover": "#d4a9a7", "accent_active": "#bc9695",
        "fg": "#e0def4", "fg_dim": "#908caa", "on_accent": "#191724",
        "dark": True,
    },
    "kanagawa": {
        "bg": "#1f1f28", "bg_alt": "#16161d", "bg_raised": "#2a2a37",
        "accent": "#e6c384", "accent_rgb": "230, 195, 132",
        "accent_hover": "#cfb077", "accent_active": "#b89c6a",
        "fg": "#dcd7ba", "fg_dim": "#938aa9", "on_accent": "#1f1f28",
        "dark": True,
    },
    "catppuccin_latte": {
        "bg": "#eff1f5", "bg_alt": "#e6e9ef", "bg_raised": "#dce0e8",
        "accent": "#8839ef", "accent_rgb": "136, 57, 239",
        "accent_hover": "#7a33d7", "accent_active": "#6d2ebf",
        "fg": "#4c4f69", "fg_dim": "#6c6f85", "on_accent": "#eff1f5",
        "dark": False,
    },
    "rose_pine_dawn": {
        "bg": "#faf4ed", "bg_alt": "#f2e9e1", "bg_raised": "#e9e0d7",
        "accent": "#b4637a", "accent_rgb": "180, 99, 122",
        "accent_hover": "#a2596e", "accent_active": "#904f62",
        "fg": "#575279", "fg_dim": "#797593", "on_accent": "#faf4ed",
        "dark": False,
    },
    "github_light": {
        "bg": "#ffffff", "bg_alt": "#f6f8fa", "bg_raised": "#eaeef2",
        "accent": "#1a7f37", "accent_rgb": "26, 127, 55",
        "accent_hover": "#177232", "accent_active": "#15662c",
        "fg": "#1f2328", "fg_dim": "#59636e", "on_accent": "#ffffff",
        "dark": False,
    },
    "dracula": {
        "bg": "#282a36", "bg_alt": "#21222c", "bg_raised": "#343746",
        "accent": "#bd93f9", "accent_rgb": "189, 147, 249",
        "accent_hover": "#a67ef0", "accent_active": "#9566d9",
        "fg": "#f8f8f2", "fg_dim": "#8491b6", "on_accent": "#191a21",
        "dark": True,
    },
    "gruvbox": {
        "bg": "#fbf1c7", "bg_alt": "#f2e5bc", "bg_raised": "#ebdbb2",
        "accent": "#d65d0e", "accent_rgb": "214, 93, 14",
        "accent_hover": "#af3a03", "accent_active": "#8a2f02",
        "fg": "#3c3836", "fg_dim": "#70655c", "on_accent": "#fbf1c7",
        "dark": False,
    },
    "gruvbox_dark": {
        "bg": "#282828", "bg_alt": "#32302f", "bg_raised": "#3c3836",
        "accent": "#fe8019", "accent_rgb": "254, 128, 25",
        "accent_hover": "#d65d0e", "accent_active": "#af3a03",
        "fg": "#ebdbb2", "fg_dim": "#a89984", "on_accent": "#282828",
        "dark": True,
    },
    "tokyo_night": {
        "bg": "#1a1b26", "bg_alt": "#16161e", "bg_raised": "#24283b",
        "accent": "#7aa2f7", "accent_rgb": "122, 162, 247",
        "accent_hover": "#6690e8", "accent_active": "#5478d1",
        "fg": "#c0caf5", "fg_dim": "#7881ac", "on_accent": "#1a1b26",
        "dark": True,
    },
}

THEME_ORDER: List[str] = [
    "system", "plunderhub",
    # dark
    "catppuccin_mocha", "rose_pine", "tokyo_night", "dracula", "kanagawa", "gruvbox_dark",
    # light
    "catppuccin_latte", "rose_pine_dawn", "gruvbox", "github_light",
]

# Themes that have been retired, and the closest one still here - so a
# saved choice keeps a similar look instead of falling back to the
# default.
THEME_RENAMES: Dict[str, str] = {
    "midnight_mirage": "catppuccin_mocha",
    "nord_dark": "tokyo_night",
    "tokyo_night_storm": "tokyo_night",
    "sweet": "rose_pine",
    "nord_light": "catppuccin_latte",
    "tokyo_night_light": "catppuccin_latte",
}
# a "plunderhub" a sajátom, a többi ismert paletta portja

DEFAULT_ACCENT = "#3584e4"
DEFAULT_ACCENT_RGB = "53, 132, 228"
DEFAULT_ACCENT_CHECK = "#bd93f9"
DEFAULT_ACCENT_SOFT = "rgba(53, 132, 228, 0.18)"
DEFAULT_ACCENT_SOFT_HOVER = "rgba(53, 132, 228, 0.26)"


def _accent_css(accent: str, accent_check: str, soft: str, soft_hover: str, accent_rgb: str,
                card_shadow: str = "none", card_shadow_active: str = "none",
                on_accent: str = "#ffffff") -> str:
    return f"""
    /* Redefines the NAMED colors Adwaita (and GTK3 themes generally)
       use internally for "the selected/accent color" - this is the
       idiomatic way to theme a GTK3 app's accent color, and is what
       actually gets elements like the radio button's checked dot to
       follow the active theme: that dot renders via Adwaita's own
       internal reference to one of these variables (exactly which one
       depends on the GTK/theme version), not simply via a
       background-color/color property we could override directly on
       the widget with a normal rule - the explicit overrides further
       below still stay in place too, as a second layer, but this is
       the fix that actually addresses the root cause. */
    @define-color theme_selected_bg_color {accent};
    @define-color selected_bg_color {accent};
    /* The startup scrim covers the window fully while it loads, so the
       library builds itself up from nothing once it is ready (the palette
       themes paint it in their own bg, see _build_theme_css). */
    .startup-scrim-solid {{ background-color: @theme_bg_color; }}
    @define-color accent_color {accent};
    @define-color accent_bg_color {accent};
    .game-cover-selected {{
        /* .game-cover:hover / :active / :checked / :focus (further
           below) each set the "border" SHORTHAND to "none", which
           resets border-color right along with it - clicking a card
           to select it leaves it BOTH freshly ".game-cover-selected"
           AND, almost always, either still hovered (the mouse hasn't
           moved away yet) or focused (GTK gives a clicked button
           keyboard focus by default) at the very same moment, so one
           of those "no border at all" rules was winning the very
           instant selection was supposed to become visible - looking
           like the selection outline "didn't always activate", even
           though the underlying selected_uids state itself was
           completely correct the whole time (the checkmark, a
           separate widget entirely unaffected by this, reflected that
           correctly the whole time).
           GTK3's CSS engine doesn't support `!important` at all (confirmed:
           it's a straight parse error, not just ignored) - the actual fix
           is on those OTHER rules instead, further below: an explicit
           ":not(.game-cover-selected)" on each of them means they simply
           don't match a selected card's button in the first place, rather
           than matching and then hoping to lose a specificity contest
           this rule can't actually force a win of. */
        border-color: {accent};
        background-color: {soft};
        background-image: none;
    }}
    .game-cover-selected:hover {{
        border-color: {accent};
        background-color: {soft_hover};
        background-image: none;
    }}
    /* Hover carried by elevation plus a light tint. The old heavy
       accent wash sat behind the cover and bled a colored halo around
       it, changing the grid's hue as the mouse moved. GTK3 CSS has no
       transform, so the shadow does this alone. */
    /* Hover is ELEVATION only. Even at 0.18 the accent wash sat behind
       the artwork and bled a coloured halo out around every edge of it,
       so the grid changed hue as the pointer crossed it - and on a cover
       that already uses the accent's own colour range it read as a
       rendering fault rather than as feedback. A shadow says "this one"
       without touching the art. */
    .game-cover:hover:not(.game-cover-selected) {{
        border: none;
        outline: none;
        box-shadow: {card_shadow};
        background-color: transparent;
        background-image: none;
    }}
    .game-cover:not(.game-cover-selected):active,
    .game-cover:not(.game-cover-selected):checked,
    .game-cover:not(.game-cover-selected):focus {{
        border: none;
        outline: none;
        box-shadow: {card_shadow_active};
        background-color: rgba({accent_rgb}, 0.22);
        background-image: none;
    }}
    /* Overrides the :focus tint above while the mouse is hovering a
       DIFFERENT card (see _suppress_focus_ring_except /
       _restore_focus_ring) - needs to be more specific than the plain
       ".game-cover:focus" rule above (an extra class does that), since
       real GTK keyboard focus doesn't actually move just because the
       mouse is somewhere else; this only hides its visual ring for as
       long as that's true. */
    .game-cover.game-cover-focus-suppressed:focus,
    .game-cover.game-cover-focus-suppressed:active,
    .game-cover.game-cover-focus-suppressed:checked {{
        background-color: transparent;
        background-image: none;
    }}
    .view-toggle-btn:checked {{
        border: 2px solid {accent};
    }}
    /* Adwaita paints checked boxes, radios and switches with a blue
       gradient in background-image, which sits on top of any
       background-color - so the image has to go too, or the theme
       colour never shows. color is what the check mark/dot is drawn in. */
    checkbutton check:checked, checkbutton check:indeterminate,
    radiobutton radio:checked, radiobutton radio:indeterminate,
    checkbutton check:checked:hover, checkbutton check:indeterminate:hover,
    radiobutton radio:checked:hover, radiobutton radio:indeterminate:hover,
    checkbutton check:checked:active, radiobutton radio:checked:active,
    checkbutton check:checked:backdrop, checkbutton check:indeterminate:backdrop,
    radiobutton radio:checked:backdrop, radiobutton radio:indeterminate:backdrop {{
        background-color: {accent_check};
        background-image: none;
        border-color: {accent_check};
        color: {on_accent};
    }}
    switch:checked, switch:checked:backdrop {{
        background-color: {accent};
        background-image: none;
        border-color: {accent};
    }}
    /* Switched off by its section (gamescope off, say): Adwaita leaves a
       disabled switch half see-through, and whatever is behind tinted
       it - on a red cover, red. A flat, dim one instead. */
    switch:disabled, switch:disabled:backdrop {{
        background-color: rgba(127, 127, 127, 0.18);
        background-image: none;
        border-color: transparent;
    }}
    switch:disabled slider {{
        background-image: none;
        background-color: rgba(255, 255, 255, 0.35);
        border-color: transparent;
        box-shadow: none;
    }}
    selection, entry selection, textview text selection, label selection,
    spinbutton selection {{
        background-color: {accent};
        color: {on_accent};
    }}
    entry:focus, spinbutton:focus {{
        border-color: {accent};
        box-shadow: inset 0 0 0 1px {accent};
    }}
    """


def _hex_to_rgb(value: str) -> Tuple[int, int, int]:
    h = value.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _hex_to_rgb_str(value: str) -> str:
    r, g, b = _hex_to_rgb(value)
    return f"{r}, {g}, {b}"


def _contrast(a: str, b: str) -> float:
    def _lum(value: str) -> float:
        out = []
        for c in _hex_to_rgb(value):
            c /= 255
            out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _text_on(background: str, preferred: str) -> str:
    if _contrast(preferred, background) >= 4.5:
        return preferred
    black_r = _contrast("#000000", background)
    white_r = _contrast("#ffffff", background)
    return "#000000" if black_r >= white_r else "#ffffff"


def _blend(base: str, other: str, amount: float) -> str:
    a = _hex_to_rgb(base)
    b = _hex_to_rgb(other)
    mixed = tuple(max(0, min(255, round(a[i] + (b[i] - a[i]) * amount))) for i in range(3))
    return "#%02x%02x%02x" % mixed


def accent_text_color(p: Dict) -> str:
    """What text (and icons) on an accent-filled surface are drawn in:
    the palette's own choice, unless that is too faint on the accent."""
    return _text_on(p["accent"], p["on_accent"])


DANGER = "#e5534b"
DANGER_FG = "#1a0503"


def _settings_css(accent: str, tint: Callable[[float], str], raised: str, sidebar: str,
                  fg: str, fg_dim: str) -> str:
    """The Settings window (sidebar, cards, rows), the launcher install
    window's choice cards, and red destructive buttons. tint(a) is the
    accent at alpha a - a plain rgba() for PlunderHub's own themes, GTK's
    alpha() of the desktop's accent for the system theme."""
    return f"""
.settings-sidebar {{
    background-color: {sidebar};
    border-right: 1px solid {tint(0.18)};
}}
list.settings-nav {{ background-color: transparent; }}
.settings-nav row {{
    padding: 9px 12px;
    margin: 2px 8px;
    border-radius: 10px;
    background: transparent;
    outline: none;
    /* Adwaita fades "all": the text and icon colour too, which re-laid
       the whole window out on every frame of a page change */
    transition: background-color 150ms ease-out;
}}
.settings-nav row:hover {{ background-color: {tint(0.10)}; }}
.settings-nav row:selected, .settings-nav row:selected:hover, .settings-nav row:selected:focus {{
    background-color: {tint(0.20)}; background-image: none; color: {fg};
}}
.settings-nav row:selected label {{ color: {fg}; }}
.settings-nav row:focus {{ box-shadow: inset 0 0 0 2px {accent}; }}
.settings-app-title {{ font-weight: 800; font-size: 115%; }}
.settings-app-sub {{ color: {fg_dim}; font-size: 85%; }}
.settings-page-title {{ font-size: 165%; font-weight: 800; }}
.settings-page-sub {{ color: {fg_dim}; }}
.settings-group-title {{ color: {accent}; font-weight: 700; font-size: 85%; letter-spacing: 1px; }}
.settings-card {{
    background-color: {raised};
    border-radius: 14px;
    border: 1px solid {tint(0.10)};
}}
.settings-row {{ padding: 12px 16px; }}
.settings-row-sep {{ background-color: {tint(0.10)}; min-height: 1px; }}
.settings-row-title {{ font-weight: 600; }}
.settings-row-sub {{ color: {fg_dim}; font-size: 88%; }}
.settings-status-on {{ color: {accent}; font-size: 85%; font-weight: 600; }}
.settings-status-off {{ color: {fg_dim}; font-size: 85%; }}
.settings-flash {{ background-color: {tint(0.28)}; border-radius: 14px; }}
/* The whole row lights up under the keyboard/controller, whatever
   kind of control it holds - so it's always clear where you are. */
.settings-row-focused {{
    background-color: {tint(0.13)};
    box-shadow: inset 4px 0 0 {accent};
    border-radius: 10px;
}}
.settings-danger {{ border: 1px solid alpha({DANGER}, 0.55); background-color: alpha({DANGER}, 0.07); }}
.settings-danger-title {{ color: {DANGER}; }}
.settings-card switch, .gp-card switch, .ws-ask-row switch {{ background-image: none; background-color: alpha({fg_dim}, 0.45); border-color: transparent; }}
.settings-card switch:checked, .gp-card switch:checked, .ws-ask-row switch:checked {{ background-color: {accent}; }}
.settings-card switch slider, .gp-card switch slider, .ws-ask-row switch slider {{
    background-image: none;
    background-color: #ffffff;
    border-color: rgba(0, 0, 0, 0.15);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.35);
}}
.settings-card switch:focus, .gp-card switch:focus, .ws-ask-row switch:focus {{ box-shadow: 0 0 0 3px {tint(0.55)}; }}
.settings-card scale.settings-editing slider {{ box-shadow: 0 0 0 5px {tint(0.45)}; }}
.settings-card scale.settings-editing {{ outline: 2px solid {accent}; outline-offset: 4px; }}
.settings-card entry.settings-editing,
.settings-card spinbutton.settings-editing {{
    outline: 2px solid {accent};
    outline-offset: 2px;
    box-shadow: 0 0 0 4px {tint(0.30)};
}}
flowbox.settings-themes flowboxchild {{
    padding: 8px;
    border-radius: 12px;
    border: 2px solid transparent;
    outline: none;
}}
flowbox.settings-themes flowboxchild:hover {{ background-color: {tint(0.10)}; }}
flowbox.settings-themes flowboxchild:selected {{ border-color: {accent}; background-color: {tint(0.14)}; }}
flowbox.settings-themes flowboxchild.settings-theme-focused {{
    box-shadow: inset 0 0 0 3px {accent};
    background-color: {tint(0.26)};
}}
flowbox.settings-themes flowboxchild.settings-theme-focused:selected {{ box-shadow: inset 0 0 0 4px {accent}; }}
.settings-swatch-name {{ font-size: 85%; }}
scrolledwindow.settings-scroll scrollbar {{
    background-color: transparent;
    background-image: none;
    border: none;
}}
scrolledwindow.settings-scroll scrollbar.vertical trough {{
    background-color: {tint(0.12)};
    border-radius: 999px;
    min-width: 10px;
    margin: 10px 6px;
}}
scrolledwindow.settings-scroll scrollbar.vertical slider {{
    background-image: linear-gradient(to bottom, {accent}, {tint(0.75)});
    border: none;
    border-radius: 999px;
    min-width: 10px;
    min-height: 48px;
    margin: 0;
    box-shadow: 0 0 8px {tint(0.55)};
}}
scrolledwindow.settings-scroll scrollbar.vertical slider:hover {{
    background-image: linear-gradient(to bottom, {accent}, {accent});
}}
scrolledwindow.settings-scroll scrollbar.vertical slider:active {{
    box-shadow: 0 0 12px {tint(0.8)};
}}
.settings-swatch-current {{ color: {accent}; font-weight: 700; }}
.settings-footer {{ color: {fg_dim}; font-size: 85%; }}
.settings-hint {{ color: {accent}; font-size: 85%; font-weight: 600; }}
.settings-key {{
    background-color: {sidebar};
    border: 1px solid {tint(0.35)};
    border-bottom-width: 3px;
    border-radius: 7px;
    padding: 1px 9px;
    min-width: 14px;
    font-weight: 700;
    font-size: 88%;
}}
.settings-tag {{
    color: {fg_dim};
    border: 1px solid alpha({fg_dim}, 0.6);
    border-radius: 999px;
    padding: 0 8px;
    font-size: 75%;
}}
.settings-shortcut-head {{ color: {fg_dim}; font-size: 80%; font-weight: 700; }}
.settings-shortcut {{ padding-top: 8px; padding-bottom: 8px; }}
.settings-search-hit {{ padding: 8px 12px; }}
.settings-search-page {{ color: {fg_dim}; font-size: 82%; }}
.settings-pill {{
    background-color: {tint(0.18)};
    color: {accent};
    border-radius: 999px;
    padding: 1px 9px;
    font-size: 75%;
    font-weight: 700;
}}
button.install-choice {{
    background-image: none;
    background-color: {raised};
    border: 2px solid transparent;
    border-radius: 14px;
    padding: 14px 16px;
    box-shadow: none;
}}
button.install-choice:hover {{ background-color: {tint(0.10)}; }}
button.install-choice:checked {{ border-color: {accent}; background-color: {tint(0.14)}; }}
button.install-choice:disabled {{ opacity: 0.45; }}
.settings-card button:disabled {{ opacity: 0.4; }}
/* Red buttons for things that can't be undone. Named states, because
   the generic button hover/active tint would otherwise win and wash the
   red out. */
/* Quiet until pointed at: a red tint and edge, filled red under the
   pointer - so the click itself is never a surprise. */
button.destructive-action {{
    background-image: none;
    background-color: alpha({DANGER}, 0.14);
    border-color: alpha({DANGER}, 0.50);
    color: {DANGER};
}}
button.destructive-action label {{ color: {DANGER}; font-weight: 600; }}
button.destructive-action:hover {{ background-image: none; background-color: {DANGER}; border-color: {DANGER}; }}
button.destructive-action:hover label {{ color: {DANGER_FG}; }}
button.destructive-action:active {{ background-image: none; background-color: #c9433c; border-color: #c9433c; }}
button.destructive-action:active label {{ color: {DANGER_FG}; }}
button.destructive-action:focus {{ box-shadow: 0 0 0 3px alpha({DANGER}, 0.35); }}
"""


def _home_css(accent: str, tint: Callable[[float], str], raised: str, sidebar: str,
              fg: str, fg_dim: str, on_accent: str = "#04140f") -> str:
    """The main window: sidebar, top bar, the "Continue playing" hero,
    section headings, game cards and list rows (see HomeMixin).
    on_accent is text on the accent - dark on most themes, light on the
    light ones."""
    return f"""
/* the list view's rows - Edit order paints its own the same */
@define-color plunderhub_raised {raised};
.home-titlebar-stub {{
    min-height: 0;
    padding: 0;
    margin: 0;
    border: none;
    box-shadow: none;
    background: none;
}}
.home-sidebar {{
    background-color: {sidebar};
    border-right: 1px solid {tint(0.18)};
}}
button.home-brand-btn, button.home-nav-action {{
    background-color: transparent;
    border-color: transparent;
}}
button.home-brand-btn:hover,
button.home-nav-action:hover {{
    background-color: {tint(0.12)};
}}
button.home-brand-btn {{ padding: 6px 8px; border-radius: 12px; }}
.home-brand {{ font-weight: 800; font-size: 120%; }}
.home-brand-accent {{ color: {accent}; }}
.home-update-card {{
    background-image: linear-gradient(160deg, {tint(0.20)}, {tint(0.06)});
    border: 1px solid {tint(0.45)};
    border-radius: 16px;
    padding: 12px;
    margin: 0 10px 10px 10px;
}}
.home-update-title {{ font-weight: 800; }}
button.lang-tile {{ padding: 12px 10px; }}
button.lang-tile label {{ font-weight: 700; }}
.welcome-title {{ font-size: 150%; }}
.welcome-small {{ font-size: 88%; }}
/* Solid, not see-through: the bubbles pass behind the whole card, as
   they do behind its button - through a see-through one they showed
   over its text and logos. The same tint, on the opaque sidebar colour. */
.empty-card {{
    background-color: {sidebar};
    background-image: linear-gradient({tint(0.07)}, {tint(0.07)});
    border: 1px solid {tint(0.22)};
    border-radius: 20px;
    padding: 34px 40px;
}}
.empty-title {{ font-size: 150%; font-weight: 800; }}
.welcome-beta {{ font-size: 86%; padding: 3px 12px; background-color: {tint(0.16)}; }}
/* the welcome card's "Report a bug": a link in the accent, in the beta note.
   Hover tints the background only - an underline on the label lays the
   whole card out again (perf_guard). */
.welcome-note button.welcome-bug {{
    background-color: transparent;
    background-image: none;
    border-color: transparent;
    box-shadow: none;
    border-radius: 999px;
    padding: 2px 8px;
    color: {accent};
    font-weight: 700;
}}
.welcome-note button.welcome-bug:hover {{ background-color: {tint(0.14)}; }}
.welcome-feature {{
    background-color: {tint(0.08)};
    border: 1px solid {tint(0.16)};
    border-radius: 14px;
    padding: 10px 14px;
}}
/* GitHub / Ko-fi on the welcome card: the About card's pills, a size down */
button.about-link.welcome-link {{ padding: 6px 14px; font-size: 92%; }}
/* the licence link: as quiet as the version beside it */
button.about-quiet.welcome-quiet {{ font-size: 88%; font-weight: 400; padding: 2px 8px; }}
button.about-quiet.welcome-quiet label {{ opacity: 0.7; }}
.welcome-note {{
    background-color: {tint(0.12)};
    border: 1px solid {tint(0.35)};
    border-radius: 14px;
    padding: 12px 16px;
}}
/* Report a bug (DialogsMixin._show_bug_report) */
.bug-step-big {{ color: {accent}; font-weight: 800; font-size: 170%; min-width: 18px; }}
.bug-mail-address {{ color: {accent}; font-weight: 700; }}
.bug-mail {{ padding: 8px 10px 8px 6px; }}
.bug-section {{ font-weight: 800; }}
.bug-info {{
    background-color: {tint(0.08)};
    border: 1px solid {tint(0.16)};
    border-radius: 12px;
    padding: 6px 8px 6px 4px;
}}
.bug-info-text {{ font-size: 88%; margin-left: 12px; margin-right: 8px; }}
/* the fold's heading: hover tints the background only (perf_guard) */
button.bug-toggle {{
    background-color: transparent;
    background-image: none;
    border-color: transparent;
    box-shadow: none;
    border-radius: 999px;
    padding: 4px 10px;
}}
button.bug-toggle:hover {{ background-color: {tint(0.12)}; }}
button.home-play.bug-small-pill {{ padding: 4px 14px; font-size: 90%; }}
.bug-privacy {{
    background-color: {tint(0.12)};
    border: 1px solid {tint(0.35)};
    border-radius: 12px;
    padding: 10px 14px;
}}
.bug-privacy-title {{ font-weight: 800; }}
scrolledwindow.bug-scroll scrollbar.vertical trough {{ margin: 2px 0 2px 6px; }}
.update-log-box {{ border-radius: 12px; border: 1px solid {tint(0.18)}; }}
.update-log-box textview, .update-log-box textview text {{ background-color: rgba(0, 0, 0, 0.25); font-size: 85%; }}
button.update-details-btn {{
    background-image: none;
    background-color: transparent;
    border: none;
    box-shadow: none;
    color: {fg_dim};
    padding: 4px 8px;
    border-radius: 999px;
}}
button.update-details-btn:hover {{ background-color: {tint(0.12)}; }}
.home-update-versions {{ color: {fg_dim}; font-size: 85%; font-feature-settings: "tnum"; }}
button.home-update-install, button.home-update-later {{
    background-image: none;
    border-radius: 999px;
    padding: 5px 10px;
    font-weight: 700;
    box-shadow: none;
}}
button.home-update-install {{ background-color: {accent}; color: {on_accent}; border: none; }}
button.home-update-install label {{ color: {on_accent}; }}
button.home-update-install:hover {{ background-color: shade({accent}, 1.08); }}
button.home-update-later {{ background-color: transparent; border: 1px solid {tint(0.35)}; }}
button.home-update-later:hover {{ background-color: {tint(0.14)}; }}
.beta-pill {{
    color: {accent};
    border: 1px solid {tint(0.55)};
    border-radius: 999px;
    padding: 0 7px;
    font-size: 66%;
    font-weight: 800;
    letter-spacing: 1px;
}}
list.home-nav {{ background-color: transparent; }}
.home-nav row {{ padding: 7px 12px; margin: 1px 8px; border-radius: 10px; }}
.home-nav row:hover {{ background-color: {tint(0.10)}; }}
/* every state of it - the generic "list row:selected:focus/:hover" rule
   (accent fill) is more specific than plain :selected, and painted a
   clicked row solid accent, its Open pill vanishing into it */
.home-nav row:selected, .home-nav row:selected:hover, .home-nav row:selected:focus {{
    background-color: {tint(0.22)}; background-image: none; color: {fg};
}}
.home-nav row:selected label {{ color: {fg}; }}
.home-nav row:selected label.home-count {{ color: {accent}; }}
.home-nav row:focus {{ outline: 2px solid {accent}; outline-offset: -2px; }}
/* A launcher's row: its logo is drawn (LauncherLogo); "Running" under
   the name, small, in the accent. */
.home-nav row.home-launcher-row {{ padding: 5px 12px 5px 8px; }}
.home-launcher-status, .home-nav row:selected label.home-launcher-status {{
    color: {accent}; font-size: 80%; font-weight: 600;
}}
.home-count {{ color: {fg_dim}; font-size: 80%; font-weight: 700; }}
.home-nav-head {{
    color: {fg_dim};
    font-size: 75%;
    font-weight: 700;
    letter-spacing: 1px;
    margin: 12px 20px 2px 20px;
}}
button.home-nav-action {{ padding: 0 4px; min-height: 0; min-width: 0; border-radius: 999px; }}
/* A launcher's Open / Close, under its row once it's chosen: a quiet
   pill that says what it does, a little stronger when pointed at or
   stood on; the menu a round button beside it. */
button.home-launcher-open, button.home-launcher-close, button.home-launcher-more {{
    padding: 3px 13px 3px 10px;
    min-height: 0;
    min-width: 0;
    border-radius: 999px;
    background-image: none;
    box-shadow: none;
    outline: none;
}}
button.home-launcher-open {{ background-color: alpha({accent}, 0.14); border: 1px solid alpha({accent}, 0.45); }}
button.home-launcher-open label {{ color: {fg}; font-weight: 600; }}
button.home-launcher-open:hover, button.home-launcher-open:focus {{
    background-color: alpha({accent}, 0.26);
    border-color: {accent};
}}
button.home-launcher-close {{
    background-color: alpha(#e5534b, 0.10);
    border: 1px solid alpha(#e5534b, 0.45);
    color: #f08a84;
}}
button.home-launcher-close label,
.home-nav row:selected button.home-launcher-close label {{ color: #f08a84; font-weight: 600; }}
button.home-launcher-close:hover, button.home-launcher-close:focus {{
    background-color: alpha(#e5534b, 0.24);
    border-color: #e5534b;
}}
button.home-launcher-more {{ padding: 3px 8px; background-color: {tint(0.08)}; border: 1px solid transparent; color: {fg}; }}
button.home-launcher-more:hover, button.home-launcher-more:focus {{ background-color: {tint(0.18)}; border-color: {tint(0.3)}; }}
entry.home-search {{
    border-radius: 999px;
    padding: 4px 14px;
    min-height: 30px;
    background-color: {raised};
}}
.home-toolbar {{
    background-color: {raised};
    border: 1px solid {tint(0.16)};
    border-radius: 999px;
    padding: 3px;
}}
.home-toolbar button, .home-winctl button {{
    background-image: none;
    background-color: transparent;
    border-color: transparent;
    box-shadow: none;
    border-radius: 999px;
    min-height: 30px;
    min-width: 30px;
    padding: 0 6px;
}}
.home-toolbar button.home-sort-btn {{ padding: 0 12px; }}
.home-toolbar button:hover, .home-winctl button:hover {{ background-color: {tint(0.14)}; }}
.home-toolbar button:checked, .home-toolbar button.view-toggle-btn:checked {{
    background-color: {tint(0.26)};
    border-color: transparent;
}}
.home-toolbar button:active {{ background-color: {tint(0.22)}; }}
separator.home-toolbar-sep {{ margin: 0 6px; min-width: 1px; min-height: 18px; background-color: {tint(0.30)}; }}
.home-winctl button.window-close-btn:hover {{ background-color: #e5534b; }}
popover.home-menu {{
    background-color: {sidebar};
    border: 1px solid {tint(0.22)};
    border-radius: 14px;
    padding: 6px;
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
}}
.home-menu-heading {{
    color: {fg_dim};
    font-size: 75%;
    font-weight: 700;
    letter-spacing: 1px;
    margin: 6px 12px 4px 12px;
}}
button.home-menu-row {{
    background-image: none;
    background-color: transparent;
    border-color: transparent;
    box-shadow: none;
    border-radius: 10px;
    padding: 8px 12px;
    min-width: 250px;
}}
button.home-menu-row:hover, button.home-menu-row:focus {{ background-color: {tint(0.14)}; outline: none; }}
button.home-menu-row:active {{ background-color: {tint(0.24)}; }}
separator.home-menu-sep {{ margin: 4px 10px; min-height: 1px; background-color: {tint(0.16)}; }}
.home-floating {{ background-color: transparent; }}
.home-selection-bar {{
    background-color: {sidebar};
    border: 1px solid {tint(0.35)};
    border-radius: 999px;
    padding: 6px 6px 6px 20px;
    margin: 4px 16px 20px 16px;
    box-shadow: 0 8px 18px rgba(0, 0, 0, 0.5);
}}
.home-selection-count {{ font-weight: 700; }}
.home-selection-bar button:not(.home-play) {{
    background-image: none;
    background-color: transparent;
    border-color: transparent;
    box-shadow: none;
    border-radius: 999px;
    min-height: 34px;
    min-width: 34px;
    padding: 0 6px;
}}
.home-selection-bar button:not(.home-play):hover {{ background-color: {tint(0.16)}; }}
.home-launch-dialog {{
    background-color: {sidebar};
    border: 1px solid {tint(0.30)};
    border-radius: 20px;
}}
.home-launch-title {{ font-size: 160%; font-weight: 800; }}
/* Set sail over the game's hero (see _confirm_launch): light text with a
   shadow on the shaded picture, whatever the theme. */
.home-launch-hero label {{ color: #ffffff; text-shadow: 0 1px 4px rgba(0, 0, 0, 0.9); }}
.home-launch-hero label.home-hero-kicker {{ color: {accent}; text-shadow: 0 1px 4px rgba(0, 0, 0, 0.95); }}
.home-launch-hero label.home-launch-meta {{ color: rgba(255, 255, 255, 0.85); }}
.home-launch-hero button.home-pill-ghost {{
    background-color: rgba(0, 0, 0, 0.38);
    border-color: rgba(255, 255, 255, 0.38);
}}
.home-launch-hero button.home-pill-ghost:hover, .home-launch-hero button.home-pill-ghost:focus {{
    background-color: rgba(0, 0, 0, 0.55);
    border-color: rgba(255, 255, 255, 0.7);
}}
.home-launch-hero button.home-pill-ghost label {{ color: #ffffff; }}
.home-launch-hero button.suggested-action label {{ color: {on_accent}; text-shadow: none; }}
.home-launch-icon {{ -gtk-icon-shadow: 0 4px 14px rgba(0, 0, 0, 0.6); }}
.about-wordmark {{ font-size: 260%; letter-spacing: -0.5px; }}
button.about-link {{ border-radius: 999px; padding: 9px 20px; font-weight: 600; }}
button.about-license-btn {{ border-radius: 999px; color: {fg_dim}; font-size: 85%; }}
button.about-quiet {{
    background-image: none;
    background-color: transparent;
    border: none;
    box-shadow: none;
    border-radius: 999px;
    padding: 4px 12px;
    color: {fg_dim};
    font-size: 88%;
}}
button.about-quiet:hover {{ background-color: {tint(0.12)}; }}
.about-tagline {{ font-size: 95%; }}
.about-beta {{ font-size: 72%; padding: 1px 8px; }}
button.about-close {{
    background-image: none;
    background-color: {tint(0.10)};
    border: none;
    border-radius: 999px;
    padding: 0;
    /* a round 34 px button, whatever the icon inside */
    min-width: 34px;
    min-height: 34px;
    box-shadow: none;
}}
button.about-close:hover {{ background-color: {tint(0.22)}; }}
button.about-close:focus {{ outline: none; box-shadow: inset 0 0 0 2px {accent}; }}
.home-resupply-pill {{
    background-color: {sidebar};
    border: 1px solid {tint(0.35)};
    border-radius: 999px;
    padding: 7px 18px 7px 14px;
    margin: 0 12px 14px 12px;
    box-shadow: 0 6px 14px rgba(0, 0, 0, 0.45);
    font-weight: 600;
}}
.home-new-pill {{
    background-color: {accent};
    color: {on_accent};
    border-radius: 999px;
    padding: 1px 10px;
    font-size: 80%;
    font-weight: 800;
}}
.home-new-pill.console {{ font-size: 110%; padding: 3px 14px; }}
.home-padglyph {{ font-size: 150%; font-weight: 900; }}
.home-keycap {{
    background-color: rgba(0, 0, 0, 0.16);
    border: 1px solid rgba(127, 127, 127, 0.45);
    border-radius: 6px;
    padding: 0 7px;
    font-size: 78%;
    font-weight: 700;
}}
.home-section {{ font-size: 125%; font-weight: 800; }}
.home-hero-kicker {{ color: {accent}; font-weight: 800; letter-spacing: 3px; font-size: 80%;
                     text-shadow: 0 1px 3px rgba(0, 0, 0, 0.8); }}
.home-hero-title {{ font-size: 260%; font-weight: 800; color: #ffffff;
                    text-shadow: 0 2px 12px rgba(0, 0, 0, 0.55); }}
.home-hero-meta label {{ color: rgba(255, 255, 255, 0.86); text-shadow: 0 1px 3px rgba(0, 0, 0, 0.7); }}
.home-hero-timer {{ font-size: 190%; font-weight: 800; color: {accent}; text-shadow: 0 1px 6px rgba(0, 0, 0, 0.6); }}
.home-hero-timer-caption {{ color: rgba(255, 255, 255, 0.82); margin-top: 6px; text-shadow: 0 1px 3px rgba(0, 0, 0, 0.7); }}
.home-hero-status {{ color: rgba(255, 255, 255, 0.82); }}
button.home-play {{ border-radius: 999px; padding: 8px 24px; }}
button.home-play label {{ font-weight: 800; }}
/* Setting sail: a busy pill, readable over any hero (see _home_play_button). */
button.home-play.home-play-busy,
button.home-play.home-play-busy:hover {{
    background-image: none;
    background-color: alpha({accent}, 0.22);
    border: 1px solid {accent};
    box-shadow: none;
}}
button.home-play.home-play-busy label {{ color: #ffffff; }}
button.home-play.home-play-busy spinner {{ color: {accent}; }}
/* Focused from the keyboard or a controller the button turns white, the
   way console UIs mark the focused button - an outer ring got clipped by
   the hero's text column, and an accent ring vanished on the accent fill. */
button.home-play:focus {{
    outline: none;
    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.45);
    background-image: none;
    background-color: #ffffff;
    border-color: #ffffff;
}}
button.home-play:focus label {{ color: #0b1514; }}
/* Set sail's glow is drawn by _draw_play_glow from a blur made once; this
   one is never painted - it only makes room for that glow round it. */
button.home-play.suggested-action.home-play-glow:not(:focus) {{ box-shadow: 0 0 0 20px transparent; }}
/* The 10 px on top are the room the card rises into - drawn by
   GameCard._draw_lift, shadow and all, with no relayout of the grid. */
button.home-card {{
    padding: 10px 0 0 0;
    margin: 0;
    border-radius: 8px;
    /* nothing of it changes on hover any more; .game-cover's "transition:
       0.2s" re-laid the whole window out on every frame of it */
    transition: none;
}}
/* No tinted backdrop behind a card in any state - the cover itself,
   its lift and shadow are the feedback (focus is painted on the cover). */
button.home-card.game-cover,
button.home-card.game-cover:hover:not(.game-cover-selected),
button.home-card.game-cover:not(.game-cover-selected):focus,
button.home-card.game-cover:not(.game-cover-selected):active,
button.home-card.game-cover:not(.game-cover-selected):checked {{
    background-color: transparent;
    background-image: none;
    border: none;
    /* never painted: it makes the card's clip big enough for the shadow
       GameCard._draw_lift draws when it rises */
    box-shadow: 0 0 0 54px transparent;
}}
button.home-card.game-cover.game-cover-selected {{
    background-color: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
}}
/* A row rises like a card when pointed at or stood on - into its top
   margin, its shadow and focus ring and all drawn by
   _draw_list_row_lift, with no relayout of the window. */
.home-list-row {{
    background-color: {raised};
    border-radius: 14px;
    padding: 6px 18px 6px 6px;
    margin-top: 5px;
    margin-bottom: 0;
    transition: none;
    /* never painted: it makes the row's clip big enough for that shadow */
    box-shadow: 0 0 0 40px transparent;
}}
button.home-list-row-btn {{ padding: 4px 10px; border-radius: 10px; transition: none; }}
button.home-list-row-btn.game-cover:not(.game-cover-selected),
button.home-list-row-btn.game-cover:hover:not(.game-cover-selected),
button.home-list-row-btn.game-cover:not(.game-cover-selected):focus,
button.home-list-row-btn.game-cover:not(.game-cover-selected):active {{
    background-color: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
    outline: none;
}}
/* A list row over its game's hero (see _draw_list_row_hero): the row
   paints the picture and its shade itself, the text goes light. */
.home-list-row.home-list-row-hero {{ background-color: transparent; }}
.home-list-row-hero button.home-list-row-btn label {{
    color: #ffffff;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.85);
}}
.home-list-row-hero button.home-list-row-btn label.settings-row-sub {{ color: rgba(255, 255, 255, 0.82); }}
.home-list-row-hero button.home-list-row-btn label.settings-status-on {{ color: {accent}; }}
.home-list-row-hero button.home-list-row-btn image {{ -gtk-icon-shadow: 0 1px 3px rgba(0, 0, 0, 0.7); }}
.home-empty-title {{ font-size: 140%; font-weight: 800; }}
.home-reorder-banner {{
    background-color: {tint(0.12)};
    border: 1px solid {tint(0.45)};
    border-radius: 16px;
    padding: 12px 16px;
}}
.home-reorder-title {{ font-size: 125%; font-weight: 800; }}
button.home-pill-ghost {{ border-radius: 999px; padding: 6px 16px; background-color: transparent; }}
/* The card dialogs (HomeMixin._card_dialog). */
.home-dialog-title {{ font-size: 135%; font-weight: 800; }}
/* The missing-art result (CoversMixin._art_fill_result): two number tiles. */
.art-result-tile {{ background-color: {tint(0.08)}; border-radius: 14px; padding: 10px 18px 12px 18px; }}
.art-result-num {{ font-size: 260%; font-weight: 800; }}
.art-result-cap {{ font-size: 95%; opacity: 0.7; }}
.art-result-hint {{ font-size: 95%; opacity: 0.75; }}
/* The undo toast (App._show_undo_toast): a pill like the resupply one,
   its Undo outlined in the accent and filled in under the pointer. The
   doubled classes outrank the generic button rules. */
.undo-toast.home-toast {{
    background-color: {sidebar};
    border: 1px solid {tint(0.35)};
    border-radius: 999px;
    padding: 6px 6px 6px 10px;
    box-shadow: 0 8px 22px rgba(0, 0, 0, 0.5);
}}
.undo-toast.home-toast label {{ color: {fg}; font-weight: 600; }}
button.undo-toast-btn.home-toast-btn {{
    background-image: none;
    background-color: transparent;
    border: 1px solid {accent};
    border-radius: 999px;
    box-shadow: none;
    padding: 6px 20px;
    transition: background-color 120ms ease-out;
}}
button.undo-toast-btn.home-toast-btn label {{ color: {accent}; font-weight: 800; }}
button.undo-toast-btn.home-toast-btn:hover,
button.undo-toast-btn.home-toast-btn:focus {{ background-color: {accent}; }}
button.undo-toast-btn.home-toast-btn:hover label,
button.undo-toast-btn.home-toast-btn:focus label {{ color: {on_accent}; }}
button.undo-toast-btn.home-toast-btn:active {{ background-color: alpha({accent}, 0.8); }}
/* "Back to top": a floating pill, lifting a little under the pointer. */
button.scroll-top-btn.home-top-btn {{
    background-image: none;
    background-color: {sidebar};
    border: 1px solid {tint(0.35)};
    border-radius: 999px;
    padding: 9px 20px 9px 15px;
    min-width: 0;
    min-height: 0;
    box-shadow: 0 8px 22px rgba(0, 0, 0, 0.5);
    transition: border-color 140ms ease-out, background-color 140ms ease-out;
}}
button.scroll-top-btn.home-top-btn label {{ color: {fg}; font-weight: 700; }}
button.scroll-top-btn.home-top-btn:hover {{
    background-color: {raised};
    border-color: {accent};
    /* lifted by a deeper shadow, not a margin: geometry would re-lay the window out */
    box-shadow: 0 11px 26px rgba(0, 0, 0, 0.6);
}}
button.scroll-top-btn.home-top-btn:active {{ background-color: {tint(0.18)}; }}
/* "Animations paused" (App._build_animations_paused_bar): a status pill
   like the resupply one. */
/* The margin is the room its shadow is drawn in: the revealer it slides
   down in cuts off whatever reaches past it, which showed as a hard-edged
   dark box around the pill. */
.animations-paused-bar.home-status-pill {{
    background-color: {sidebar};
    border: 1px solid {tint(0.35)};
    border-radius: 999px;
    padding: 6px 18px 6px 7px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.45);
    margin: 12px 24px 30px 24px;
}}
.animations-paused-bar.home-status-pill label {{ color: {fg}; }}
.animations-paused-bar.home-status-pill label.dim-label {{ color: {fg_dim}; }}
.home-status-title {{ font-weight: 700; }}
/* The game's right-click menu (game_page.GameMenu). */
popover.game-menu {{
    background-color: {sidebar};
    border: 1px solid {tint(0.22)};
    border-radius: 16px;
    padding: 8px;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
}}
.gm-title {{ font-weight: 800; font-size: 108%; }}
.gm-row-sub {{ color: {fg_dim}; font-size: 85%; }}
.gm-chev {{ color: {fg_dim}; }}
button.gm-quick {{
    background-image: none;
    background-color: {tint(0.08)};
    border: 1px solid transparent;
    border-radius: 12px;
    padding: 8px 6px;
    min-width: 76px;
    box-shadow: none;
}}
button.gm-quick:hover {{ background-color: {tint(0.18)}; border-color: {tint(0.35)}; }}
button.gm-quick label {{ font-size: 85%; font-weight: 700; }}
button.gm-quick.gm-primary {{ background-color: {accent}; }}
button.gm-quick.gm-primary:hover {{ background-color: alpha({accent}, 0.85); }}
button.gm-quick.gm-primary label {{ color: {on_accent}; }}
button.gm-quick.gm-danger {{ background-color: #e5534b; }}
button.gm-quick.gm-danger label {{ color: #1a0503; }}
button.gm-quick.gm-on label {{ color: {accent}; }}
button.gm-row {{
    background-image: none;
    background-color: transparent;
    border-color: transparent;
    box-shadow: none;
    border-radius: 10px;
    padding: 8px 10px;
}}
button.gm-row:hover, button.gm-row:focus {{ background-color: {tint(0.14)}; outline: none; }}
separator.gm-sep {{ margin: 6px 6px; min-height: 1px; background-color: {tint(0.16)}; }}
separator.gm-vsep {{ margin: 6px 8px; min-width: 1px; background-color: {tint(0.16)}; }}
button.gm-split {{
    background-image: none;
    background-color: transparent;
    border-color: transparent;
    box-shadow: none;
    border-radius: 10px;
    padding: 6px 8px;
}}
button.gm-split:hover {{ background-color: {tint(0.14)}; }}
button.gm-split label {{ color: {fg_dim}; font-size: 90%; font-weight: 600; }}
/* The game page and the SteamGridDB picker (game_page.GamePage / ArtPicker). */
/* Flat, so the hero's fade meets one colour: the dialog default is a gradient. */
dialog.gp-window, .gp-window {{ background-color: {sidebar}; background-image: none; border: 1px solid {tint(0.22)}; }}
.gp-tabs {{ background-color: {raised}; border-radius: 999px; padding: 4px; }}
.gp-tabs button {{
    background-image: none;
    background-color: transparent;
    border: none;
    box-shadow: none;
    border-radius: 999px;
    padding: 6px 18px;
}}
.gp-tabs button:checked {{ background-color: {accent}; }}
.gp-tabs button:checked label {{ color: {on_accent}; }}
.gp-tabs button label {{ font-weight: 700; }}
.gp-card {{ background-color: {raised}; border: 1px solid {tint(0.14)}; border-radius: 14px; padding: 14px 16px; }}
button.gp-card-btn {{
    background-image: none;
    background-color: {raised};
    border: 1px solid {tint(0.14)};
    border-radius: 14px;
    padding: 14px 16px;
    box-shadow: none;
}}
button.gp-card-btn:hover, button.gp-card-btn:focus {{ border-color: {accent}; background-color: {tint(0.10)}; }}
.gp-card-label {{ color: {fg_dim}; font-size: 82%; font-weight: 700; letter-spacing: 1px; }}
.gp-card-value {{ font-size: 112%; font-weight: 700; }}
.gp-open-hint {{ color: {accent}; font-size: 82%; font-weight: 700; }}
.gp-note {{ color: {fg_dim}; }}
/* The image picker (image_picker.ImageFilePicker): a file dragged over
   it lights the whole window's edge. */
.up-root {{ border: 2px solid transparent; border-radius: 12px; }}
.up-root.up-drop {{ border: 2px dashed {accent}; background-color: {tint(0.05)}; }}
.up-name {{ font-size: 85%; }}
button.gp-tool-missing {{
    background-image: none;
    background-color: alpha(#e8a33d, 0.14);
    border: 1px solid alpha(#e8a33d, 0.45);
    border-radius: 999px;
    padding: 2px 10px;
    box-shadow: none;
    color: #e8a33d;
    font-weight: 600;
    font-size: 88%;
}}
button.gp-tool-missing:hover {{ background-color: alpha(#e8a33d, 0.26); }}
button.insp-passed {{
    background-image: none;
    background-color: transparent;
    border: none;
    border-radius: 0 0 14px 14px;
    box-shadow: none;
}}
button.insp-passed:hover {{ background-color: {tint(0.08)}; }}
.batch-glyph {{ font-weight: 800; }}
/* Shipwright: tiles that open their card with a click and are picked
   with the tick in their corner, and a bar with what's picked */
button.sw-tile {{
    background-image: none;
    background-color: {raised};
    border: 2px solid {tint(0.10)};
    border-radius: 16px;
    padding: 16px 16px;
    box-shadow: none;
}}
/* No fade on the tiles: GTK restyles a whole tile on every frame of one
   (sweeping over them took 65 % CPU, 23 % without) */
button.sw-tile {{ transition: none; }}
.sw-tile-status {{ font-size: 88%; }}
.sw-section-count {{ color: {fg_dim}; font-size: 85%; margin-left: 4px; }}
button.sw-link {{
    background: transparent; border: none; box-shadow: none;
    color: {accent}; font-weight: 700; padding: 4px 10px; border-radius: 999px;
}}
button.sw-link:hover {{ background-color: {tint(0.14)}; }}
button.sw-chip {{
    background-image: none;
    background-color: transparent;
    border: 1px solid {tint(0.22)};
    border-radius: 999px;
    padding: 5px 16px;
    box-shadow: none;
    color: {fg_dim};
    font-weight: 600;
}}
button.sw-chip:hover {{ background-color: {tint(0.10)}; color: {fg}; }}
button.sw-chip:checked {{ background-color: {accent}; border-color: {accent}; color: {on_accent}; }}
button.sw-chip:checked label {{ color: {on_accent}; }}
/* Shipwright's Proton page (ui/proton_page.py) */
.proton-pill {{
    border-radius: 999px;
    padding: 1px 9px;
    font-size: 75%;
    font-weight: 700;
}}
.proton-pill-accent {{ background-color: {tint(0.18)}; color: {accent}; }}
.proton-pill-quiet {{ background-color: {tint(0.08)}; color: {fg_dim}; }}
.proton-status-title {{ font-size: 115%; }}
/* The release notes window (ui/release_notes_window.py). */
.notes-sidebar {{ background-color: {tint(0.04)}; border-radius: 20px 0 0 20px; }}
.notes-divider {{ background-color: {tint(0.14)}; min-width: 1px; }}
.notes-nav-title {{ font-weight: 700; }}
.notes-bullet {{ color: {accent}; font-weight: 800; }}
.notes-code {{
    background-color: {tint(0.07)};
    border: 1px solid {tint(0.12)};
    border-radius: 10px;
    padding: 10px 14px;
    margin: 4px 0;
}}
.notes-code-text {{ font-family: monospace; font-size: 92%; }}
button.notes-game-chip {{
    background-color: {tint(0.16)};
    background-image: none;
    color: {accent};
    border: none;
    box-shadow: none;
    border-radius: 999px;
    padding: 2px 12px;
    min-height: 0;
    font-weight: 700;
}}
button.notes-game-chip:hover {{ background-color: {tint(0.26)}; }}
/* only the background changes on hover: a colour change lays the window out again */
button.notes-close {{
    min-width: 34px; min-height: 34px; padding: 0;
    border-radius: 10px; border: none; box-shadow: none; background: transparent;
}}
button.notes-close:hover {{ background-color: {tint(0.12)}; }}
/* "A newer Proton release mentions this game" (ui/proton_hints.py) */
.ph-strip {{
    background-color: {tint(0.10)};
    border: 1px solid {tint(0.28)};
    border-radius: 16px;
    padding: 14px 16px;
}}
.ph-title {{ font-weight: 700; color: {accent}; }}
/* opened from the card's badge: lit a moment (GamePage._bring_hint) - border and
   shadow only, once */
.ph-strip {{ transition: border-color 400ms ease-out, box-shadow 400ms ease-out; }}
.ph-strip.ph-strip-lit {{ border-color: {accent}; box-shadow: 0 0 18px {tint(0.45)}; }}
.ph-quote {{ font-style: italic; color: {fg_dim}; }}
button.ph-pill {{
    background-color: {tint(0.18)}; background-image: none; color: {accent};
    border: none; box-shadow: none; border-radius: 999px;
    padding: 0 9px; min-height: 0; font-size: 75%; font-weight: 700;
}}
button.ph-pill:hover {{ background-color: {tint(0.30)}; }}
/* The Captain's Log (ui/captains_log.py): the notes on a raised panel */
scrolledwindow.log-notes {{
    background-color: {tint(0.06)};
    border: 1px solid {tint(0.12)};
    border-radius: 14px;
    margin-top: 6px;
}}
.home-chip-count {{ opacity: 0.7; font-weight: 600; }}
.sw-strip {{
    background-color: {raised};
    border: 1px solid {tint(0.10)};
    border-radius: 14px;
    padding: 10px 14px;
}}
.sw-strip.sw-strip-warn {{ border-color: #e8a33d; background-color: alpha(#e8a33d, 0.08); }}
.sw-strip-title {{ font-weight: 700; }}
.sw-picked {{ font-weight: 600; }}
button.sw-source {{
    background-image: none;
    background-color: {tint(0.08)};
    border: 1px solid {tint(0.26)};
    box-shadow: none;
    padding: 1px 12px;
    min-height: 0;
    font-size: 86%;
    font-weight: 700;
    color: {fg_dim};
}}
button.sw-source:first-child {{ border-radius: 999px 0 0 999px; }}
button.sw-source:last-child {{ border-radius: 0 999px 999px 0; }}
/* background only: a label's colour change on hover re-lays the window out */
button.sw-source:hover {{ background-color: {tint(0.18)}; }}
button.sw-source:checked {{ background-color: {accent}; border-color: {accent}; color: {on_accent}; }}
button.sw-source:checked label {{ color: {on_accent}; }}
button.sw-source:disabled {{ opacity: 0.45; }}
button.sw-tile:hover {{ background-color: {tint(0.10)}; border-color: {tint(0.30)}; }}
button.sw-tile.sw-picked {{ border-color: {accent}; background-color: {tint(0.16)}; }}
button.sw-tile.sw-picked-remove {{ border-color: {DANGER}; background-color: alpha({DANGER}, 0.10); }}
/* the tick in a tile's corner (Tick draws the box) */
button.sw-tick {{
    padding: 4px; min-width: 0; min-height: 0; border-radius: 9px;
    background: none; border: none; box-shadow: none; outline: none;
}}
button.sw-tick:hover {{ background-color: {tint(0.16)}; }}
/* a tile's card (ToolCard) */
.sw-card-desc {{ color: {fg}; font-size: 105%; }}
.sw-card-caption {{ color: {fg_dim}; font-size: 75%; font-weight: 800; letter-spacing: 2px; }}
/* stood on with the keyboard or a controller: the tile's own border
   lights up - a ring round it as well drew double, cut at the bottom */
button.sw-tile:focus {{ box-shadow: none; outline: none; border-color: alpha({fg}, 0.85); }}
button.sw-tile.sw-picked:focus {{ border-color: {accent}; box-shadow: inset 0 0 0 1px alpha({fg}, 0.7); }}
button.sw-tile.sw-picked-remove:focus {{ border-color: {DANGER}; box-shadow: inset 0 0 0 1px alpha({fg}, 0.7); }}
.sw-tile-name {{ font-weight: 700; font-size: 105%; }}
.sw-badge {{
    background-color: {tint(0.18)};
    color: {accent};
    border-radius: 999px;
    padding: 1px 9px;
    font-size: 78%;
    font-weight: 700;
}}
.sw-nav-count {{ color: {fg_dim}; font-size: 85%; }}
/* the marked shelf follows the scrolling: a fade on every change kept
   repainting most of the window while scrolling */
.sw-nav row {{ transition: none; }}
.sw-collapsed .settings-nav row {{ padding: 8px 2px; margin: 2px 8px; }}
.sw-collapsed .sw-nav-count {{ font-size: 78%; }}
/* the hairline between the pages and the shelves (_nav_divider) */
.settings-nav row.sw-nav-divider, .settings-nav row.sw-nav-divider:hover {{
    padding: 0; margin: 6px 18px; min-height: 1px; background: none;
}}
.sw-collapsed .settings-nav row.sw-nav-divider {{ margin: 6px 24px; }}
.sw-nav-divider-line {{ background-color: {tint(0.16)}; min-height: 1px; }}
button.sw-collapse {{
    min-width: 34px; min-height: 34px; padding: 0;
    border-radius: 10px; border: none; box-shadow: none;
    background: transparent; color: {fg_dim};
}}
button.sw-collapse:hover {{ background-color: {tint(0.12)}; color: {fg}; }}
.sw-nav-warn {{ color: #e8a33d; font-weight: 700; }}
.sw-nav-accent {{ color: {accent}; font-weight: 700; }}
.sw-attention-badge {{
    background-color: {accent};
    color: {on_accent};
    border-radius: 999px;
    padding: 1px 8px;
    font-size: 75%;
    font-weight: 700;
}}
.sw-attention-badge.sw-attention-warn {{ background-color: #e8a33d; color: #1d1405; }}
.sw-attention-badge.sw-attention-rec {{
    background-color: transparent;
    color: {accent};
    box-shadow: inset 0 0 0 1px {accent};
}}
.sw-hero {{
    background-color: {raised};
    border: 1px solid {tint(0.10)};
    border-radius: 16px;
    padding: 18px 20px;
}}
.pfx-pick-scroll {{
    background-color: {raised};
    border: 1px solid {tint(0.14)};
    border-radius: 14px;
}}
list.pfx-pick-list {{ background-color: transparent; }}
.pfx-pick-list row {{ border-radius: 10px; margin: 2px 6px; background: transparent; }}
.pfx-pick-list row:hover {{ background-color: {tint(0.10)}; }}
.pfx-pick-list row:selected {{ background-color: {tint(0.22)}; color: {fg}; }}
.pfx-pick-list row:selected label {{ color: {fg}; }}
.pfx-pick-row {{ padding: 8px 10px; }}
.pfx-pick-name {{ font-weight: 700; }}
.sw-actionbar {{
    background-color: {sidebar};
    border-top: 1px solid {tint(0.18)};
    padding: 12px 22px;
    border-radius: 0 0 20px 0;
}}
.batch-pending {{ color: {fg_dim}; }}
.batch-active {{ color: {accent}; font-weight: 700; }}
.batch-done {{ color: {accent}; }}
.batch-failed {{ color: {DANGER}; }}
.batch-skipped {{ color: {fg_dim}; }}
.gp-status-on {{ color: {accent}; font-weight: 700; }}
.gp-setting {{ padding: 8px 0; }}
button.gp-close {{
    background-image: none;
    background-color: rgba(0, 0, 0, 0.45);
    border: none;
    border-radius: 999px;
    padding: 6px;
    box-shadow: none;
}}
button.gp-close:hover {{ background-color: rgba(0, 0, 0, 0.65); }}
button.gp-close:focus {{ outline: none; box-shadow: inset 0 0 0 2px {accent}; }}
.settings-pill-strong {{ background-color: {accent}; color: {on_accent}; }}
button.gp-info {{
    background-image: none;
    background-color: transparent;
    border: none;
    box-shadow: none;
    border-radius: 999px;
    padding: 4px;
}}
button.gp-info:hover {{ background-color: {tint(0.16)}; }}
button.gp-chip {{
    background-image: none;
    background-color: {raised};
    border: 1px solid {tint(0.18)};
    border-radius: 999px;
    padding: 4px 14px;
    box-shadow: none;
}}
button.gp-chip:checked {{ background-color: {tint(0.22)}; border-color: {accent}; }}
button.gp-chip label {{ font-weight: 600; }}
.gp-footer {{ background-color: {sidebar}; border-top: 1px solid {tint(0.16)}; padding: 12px 18px; }}
list.saves-backup-list {{ background-color: transparent; }}
list.saves-backup-list row {{ border-radius: 10px; }}
list.saves-backup-list row:selected {{ background-color: {tint(0.22)}; }}
.gm-warning {{
    background-color: alpha(#e8a33d, 0.16);
    border: 1px solid alpha(#e8a33d, 0.45);
    border-radius: 10px;
    padding: 8px 10px;
    margin: 4px 6px 6px 6px;
}}
.gp-notice {{
    background-color: {tint(0.10)};
    border: 1px solid {tint(0.30)};
    border-radius: 14px;
    padding: 12px 16px;
}}
.gp-code-chip {{
    background-color: {tint(0.12)};
    border: 1px solid {tint(0.30)};
    border-radius: 8px;
    padding: 3px 10px;
    font-family: monospace;
    font-size: 90%;
}}
textview.gp-text, textview.gp-text text {{ background-color: {sidebar}; border-radius: 10px; }}
/* The shortcuts dialog (HomeMixin._show_shortcuts_dialog). */
.home-shortcuts-panel {{
    background-color: {raised};
    border: 1px solid {tint(0.18)};
    border-radius: 16px;
    padding: 16px 18px 10px 18px;
}}
.home-shortcuts-rule {{ background-color: {tint(0.12)}; min-height: 1px; }}
.home-shortcuts-pad-head {{ color: {fg_dim}; font-size: 80%; font-weight: 700; margin-bottom: 4px; }}
.home-shortcuts-brand {{ font-size: 85%; font-weight: 700; margin-bottom: 4px; }}
/* A keycap: a lighter top and a thicker bottom edge. */
.home-shortcut-key {{
    background-color: {tint(0.12)};
    border: 1px solid {tint(0.40)};
    border-bottom-width: 3px;
    border-radius: 8px;
    padding: 2px 9px;
    min-width: 14px;
    min-height: 22px;
    font-size: 88%;
    font-weight: 800;
    color: {fg};
}}
.home-shortcut-pad {{
    background-color: rgba(255, 255, 255, 0.05);
    border: 1px solid {tint(0.35)};
    border-radius: 999px;
    font-weight: 800;
    color: {fg};
}}
.home-shortcut-pad-wide {{ padding: 4px 12px; min-width: 40px; font-size: 85%; }}
/* A combo's options as a card (App._open_combo_picker): rows like Ship's
   Controls', the one stood on lit with the accent's line on its left. */
list.combo-picker-list {{ background: none; }}
list.combo-picker-list row {{
    padding: 10px 14px; margin: 1px 0; border-radius: 10px;
    background: none; box-shadow: none; outline: none;
}}
list.combo-picker-list row:hover {{ background-color: {tint(0.10)}; }}
list.combo-picker-list row:focus, list.combo-picker-list row.kb-focus {{
    background-color: {tint(0.16)}; box-shadow: inset 4px 0 0 {accent}; outline: none;
}}
list.combo-picker-list row.combo-picker-current label {{ color: {accent}; font-weight: 700; }}
/* What the keyboard or a controller stands on (ui/keynav.py), when it
   isn't a whole setting's row: a ring in the text colour - it shows on
   the accent fill of a picked tab too. Last, over every :focus rule. */
button.kb-focus, button.kb-focus:focus, button.kb-focus:hover, button.kb-focus:checked,
button.kb-focus:checked:focus, button.kb-focus:active, switch.kb-focus, switch.kb-focus:focus,
scale.kb-focus, entry.kb-focus, spinbutton.kb-focus, row.kb-focus, row.kb-focus:focus {{
    outline: none;
    box-shadow: 0 0 0 2px alpha({fg}, 0.9);
}}
"""


def settings_css_for_palette(p: Dict) -> str:
    rgb = p["accent_rgb"]
    args = (p["accent"], lambda a: f"rgba({rgb}, {a})", p["bg_raised"], p["bg_alt"], p["fg"], p["fg_dim"])
    return _settings_css(*args) + _home_css(*args, on_accent=p["on_accent"])


def settings_css_for_system() -> str:
    """The same, on top of whatever GTK theme the desktop uses."""
    args = ("@theme_selected_bg_color", lambda a: f"alpha(@theme_selected_bg_color, {a})",
            "alpha(@theme_fg_color, 0.05)", "alpha(@theme_fg_color, 0.03)",
            "@theme_fg_color", "alpha(@theme_fg_color, 0.62)")
    return _settings_css(*args) + _home_css(*args, on_accent="@theme_selected_fg_color")


def _build_theme_css(p: Dict) -> str:
    accent = p["accent"]
    accent_rgb = p["accent_rgb"]
    on_accent = accent_text_color(p)
    border = f"rgba({accent_rgb}, 0.30)"


    is_dark = bool(p.get("dark", True))

    shadow_rgb = "0, 0, 0" if is_dark else _hex_to_rgb_str(p["fg"])
    if is_dark:
        shadow_sm = f"0 1px 3px rgba({shadow_rgb}, 0.45)"
        shadow_md = f"0 2px 8px rgba({shadow_rgb}, 0.40)"
        shadow_lg = f"0 6px 20px rgba({shadow_rgb}, 0.50)"
    else:
        shadow_sm = f"0 1px 3px rgba({shadow_rgb}, 0.10)"
        shadow_md = f"0 2px 8px rgba({shadow_rgb}, 0.10)"
        shadow_lg = f"0 6px 20px rgba({shadow_rgb}, 0.14)"

    bg_overlay = _blend(p["bg_raised"], accent, 0.06 if is_dark else 0.04)

    r_sm = "6px"
    r_md = "8px"
    r_lg = "14px"
    r_pill = "999px"

    glyph_bg = "rgba(15, 15, 20, 0.92)" if is_dark else "rgba(255, 255, 255, 0.96)"
    glyph_fg = "#ffffff" if is_dark else p["fg"]
    scrim_bg = "rgba(20, 20, 24, 0.45)" if is_dark else "rgba(255, 255, 255, 0.72)"
    scrim_bg_hover = "rgba(20, 20, 24, 0.75)" if is_dark else "rgba(255, 255, 255, 0.92)"
    scrim_strong = "rgba(20, 20, 24, 0.94)" if is_dark else "rgba(255, 255, 255, 0.97)"
    scrim_fg = "#ffffff" if is_dark else p["fg"]
    hairline = ("rgba(255, 255, 255, 0.12)" if is_dark
                else f"rgba({_hex_to_rgb_str(p['fg'])}, 0.14)")

    accent_part = _accent_css(accent, accent, f"rgba({accent_rgb}, 0.16)",
                              f"rgba({accent_rgb}, 0.26)", accent_rgb,
                              card_shadow=shadow_md, card_shadow_active=shadow_lg,
                              on_accent=on_accent)
    chrome_part = f"""
window, .background {{
    background-color: {p['bg']};
    background-image: none;
    color: {p['fg']};
}}
/* Deliberately NOT the same gradient rule as drawingarea.ocean-bg below,
   even though the recipe looks identical at a glance - background-size/
   -position are calculated relative to EACH element's own box, so the
   window (tall, includes the header/title-pill area) and the bubble
   canvas (shorter, only the scrollable area) would crop the "same"
   gradient completely differently, creating a visible seam exactly where
   they meet. Keeping the window flat avoids that seam entirely - it's
   only visible in the narrow strip above the game list anyway, where a
   plain color reads as intentional rather than a mismatched patch. */
dialog, messagedialog, filechooser,
.dialog-vbox, notebook, stack {{
    background-color: {p['bg']};
    background-image: linear-gradient(120deg, {p['bg']} 0%, {p['bg_alt']} 55%, {p['bg']} 100%);
    background-repeat: no-repeat;
    background-size: 160% 160%;
    background-position: 20% 10%;
    color: {p['fg']};
}}
/* Same gradient as above, but pinned to "0% 0%" instead of "20% 10%" -
   this guarantees the gradient's own top-left corner (its 0% color stop,
   i.e. plain background color) is what actually touches the flat window background
   directly above it, so the seam between them lines up on an EXACT color
   match rather than just "hopefully close enough". */
drawingarea.ocean-bg {{
    background-color: {p['bg']};
    background-image: linear-gradient(120deg, {p['bg']} 0%, {p['bg_alt']} 55%, {p['bg']} 100%);
    background-repeat: no-repeat;
    background-size: 160% 160%;
    background-position: 0% 0%;
    color: {p['fg']};
}}
/* The scrollable game list sits ON TOP of a dedicated "drawingarea.ocean-
   bg" canvas (see the self.main_overlay / self.bubble_canvas setup in
   __init__, and _on_bubble_canvas_draw) that paints this same gradient
   as its own CSS background, with the floating bubbles drawn over it in
   Cairo - so scrolledwindow/viewport must stay fully transparent below,
   or they'd paint over that canvas and hide it completely. Per the
   product requirement ("static by default, like being underwater"), the
   gradient itself does NOT move - only the bubble field does, driven
   purely by scrolling (see _on_scroll_value_changed / _tick_bubbles). */
scrolledwindow, viewport {{
    background-color: transparent;
    background-image: none;
}}
headerbar, .titlebar {{
    background-image: none;
    background: {p['bg_alt']};
    color: {p['fg']};
    border-bottom: 1px solid {border};
    box-shadow: none;
    text-shadow: none;
}}
/* Header toolbar buttons are FLAT. They used to each carry their own
   outline, which put about a dozen boxes in a row across the top - and
   the bar already groups things with separators, so the outlines were a
   second, competing grouping mechanism saying nothing extra. The border
   comes back on hover and when a toggle is active, which is when it
   actually means something.

   Linked groups (the grid/list pair, the Launchers/All switch) keep
   their outline: there the border is what says "these belong together
   and one of them is on". */
headerbar button,
.titlebar button {{
    background-color: transparent;
    border-color: transparent;
    box-shadow: none;
}}
headerbar button:hover,
.titlebar button:hover {{
    background-color: rgba({accent_rgb}, 0.16);
    border-color: rgba({accent_rgb}, 0.35);
}}
/* Linked groups get their outline back, as a separate and more specific
   rule. Excluding them from the rule above with a negation instead is
   not possible here: GTK3's CSS parser accepts only a simple selector
   inside a negation, and a descendant combinator in one is a parse
   error that takes the rest of the stylesheet down with it - which is
   how this file once left the whole app rendering in default grey. */
headerbar .linked button,
.titlebar .linked button {{
    background-color: {p['bg_raised']};
    border-color: {border};
}}
/* One active-state language for every toggle in the bar. The view
   switcher used an accent border plus an accent icon while the
   Launchers/All switch used an accent border plus accent text - similar
   enough to look like a mistake, different enough to be one. */
headerbar button:checked,
headerbar button:active,
.titlebar button:checked,
.titlebar button:active {{
    background-color: rgba({accent_rgb}, 0.22);
    border-color: {accent};
    color: {p['fg']};
}}
/* Window controls sit lower in the visual hierarchy than the app's own
   actions: closing the window is not the same kind of thing as opening
   Settings, and at identical weight they read as interchangeable. */
.window-control-btn {{
    background-color: transparent;
    border-color: transparent;
    color: {p['fg_dim']};
    box-shadow: none;
}}
.window-control-btn:hover {{
    background-color: rgba({accent_rgb}, 0.16);
    border-color: transparent;
    color: {p['fg']};
}}
/* Close gets the red hover every desktop uses, so the one destructive
   control in the row is identifiable before it's clicked. */
.window-close-btn:hover {{
    background-color: #c0392b;
    color: #ffffff;
}}
headerbar .title, .titlebar .title {{
    color: {p['fg']};
}}
messagedialog .titlebar, dialog .titlebar {{
    background-image: none;
    background: {p['bg_alt']};
}}
/* GTK's own dashed focus rectangle, nowhere: it showed on whatever a
   click had just focused - a button, a slider, a switch. Keyboard and
   controller focus has the app's own ring (.controller-focus-ring);
   outlines the app draws on purpose (a setting being edited) are set
   by more specific rules and stay. */
* {{ outline-style: none; outline-width: 0; }}
/* Buttons are pills, as everything else in the app is rounded: a light
   accent tint with a thin accent edge, stronger under the pointer. The
   app's own buttons (cards, menu rows, tiles...) set their own shape
   further down and win over this. */
button:not(.game-cover) {{
    /* No dotted rectangle after a click - keyboard and controller focus
       has its own ring (.controller-focus-ring). */
    outline: none;
    background-image: none;
    background-color: rgba({accent_rgb}, 0.10);
    color: {p['fg']};
    border: 1px solid rgba({accent_rgb}, 0.26);
    border-radius: {r_pill};
    padding: 6px 16px;
    font-weight: 600;
    box-shadow: none;
    text-shadow: none;
    transition: background-color 120ms ease-out, border-color 120ms ease-out, box-shadow 120ms ease-out;
}}
button:not(.game-cover):hover {{
    background-color: rgba({accent_rgb}, 0.20);
    border-color: rgba({accent_rgb}, 0.55);
}}
button:not(.game-cover):active, button:not(.game-cover):checked {{
    background-color: rgba({accent_rgb}, 0.30);
}}
button:not(.game-cover):disabled {{
    background-color: rgba({accent_rgb}, 0.05);
    border-color: rgba({accent_rgb}, 0.12);
}}
/* The drop-down's own button: the arrow in the accent. */
combobox button arrow, combobox button .arrow {{ color: {accent}; -gtk-icon-shadow: none; }}
combobox button {{ outline: none; padding: 6px 14px 6px 16px; }}
/* Side by side in a group they share one pill: rounded only at its ends. */
.linked > button:not(.game-cover) {{ border-radius: 0; }}
.linked > button:not(.game-cover):first-child {{ border-radius: {r_pill} 0 0 {r_pill}; }}
.linked > button:not(.game-cover):last-child {{ border-radius: 0 {r_pill} {r_pill} 0; }}
.linked > button:not(.game-cover):only-child {{ border-radius: {r_pill}; }}
/* Inside a number field the +/- stay small and square-ish. */
spinbutton button {{ border-radius: 8px; padding: 2px 8px; border-color: transparent; }}
button.suggested-action {{
    background-image: none;
    background-color: {accent};
    color: {on_accent};
    border-color: {accent};
    box-shadow: 0 2px 10px rgba({accent_rgb}, 0.35);
}}
button.suggested-action:hover {{
    background-color: {p['accent_hover']};
    border-color: {p['accent_hover']};
}}
button.suggested-action:active {{
    background-color: {p['accent_active']};
}}
/* Game/launcher cards (see _create_cover_image / _create_launcher_card,
   .game-cover class) - the generic "button" rule above deliberately does
   NOT apply to them (see :not(.game-cover) above). Their resting state
   (transparent background, no border) lives in the structural CSS
   (_load_css) - the hover/active background color is set EXCLUSIVELY by
   _accent_css() (above), so there's never two conflicting rules on the
   same node. */
entry, textview, textview text, treeview, treeview.view {{
    background-color: {p['bg_alt']};
    color: {p['fg']};
    border-color: {border};
    caret-color: {accent};
}}
/* Adwaita draws a focus ring on focused entries (e.g. the header search
   box) using its own default accent color (blue), regardless of which
   color is used everywhere else - this overrides it to match the active
   theme's accent instead, the same color used for every other highlight
   in the app (see _accent_css).

   :not(#controller-focus-ring-active) - so this rule steps aside
   entirely (rather than just hoping to lose on specificity) whenever
   App._on_focus_ring_widget_notify's own, deliberately much louder
   focus indicator is the one currently active on this exact entry -
   see that ID's own rule in App._load_css. */
entry:focus:not(#controller-focus-ring-active), entry:focus-within:not(#controller-focus-ring-active) {{
    border-color: {accent};
    box-shadow: inset 0 0 0 1px {accent};
}}
notebook > header {{
    background-image: none;
    background-color: {p['bg_alt']};
    border-color: {border};
}}
notebook header tabs tab {{
    color: {p['fg_dim']};
}}
/* The active/current tab's own indicator - Adwaita (and its many
   per-distro forks) draws this differently across GTK3 versions: some
   paint it via a bottom border, others via an inset box-shadow
   instead - setting BOTH here covers either mechanism, rather than
   only fixing it on whichever one happens to be used on any one
   given system. "notebook header tabs tab" (descendant combinators
   throughout, not "notebook > header") for the same reason - a direct-
   child combinator between notebook and header broke on at least one
   real GTK3 version where an extra wrapper node sits between them,
   silently making this whole rule never match anything on that
   system, so it never overrode Adwaita's own default (blue) tab
   indicator color there at all. */
notebook header tabs tab:checked,
notebook header tabs tab:checked:hover,
notebook header tabs tab:checked:focus {{
    color: {accent};
    border-bottom: 2px solid {accent};
    box-shadow: inset 0 -2px 0 0 {accent};
    background-image: none;
}}
/* The Settings dialog's own search results (see show_options -
   search_results_list, a Gtk.ListBox in BROWSE selection mode) -
   without this, the currently-selected/highlighted result used
   Adwaita's own default (blue) selected-row color, the one other
   place in the app that still did, once the notebook tab fix above
   was in. on_accent (rather than assuming white, or leaving GTK's
   own default selected-row text color) is what keeps the result's
   text legible against {accent} specifically - see THEME_PALETTES's
   own docstring for why every palette carries its own pre-chosen
   contrast color for exactly this "text sitting ON TOP of the accent
   color itself" situation, rather than computing one on the fly.

   "list row:selected", NOT "list-box row:selected" - a first attempt
   at this rule used the latter (the Python/C CLASS name, GtkListBox)
   and silently matched nothing at all: per GTK3's own CSS node docs,
   GtkListBox's actual CSS node is named "list" (GtkListBoxRow's is
   "row", already correct above) - the two only happen to share a
   name for plenty of OTHER widget types (button, entry, ...) where
   the class name and CSS node name are identical, which is what made
   the mismatch easy to miss here specifically. */
list row:selected,
list row:selected:hover,
list row:selected:focus {{
    background-color: {accent};
    background-image: none;
    color: {on_accent};
}}
/* The native GtkFileChooser's own file list is a GtkTreeView (CSS
   node "treeview"), completely separate from the ListBox-based
   "list row:selected" rule right above - without this, a selected
   row there had no visible highlight at all against this theme's
   dark background (reported directly: clicking a file in "Import
   plunder inventory" gave no feedback that anything was selected).
   Scoped to "filechooser" specifically, rather than a bare
   "treeview:selected", since the app itself never uses a
   Gtk.TreeView anywhere (the game grid/list are FlowBox/ListBox) -
   no reason to risk this affecting some other TreeView-based widget
   this theme hasn't been checked against. */
filechooser treeview:selected,
filechooser treeview:selected:hover,
filechooser treeview:selected:focus {{
    background-color: {accent};
    background-image: none;
    color: {on_accent};
}}
scrollbar slider {{
    background-color: rgba({accent_rgb}, 0.32);
    border-radius: {r_pill};
}}
scrollbar slider:hover {{
    background-color: rgba({accent_rgb}, 0.52);
}}
/* Tooltips. Setting nothing here isn't the same as leaving them alone:
   "window, .background" above also matches the tooltip window, so its
   background followed the palette while its text kept Adwaita's fixed
   near-white - unreadable on the light themes.

   The color has to go on the label too; Adwaita targets "tooltip label"
   specifically and a rule on the parent loses to it. The .tooltip
   variant covers GTK before the 3.20 node rename. */
tooltip, tooltip.background, .tooltip {{
    background-color: {bg_overlay};
    background-image: none;
    color: {p['fg']};
    border: 1px solid {border};
    border-radius: {r_sm};
    box-shadow: {shadow_md};
    text-shadow: none;
}}
tooltip label, tooltip.background label, .tooltip label {{
    color: {p['fg']};
    background-color: transparent;
}}
/* Popovers sit at the highest surface level - at bg_alt they matched
   the dialog behind them and relied on their border alone. */
menu, menuitem, popover, popover.background, popover contents {{
    background-color: {bg_overlay};
    color: {p['fg']};
}}
popover, popover.background {{
    /* Shadow rather than outline-plus-shadow. Light themes keep a
       hairline - a soft shadow alone doesn't define an edge on a
       pale background. */
    border: {"none" if is_dark else f"1px solid {border}"};
    box-shadow: {shadow_lg};
}}
menuitem:hover {{
    background-color: rgba({accent_rgb}, 0.16);
}}
/* Drop-down lists and right-click menus: a rounded card with a shadow,
   rows that light up as rounded pills. */
/* the menu's own window stays clear around the card's corners */
window.popup, window.popup.background {{ background-color: transparent; background-image: none; }}
.csd.popup decoration, window.popup decoration {{
    border-radius: 14px;
    box-shadow: {shadow_lg}, 0 0 0 1px rgba({accent_rgb}, 0.22);
}}
menu, .menu {{
    border-radius: 14px;
    padding: 8px;
    border: none;
}}
menu menuitem {{
    border-radius: 9px;
    padding: 8px 14px;
    min-height: 0;
    background-color: transparent;
    transition: background-color 100ms ease-out;
}}
menu menuitem:hover {{
    background-color: rgba({accent_rgb}, 0.20);
    color: {p['fg']};
}}
menu menuitem:hover label {{ color: {p['fg']}; }}
menu menuitem label {{ font-weight: 500; }}
menu arrow {{ border: none; background: none; min-height: 14px; }}
separator {{
    background-color: {border};
}}
/* A touch more muted than the generic `separator` rule above - this
   one specifically marks "a new settings section starts here" (see
   App._settings_heading), meant to read as a soft, barely-there break
   rather than a strong visual line. Half of `border`'s own 0.30 alpha
   (see border's own definition above), built directly from the same
   accent_rgb rather than nesting alpha() around an already-rgba()
   color, which GTK's CSS engine doesn't reliably support. */
.settings-section-rule {{
    background-color: rgba({accent_rgb}, 0.15);
    min-height: 1px;
}}
/* Extra vertical breathing room around every checkbox/radio row in
   Settings - confirmed live that GtkCheckButton/GtkRadioButton's own
   "check"/"radio" indicator node ignores margin, padding, min-width,
   and even a background-color set directly on it in this GTK
   version, so this padding on the outer widget is the only lever
   that actually has any visible effect here. Paired with
   _check_with_icon/_radio_with_icon's own custom icon-then-label Box
   (which DOES let the icon-to-label gap be controlled directly) and
   the wider spacing now used on every settings tab's main vbox -
   together these are what actually fixed the reported "everything
   blends together" crampedness, not this rule alone. */
checkbutton, radiobutton {{
    padding: 4px 0;
}}
/* A brief, timed glow (see App._flash_highlight_widget) around whatever
   specific setting a Settings-search result actually landed on -
   removed again automatically a couple of seconds later, not a
   persistent state. */
.settings-search-highlight {{
    background-color: rgba({accent_rgb}, 0.22);
    border-radius: {r_sm};
}}
scale trough {{
    background-color: {p['bg_raised']};
    border-radius: {r_pill};
}}
scale highlight {{
    background-color: {accent};
    border-radius: {r_pill};
}}
scale slider {{
    background-color: {p['fg']};
    border: 1px solid {accent};
}}
/* The Auto update window (ui/self_update_mixin.py). */
.update-title {{
    font-size: 135%;
    font-weight: 700;
}}
/* Result summaries (e.g. the bulk cover download): big numbers on
   raised cards, the good one in the theme's accent. */
.summary-title {{
    font-size: 130%;
    font-weight: 700;
}}
button.summary-stat {{
    background-color: {p['bg_raised']};
    background-image: none;
    border: 2px solid transparent;
    border-radius: {r_lg};
    box-shadow: none;
    padding: 10px 16px;
}}
button.summary-stat:hover {{
    background-color: rgba({accent_rgb}, 0.10);
}}
button.summary-stat:checked {{
    background-color: rgba({accent_rgb}, 0.14);
    border-color: {accent};
}}
.summary-stat-value {{
    font-size: 220%;
    font-weight: 800;
}}
.summary-stat-good {{
    color: {accent};
}}
.summary-stat-bad {{
    color: #e5534b;
}}
.summary-stat-zero {{
    color: {p['fg_dim']};
}}
.summary-stat-label {{
    color: {p['fg_dim']};
    font-size: 90%;
}}
.summary-list {{
    background-color: {p['bg_raised']};
    border-radius: {r_lg};
}}
.summary-row-good, .summary-row-bad {{
    font-weight: 600;
}}
.summary-row-bad {{
    color: {p['fg']};
}}
.summary-row-reason {{
    color: {p['fg_dim']};
    font-size: 88%;
}}
.update-subtitle {{
    color: {p['fg_dim']};
    font-weight: 600;
}}
.update-step-pending, .update-step-label-pending {{
    color: {p['fg_dim']};
}}
.update-step-done {{
    color: {accent};
    font-weight: 800;
}}
.update-step-failed {{
    color: #e5534b;
    font-weight: 700;
}}
.update-step-label-active {{
    font-weight: 700;
}}
.update-status {{
    color: {p['fg_dim']};
    font-size: 92%;
}}
.update-status.update-status-error {{
    color: #e5534b;
    font-weight: 700;
}}
progressbar.accent-progress trough {{
    background-color: {p['bg_raised']};
    border: none;
    border-radius: {r_pill};
    min-height: 12px;
}}
progressbar.accent-progress progress {{
    background-image: linear-gradient(to right, {accent}, {p['accent_hover']});
    border: none;
    border-radius: {r_pill};
    min-height: 12px;
    box-shadow: 0 0 8px rgba({accent_rgb}, 0.55);
}}
progressbar.accent-progress.accent-progress-failed progress {{
    background-image: none;
    background-color: #e5534b;
    box-shadow: none;
}}
progressbar.accent-progress text {{
    color: {p['fg']};
    font-weight: 700;
    font-size: 90%;
}}
spinner.accent-spinner {{
    color: {accent};
}}
.update-log {{
    font-size: 85%;
}}
/* Startup scrim. GTK3 has no blur, so this obscures instead: a heavy
   wash in the WINDOW'S OWN background colour, which reads as the window
   not being ready yet rather than as a separate panel on top of it.
   Using the palette's bg rather than a fixed dark also keeps the label
   below readable on the light themes, where a dark scrim under
   dark-on-light text would not be. */
.startup-scrim {{
    background-color: rgba({_hex_to_rgb_str(p['bg'])}, 0.88);
}}
.startup-scrim.startup-scrim-solid {{ background-color: {p['bg']}; }}
.startup-scrim-label {{
    color: {p['fg']};
    font-size: 110%;
    font-weight: 600;
}}
/* Sections are separated by a surface, not by an outline - a visible
   border per launcher stacked up into a lot of nested boxes down the
   page, and a faint fill groups just as clearly with far fewer lines.
   The fill is deliberately TRANSLUCENT: the bubble field drifting
   through behind the covers is the app's own signature, and an opaque
   surface here blanked it out everywhere except the gaps between
   sections. Empty sections use the same value, so a launcher with
   nothing installed doesn't look like a different kind of thing. */
.section-frame, .section-frame-empty {{
    border: none;
    background-color: rgba({accent_rgb}, 0.045);
}}
.section-empty-note {{
    font-size: 95%;
}}
/* The launcher name, now in the header row instead of on the frame's
   border - see RenderingMixin._render_section. */
.section-title {{
    font-weight: 600;
    font-size: 115%;
}}
/* Game titles win over their playtime line, which is the way round it
   should have been: the name is what gets scanned for. */
.card-title {{
    font-weight: 600;
}}
/* Sized up from the old small-and-bold treatment, which came out
   genuinely hard to read. It stays BELOW the title in the hierarchy by
   weight and colour rather than by size - the title is bold and full
   contrast, this is neither. */
.card-playtime {{
    font-size: 112%;
}}
.count-badge {{
    background-color: rgba({accent_rgb}, 0.16);
    color: {p['fg']};
}}
/* The "PlunderHub" title badge (see App._build_title_row) - a small,
   floating, fully-rounded "island" rather than a full-width bar, so it
   reads as a light-touch brand mark instead of eating a whole row's
   worth of vertical space on the home screen. Selector is deliberately
   "button.app-title-pill" (not just ".app-title-pill") and repeats every
   property the generic "button:not(.game-cover)" rule sets (see
   chrome_part below, which is concatenated AFTER this and would
   otherwise win ties) - explicit resets on padding/min-height/min-width
   too, so no leftover default GTK button sizing can inflate this past
   its label's own natural size. */
button.app-title-pill {{
    background-image: none;
    background-color: rgba({accent_rgb}, 0.14);
    border: none;
    box-shadow: none;
    border-radius: {r_pill};
    padding: 3px 16px;
    min-height: 0;
    min-width: 0;
}}
button.app-title-pill:hover {{
    background-image: none;
    background-color: rgba({accent_rgb}, 0.24);
    border: none;
}}
/* The "set up a SteamGridDB API key" nudge (see App._build_cover_hint_bar) -
   a light accent tint, so it reads as one of the slim informational bars. */
/* The notices over the library (no API key, titles off, missing tools,
   ...): a rounded card with some air around it, its close button a
   small round one. */
.cover-hint-bar {{
    background-color: rgba({accent_rgb}, 0.12);
    border: 1px solid rgba({accent_rgb}, 0.35);
    border-radius: 14px;
    margin: 10px 16px 2px 16px;
    padding: 6px 8px 6px 10px;
}}
.cover-hint-bar button.flat {{
    background-color: transparent;
    border-color: transparent;
    border-radius: 999px;
    padding: 6px;
}}
.cover-hint-bar button.flat:hover {{ background-color: rgba({accent_rgb}, 0.18); }}
/* Shown instead of a game's cover/icon when no downloadable art exists
   for it (see App._make_no_cover_placeholder) - a tinted card rather
   than the flat black square this replaced, so it reads as "nothing
   found here" rather than "something's broken". */
.no-cover-placeholder {{
    background-color: {p['bg_raised']};
    border: 1px solid {border};
    border-radius: {r_md};
}}
.no-cover-placeholder-label {{
    color: {p['fg_dim']};
    font-size: 90%;
}}
/* The game's own title, shown on this placeholder instead of the
   generic "no cover found" message specifically when Show game
   titles under cards is off - see _make_no_cover_placeholder's own
   docstring for the reasoning. Sized and weighted a bit more
   prominently than .no-cover-placeholder-label above (this is meant
   to actually be read at a glance, not just acknowledged as boilerplate
   the way "no cover found" is), while staying the same dim, muted
   color as everything else on this card rather than full foreground
   contrast - "legyen látványos" (make it stand out) meant readable at
   a glance, not glaring against the card's own quiet resting state. */
.no-cover-placeholder-title-label {{
    color: {p['fg_dim']};
    font-size: 105%;
    font-weight: 600;
}}
/* Explains a paused ocean-bubble/animated-cover state (see
   App._build_animations_paused_bar). Floats OVER the game grid/cover
   art, not sitting flat in the normal layout - same reasoning as
   .undo-toast just below, and now the same solid, dark pill styling
   for the same reason: it needs to stay readable against ANY cover
   art behind it, not just the app's own background. */
.animations-paused-bar {{
    background-color: {scrim_strong};
    border-radius: {r_lg};
    padding: 8px 16px;
    box-shadow: {shadow_lg};
    color: {scrim_fg};
}}
/* The undo toast (see App._show_undo_toast) - floats OVER the game
   grid/cover art, not sitting flat in the normal layout the way the
   two bars above do, so it needs to stay readable against ANY
   background behind it - a solid, dark pill (like an actual toast
   notification) rather than a light accent tint. */
.undo-toast {{
    background-color: {scrim_strong};
    border-radius: {r_lg};
    padding: 10px 10px 10px 18px;
    box-shadow: {shadow_lg};
}}
.undo-toast label {{
    color: {scrim_fg};
}}
.undo-toast-btn {{
    background-color: {accent};
    color: {scrim_fg};
    border: none;
    border-radius: {r_sm};
    padding: 4px 16px;
    font-weight: 500;
}}
.undo-toast-btn:hover {{
    background-color: rgba({accent_rgb}, 0.8);
}}
/* The "scroll to top" button (see App._on_main_scroll_value_changed) -
   deliberately LOW-CONTRAST, unlike the undo toast right above: this
   one's meant to be noticed only once actually looking for it, not
   compete for attention with the game covers behind it the whole time
   it happens to be showing. A soft, translucent circle rather than the
   undo toast's solid, opaque pill - hovering brightens it a little, as
   the one bit of feedback that it's clickable. */
.scroll-top-btn {{
    background-color: {scrim_bg};
    /* Floats above the grid: shadow only, no outline. */
    border: none;
    border-radius: {r_pill};
    padding: 16px;
    box-shadow: {shadow_md};
    min-width: 32px;
    min-height: 32px;
}}
.scroll-top-btn:hover {{
    background-color: {scrim_bg_hover};
    border-color: {hairline};
}}
/* Reorder mode (see App._enter_reorder_mode). Deliberately NO dashed
   outline and no static drop-target highlight box anymore - the live
   reflow during a drag (see _on_game_drag_motion / _on_list_drag_motion
   / _on_content_drag_motion, which actually MOVE the dragged card/row/
   section to the hovered position rather than just highlighting it) is
   itself the "where would this land" feedback, so a separate highlight
   would be redundant clutter. The only state that needs its own style
   is the item actually being picked up right now - see
   .reorder-dragging. */
.reorder-drag-grip {{
    color: rgba({accent_rgb}, 0.85);
    /* Padding, not just the bare glyph's own tiny rendered size - this
       is now an actual drag_source_set() target in its own right (see
       App._render_list_items), not just a visual "you can drag this"
       hint the way it started out, so it needs a comfortably sized hit
       area to grab, the same as any other draggable/clickable control. */
    padding: 6px 8px;
}}
/* Floating grip glyph in a card's top-left corner (see
   _make_drag_grip_badge) - same near-invisible-at-rest, clearer-on-
   hover treatment as .card-menu-btn (its top-right counterpart), just
   mirrored to the opposite corner. */
.reorder-drag-grip-badge {{
    background-image: none;
    background-color: rgba(0, 0, 0, 0.45);
    border: none;
    box-shadow: none;
    border-radius: {r_pill};
    padding: 2px 8px;
    min-height: 0;
    min-width: 0;
}}
/* The card/row/section actually being dragged right now (toggled on
   "drag-begin", off on "drag-end"/"drag-failed" - see
   _on_reorder_drag_begin / _on_reorder_drag_end) - fading it out is
   what the person asked for instead of a highlight box: it visibly
   reads as "picked up" as it moves through the live reflow above. */
.reorder-dragging {{
    opacity: 0.35;
}}
/* Hamburger-menu button on cards/launcher cards (see
   App._make_card_menu_button, Options -> Appearance -> "Show menu
   button on cards"). Deliberately near-invisible at rest - a small,
   dim icon with no background - and only becomes clearly visible on
   hover, so it doesn't compete visually with the cover art/title for
   attention while still being easy to find once someone's looking. */
.card-menu-btn {{
    background-image: none;
    background-color: transparent;
    border: none;
    box-shadow: none;
    padding: 2px;
    min-height: 0;
    min-width: 0;
    opacity: 0.45;
}}
.card-menu-btn:hover {{
    background-color: rgba({accent_rgb}, 0.18);
    border-radius: {r_sm};
    opacity: 1;
}}
/* "Abandon ship" - stops a launcher's own client process (see
   App._on_stop_launcher_clicked) - same tiny/flat/low-opacity-at-rest
   sizing as .card-menu-btn right next to it (see
   _overlay_card_menu_button's extra_widget), just red on hover instead
   of accent-tinted - the same "this stops something" cue
   .game-running-stop-btn:hover uses, rather than a second, differently-
   colored accent hover that wouldn't read as a distinct, more
   consequential action next to a plain menu button. */
/* "Running" status shown next to a launched game (see App.running_procs,
   _build_running_status_row, _wrap_game_card_overlay) - overlaid on the
   cover in grid view, in the playtime slot when covers are off, or next
   to the title in list view. A solid, fully-opaque accent pill with
   bold text and a drop shadow, deliberately much bolder than
   .app-title-pill (which is a light, subtle accent tint meant to blend
   into the app's OWN background) - this one has to grab attention and
   stay legible against arbitrary, unpredictable cover art or a dark
   theme background alike, so it leans on solid color + shadow for
   contrast rather than transparency. */
.game-running-badge {{
    background-color: {accent};
    color: {on_accent};
    border: none;
    box-shadow: {shadow_sm};
    border-radius: {r_pill};
    padding: 4px 12px;
    min-height: 0;
    min-width: 0;
    font-size: 0.95em;
    font-weight: 600;
}}
.game-running-stop-btn {{
    background-image: none;
    background-color: {accent};
    color: {on_accent};
    border: none;
    box-shadow: {shadow_sm};
    border-radius: {r_pill};
    padding: 8px;
    min-height: 0;
    min-width: 0;
}}
/* Red on hover rather than a deeper accent shade - a deliberate
   "stop/danger" cue (this button ends the running game), and red reads
   as that signal regardless of which accent color the active theme
   uses, the way a plain accent-on-accent hover wouldn't. */
.game-running-stop-btn:hover {{
    background-image: none;
    background-color: #e04343;
}}
/* Darkens a running game so it visibly stands out from the rest of the
   library at a glance - either as an overlay layered on top of its
   cover image (_build_running_dimmer, when a cover is shown), or
   applied directly as this class on the card/row button itself (when
   there's no cover image to layer over - see _render_grid_items /
   _render_list_items). 60% black, i.e. whatever's underneath shows
   through at 40% of its normal brightness. */
.game-running-dimmer {{
    background-image: none;
    background-color: rgba(0, 0, 0, 0.6);
}}
/* "Support me on Ko-fi" button in the About section (see
   App._build_about_content) - Ko-fi's own brand color (a coral red,
   #FF5E5B - the same one on ko-fi.com and its own official badges),
   not the app's theme accent, so it's still recognizable as a Ko-fi
   button no matter which theme someone's using, the same way an
   official Ko-fi badge would look the same anywhere it's embedded. */
/* "View on GitHub" button in the About section (see
   App._build_about_content) - GitHub's own brand color (near-black,
   #24292F - the color GitHub uses for its own "Sign in with GitHub"/
   repository buttons site-wide), not the app's theme accent, for the
   exact same reason .kofi-btn right below uses Ko-fi's coral instead
   of the theme accent too: instantly recognizable as "the GitHub
   button" no matter which of this app's own themes someone's using. */
.github-btn {{
    background-color: #24292F;
    color: #ffffff;
    border: none;
    box-shadow: {shadow_sm};
    border-radius: {r_sm};
    padding: 8px 20px;
    font-weight: 500;
}}
.github-btn:hover {{
    background-color: #3a3f47;
}}
.kofi-btn {{
    background-color: #FF5E5B;
    color: #ffffff;
    border: none;
    box-shadow: {shadow_sm};
    border-radius: {r_sm};
    padding: 8px 20px;
    font-weight: 500;
}}
.kofi-btn:hover {{
    background-color: #ff7573;
}}
/* "License: GNU GPL v3.0" button in the About section - same rounded-
   pill SHAPE as .kofi-btn right above, for visual consistency between
   the two, but deliberately kept quiet (a thin outline, no solid fill)
   rather than also going loud/branded like Ko-fi's own button - see
   the comment above about_license_btn for why muted is the point here. */
.about-license-btn {{
    background-image: none;
    background-color: transparent;
    border: 1px solid {border};
    border-radius: {r_sm};
    padding: 4px 16px;
}}
.about-license-btn:hover {{
    background-color: rgba({accent_rgb}, 0.10);
}}
/* The "Browse SteamGridDB" cover picker (see App._show_cover_selector) -
   picking a thumbnail applies it immediately but no longer closes this
   dialog (see the note on _on_cover_selected for why), so this ring is
   the only feedback that anything happened - and which cover is
   CURRENTLY applied, if the person tries a few before settling on one. */
/* Picker thumbnails. Three states have to stay distinguishable at a
   glance: what you could click, what is applied RIGHT NOW, and what you
   just picked. Only the last of those existed before - the grid gave no
   hover feedback at all, so nothing indicated the thumbnails were even
   clickable, and reopening the picker showed no sign of which cover was
   already in use. */
.cover-picker-thumb {{
    border: 3px solid transparent;
    border-radius: {r_md};
    padding: 3px;
    background-image: none;
    background-color: transparent;
    transition: background-color 120ms ease-out;
}}
.cover-picker-thumb:hover {{
    background-color: rgba({accent_rgb}, 0.14);
    border-color: rgba({accent_rgb}, 0.45);
}}
/* Keyboard/controller focus, deliberately NOT the same as selection -
   moving focus across the grid with a pad shouldn't look like you have
   already applied each cover you passed over. */
.cover-picker-thumb:focus {{
    border-color: rgba({accent_rgb}, 0.75);
    border-style: dashed;
}}
/* The cover already in use when the picker opened. */
.cover-picker-current {{
    border: 3px solid rgba({accent_rgb}, 0.55);
    border-radius: {r_md};
}}
/* The one just clicked - the strongest of the three. */
.cover-picker-selected {{
    border: 3px solid {accent};
    border-radius: {r_md};
    background-color: rgba({accent_rgb}, 0.10);
}}
/* Source resolution under each thumbnail: quiet, but there when you're
   choosing between two similar covers. */
.picker-caption {{
    color: {p['fg_dim']};
    font-size: 90%;
}}
/* Corner marker on animated entries, sitting on top of the artwork. */
.picker-badge {{
    background-color: rgba({accent_rgb}, 0.92);
    color: {on_accent};
    border-radius: {r_sm};
    padding: 1px 6px;
    font-size: 80%;
    font-weight: 600;
}}
/* The segmented filter row that replaced the notebook tabs. */
.picker-filter-bar {{
    padding: 0;
}}
.picker-filter-btn {{
    border-radius: {r_sm};
    padding: 4px 12px;
    background-image: none;
    background-color: {p['bg_alt']};
    border: 1px solid {border};
}}
.picker-filter-btn:hover {{
    background-color: rgba({accent_rgb}, 0.16);
}}
/* The active filter is marked with a tint and an accent outline, NOT by
   filling it with the accent color: that repainted the label in
   on_accent, which on several palettes is a near-black chosen to sit on
   a saturated fill and reads as muddy at this size. Keeping the normal
   foreground color leaves the label exactly as legible as the inactive
   ones next to it. */
.picker-filter-btn:checked {{
    background-color: rgba({accent_rgb}, 0.22);
    border-color: {accent};
    font-weight: 600;
}}
.picker-filter-btn:checked:hover {{
    background-color: rgba({accent_rgb}, 0.30);
}}
.picker-filter-btn:disabled {{
    opacity: 0.45;
}}
/* Console Mode (see App._build_console_mode_view) - a big-picture,
   controller-first alternate view. Its toggle button in the header is
   twice as WIDE as the plain icon buttons next to it (Dashboard/Tools/
   Options, which render around 32px wide with a typical GTK3 theme) -
   min-width (not min-height) is what makes ONLY the width bigger,
   leaving the button's height to size itself naturally, exactly
   matching its siblings. */
.console-mode-toggle-btn {{
    min-width: 64px;
}}
.console-mode-toggle-btn.active {{
    background-color: rgba({accent_rgb}, 0.35);
    color: {accent};
}}
/* Filter tabs, the "A -> Z" shortcut, and Exit - plain pill buttons big
   enough to comfortably hit with a mouse from a couch-viewing distance,
   or navigate to directly via controller shoulder buttons (see
   _console_shift_filter) without needing pixel-precise aim at all.
   Padding is in em (not px) deliberately - see _apply_console_ui_scale -
   so the "Console Mode UI size" option can grow/shrink the WHOLE
   button (not just its text) just by scaling the inherited font-size,
   without needing any of these buttons rebuilt. */
.console-pill-btn {{
    border-radius: {r_pill};
    padding: 0.4em 1.2em;
    font-size: 1.05em;
    outline: none;
}}
.console-pill-btn:checked {{
    background-color: rgba({accent_rgb}, 0.35);
    color: {accent};
    font-weight: 600;
}}
/* Now that these are keyboard/controller-navigable (see
   _console_move_focus), they need a clear focus indicator of their
   own - same idea as .game-cover:focus below, just pill-shaped to
   match these buttons specifically. */
.console-pill-btn:focus {{
    background-color: rgba({accent_rgb}, 0.25);
}}
.console-exit-btn {{
    color: #e04343;
}}
.console-title {{
    font-size: 1.8em;
    font-weight: 600;
    opacity: 0.9;
}}
/* Badges naming the physical controller button that triggers some
   nearby action (see App._console_controller_glyph, the JS_BTN_*
   constants above for the actual mapping being labeled) - styled to
   read as an actual button cap rather than a flat color chip: an
   opaque dark face (never blends into whatever's behind it, cover art
   included) with just the RING drawn in the active theme's accent
   color, the way a real pad's face buttons are a solid cap with a
   colored ring/letter. Round by default, matching the round face
   buttons (A/B/X/Y) - .controller-glyph-shoulder below reshapes it
   into the flatter bar LB/RB actually are on real hardware instead.
   The exact width/height (a perfect circle, or a squat wide bar) AND
   the font-size are both set directly in Python, per widget, rather
   than here (see _console_controller_glyph/_console_size_glyph) - both
   need to grow together, in lockstep, with the Console Mode UI size
   setting for these to actually look bigger rather than just a bigger
   empty ring around the same tiny letter, and a single shared
   screen-wide CSS rule can't express "recompute this pixel value from
   that Python attribute". Purely a discoverability hint for controller
   users, no effect on input handling itself. The topbar ones (Sort/
   Exit/filter LB+RB) are built once and updated in place when the
   scale changes (see _console_topbar_glyphs); .controller-glyph-card
   is the one floated on top of a game's cover art instead (see
   _build_console_card), rebuilt fresh - already at the right size -
   every time the card grid itself is, and stays invisible until CSS
   says otherwise below, so it never needs adding/removing by hand as
   focus moves between cards - it just rides along on every card and
   only the FOCUSED one's copy ever becomes visible. */
.controller-glyph {{
    background-color: {glyph_bg};
    color: {glyph_fg};
    border: 2px solid {accent};
    border-radius: {r_pill};
    padding: 0;
    font-weight: 600;
}}
/* LB/RB aren't round buttons on real hardware - a short, wide bar
   instead (see the set_size_request call in _console_controller_glyph
   for the actual squashed-flat proportions), capsule-rounded on its
   two short ends rather than the small rounded-rectangle corners a
   fixed border-radius would give a taller shape. */
.controller-glyph-shoulder {{
    padding: 0 4px;
}}
/* Bigger than the topbar badges - this is the one that has to read at
   a glance, across the room, on top of arbitrary cover art. */
.controller-glyph-card {{
    opacity: 0;
    border-width: 3px;
}}
/* Lit up only on the card that actually has focus right now - same
   flowboxchild:focus hook the highlight ring below uses too, so both
   always agree on which one card is "current". */
flowbox.console-flow flowboxchild:focus .controller-glyph-card {{
    opacity: 1;
}}
/* The highlight ring around whichever card currently has KEYBOARD
   FOCUS (not just "selected" - see below for why that distinction
   matters) - scoped to flowbox.console-flow specifically (rather than
   a bare "flowboxchild" selector) so this doesn't also affect the
   normal grid view's own FlowBox, which uses the same underlying
   widget type.

   Gtk.SelectionMode.BROWSE (see _build_console_mode_view) requires
   EXACTLY one child selected AT ALL TIMES once there's at least one -
   so a card stays "selected" even after focus moves away to the top
   bar (filter tabs/Sort/Exit), since GTK has to keep selecting
   SOMETHING. Styling on ":selected" would therefore leave the ring
   visibly stuck on that last-focused card the whole time you're up in
   the top bar, which looks like two things are highlighted at once.
   ":focus" specifically tracks actual keyboard/gamepad focus instead,
   so the ring correctly disappears the moment focus genuinely leaves
   that card - not the same as "the game grid isn't showing what's
   still selected", it's the CARD's own true focus state, exactly right
   for a heads-up display. */
flowbox.console-flow flowboxchild {{
    padding: 8px;
    border-radius: {r_lg};
    border: 3px solid transparent;
}}
flowbox.console-flow flowboxchild:focus {{
    background-color: rgba({accent_rgb}, 0.15);
    border: 3px solid {accent};
}}
/* .console-card is the inner vbox _build_console_card wraps a cover +
   title (+ playtime) in - it existed as a class with no matching rule
   at all until an earlier pass gave it a translucent background fill
   as a "quiet resting-state presence", separate from the accent-
   colored focus ring. That turned out to be exactly what read as an
   unwanted permanent background/frame around every card, all the
   time (reported directly, more than once) - the normal grid view's
   own cards (see rendering_mixin.py's _render_grid_items) have NO
   such background at all behind their own title/playtime text, just
   the cover floating directly on the page background, so Console
   Mode's cards should match that exactly rather than each carrying
   their own tinted box. Background removed entirely; border-radius
   is kept (harmless with no fill, and still matches the cover's own
   pixel-level rounding - _COVER_CORNER_RADIUS in covers_mixin.py -
   in case anything ever clips against this box's edge again) and
   padding stays 0 for the same reason as before: this class sits on
   the vbox wrapping the cover itself, and any padding here would
   still visibly offset the cover from the flowboxchild's own focus
   ring. */
.console-card {{
    border-radius: {r_md};
    padding: 0;
}}
/* Keyboard/gamepad focus indicator for the NORMAL (non-Console Mode)
   grid and list views (see App._on_window_key_press's cross-section
   arrow-key handling, and the "Escape/arrows control the app" request
   generally) - deliberately added back on top of the blanket outline-
   stripping in _load_css() (.game-cover / flowboxchild there kill the
   default SYSTEM focus rectangle specifically because it looked
   inconsistent with the custom theme, not to hide focus entirely).
   Without this, arrow-key navigation between cards would work but be
   completely invisible - you could move focus, but never see where you
   currently are. */
.game-cover:focus {{
    background-color: rgba({accent_rgb}, 0.22);
}}
"""
    return accent_part + chrome_part + settings_css_for_palette(p)


FONT_FAMILY = "Inter"

_BUNDLED_FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
_FONTS_REGISTERED = False


def register_bundled_fonts() -> bool:
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return True

    lib_name = ctypes.util.find_library("fontconfig") or "libfontconfig.so.1"
    try:
        fontconfig = ctypes.CDLL(lib_name)
        fontconfig.FcConfigAppFontAddFile.restype = ctypes.c_int
        fontconfig.FcConfigAppFontAddFile.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    except (OSError, AttributeError) as e:
        logger.warning(f"Couldn't set up libfontconfig - bundled {FONT_FAMILY} font unavailable, "
                        f"falling back to the system's default UI font: {e}")
        return False

    if not _BUNDLED_FONTS_DIR.is_dir():
        logger.warning(f"Bundled fonts directory not found: {_BUNDLED_FONTS_DIR}")
        return False

    registered_any = False
    for font_path in sorted(_BUNDLED_FONTS_DIR.glob("*.ttf")):
        try:
            ok = fontconfig.FcConfigAppFontAddFile(None, str(font_path).encode("utf-8"))
        except Exception as e:
            logger.debug(f"Failed to register bundled font {font_path.name}: {e}")
            continue
        if ok:
            registered_any = True
        else:
            logger.debug(f"fontconfig rejected bundled font {font_path.name}")

    if not registered_any:
        logger.warning(f"None of the bundled {FONT_FAMILY} font files could be registered - "
                        f"falling back to the system's default UI font.")
    else:
        # Only latched on actual success - only caller (app.py's own
        # __init__) calls this once per run anyway, so this guard
        # isn't currently exercised twice in practice, but setting it
        # early (before any registration was attempted) meant a
        # failed first attempt would make every later call falsely
        # report success without ever having registered anything.
        _FONTS_REGISTERED = True
    return registered_any
