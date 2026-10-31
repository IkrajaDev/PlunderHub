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


import array
import fcntl
import logging
import os
import select
import struct
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

from plunderhub.gi_setup import GLib
from plunderhub.logging_setup import logger


JS_EVENT_BUTTON = 0x01
JS_EVENT_AXIS = 0x02
JS_EVENT_INIT = 0x80

JS_BTN_ACTIVATE = 0
JS_BTN_BACK = 1
JS_BTN_PREV_TAB = 4
JS_BTN_NEXT_TAB = 5
JS_BTN_SETTINGS = 6
JS_BTN_TOGGLE = 7
JS_BTN_SORT = 3
JS_BTN_FAVORITE = 2

PS_JS_BTN_ACTIVATE = 0
PS_JS_BTN_BACK = 1
PS_JS_BTN_SORT = 2
PS_JS_BTN_FAVORITE = 3
PS_JS_BTN_PREV_TAB = 4
PS_JS_BTN_NEXT_TAB = 5
PS_JS_BTN_SETTINGS = 8
PS_JS_BTN_TOGGLE = 9

SONY_VENDOR_ID = "054c"

# View + Start (Share + Options) held together this long: "game_switch" -
# between the game in front and PlunderHub, works while a game has the
# focus too. Either button alone acts when it's let go, so the pair
# never also opens Settings / toggles Console Mode.
CHORD_HOLD_S = 1.0


def _is_virtual(path: str) -> bool:
    """A pad made in software (uinput) - Steam Input's "virtual gamepad",
    which stands in for the real controller while Steam runs."""
    try:
        return "/devices/virtual/" in os.path.realpath(Path("/sys/class/input") / os.path.basename(path))
    except OSError:
        return False


_LONG_BITS = struct.calcsize("l") * 8


def _not_a_pad(path: str) -> bool:
    """A js node the kernel made for a pointer: a VM's absolute mouse
    ("QEMU USB Tablet"), a drawing tablet, a mouse or keyboard with an
    axis. Its moves came in as stick pushes - the focus stepped after the
    mouse and the page scrolled to it. A pad, stick or wheel has one of
    the joystick / gamepad buttons (BTN_TRIGGER 0x120 .. 0x13f); this has
    none. Unreadable: kept, as before."""
    try:
        words = (Path("/sys/class/input") / os.path.basename(path) / "device" / "capabilities"
                 / "key").read_text().split()
        bits = 0
        for word in words:          # most significant word first, a C long each
            bits = (bits << _LONG_BITS) | int(word, 16)
    except (OSError, ValueError):
        return False
    return not (bits >> 0x120) & ((1 << 0x20) - 1)


_physical_cache: Tuple[float, frozenset] = (-10.0, frozenset())


def _physical_families() -> set:
    """The kind of every real (not virtual) controller plugged in - looked
    up again after 2 s at most: Steam's virtual pad asks at every event."""
    global _physical_cache
    now = time.monotonic()
    if now - _physical_cache[0] < 2.0:
        return set(_physical_cache[1])
    found = set()
    for node in Path("/sys/class/input").glob("js*"):
        if not _is_virtual(str(node)) and not _not_a_pad(str(node)):
            found.add(_detect_controller_family(str(node)))
    _physical_cache = (now, frozenset(found))
    return found


# A press on Steam's virtual pad and on the real controller under it come
# within this of each other: one press, not two (and the real one says
# what kind of controller it is).
SAME_PRESS_S = 0.08


# Linux key codes of the pad buttons (input-event-codes.h). xpad and
# hid-playstation both put X / triangle at BTN_NORTH and Y / square at
# BTN_WEST; the other pads (Switch Pro, 8BitDo in D-input, generic HID)
# keep Select / Start, the bumpers and A / B at their codes, at other numbers.
_BTN_SOUTH, _BTN_EAST, _BTN_NORTH, _BTN_WEST = 0x130, 0x131, 0x133, 0x134
_BTN_TL, _BTN_TR, _BTN_SELECT, _BTN_START = 0x136, 0x137, 0x13a, 0x13b
_JSIOCGBUTTONS = 0x80016a12          # _IOR('j', 0x12, __u8)
_JSIOCGBTNMAP = 0x84006a34           # _IOR('j', 0x34, __u16[KEY_MAX - BTN_MISC + 1])


def _button_numbers(fd: int) -> Optional[Dict[int, int]]:
    """key code -> the joystick API's button number, from the driver's own
    map (None when it can't be read: the usual numbers are used)."""
    try:
        count = array.array("B", [0])
        fcntl.ioctl(fd, _JSIOCGBUTTONS, count)
        codes = array.array("H", [0] * 512)
        fcntl.ioctl(fd, _JSIOCGBTNMAP, codes)
    except OSError:
        return None
    return {code: number for number, code in enumerate(codes[:count[0]])}


def _detect_controller_family(path: str) -> str:
    name = os.path.basename(path)
    try:
        vendor = (Path("/sys/class/input") / name / "device" / "id" / "vendor").read_text().strip().lower()
        if vendor == SONY_VENDOR_ID:
            return "sony"
    except Exception:
        pass
    try:
        device_name = (Path("/sys/class/input") / name / "device" / "name").read_text().strip().lower()
        if any(kw in device_name for kw in ("sony", "playstation", "dualshock", "dualsense")):
            return "sony"
    except Exception:
        pass
    return "xbox"

JS_AXES_DIRECTIONAL = {0: "x", 1: "y", 6: "x", 7: "y"}
JS_AXIS_DEADZONE = 16000
JS_NAV_REPEAT_S = 0.28
JS_POLL_INTERVAL_S = 0.05

JS_AXES_SCROLL = {3: "x", 4: "y"}
# the right stick scrolls by how far it's pushed: "scroll" carries the
# push (-1 up .. 1 down, 0 = let go), sent again only when it changed by
# a step - the app moves the page every frame by it, not in jumps
JS_SCROLL_DEADZONE = 6000
JS_SCROLL_STEP = 0.04


def _scroll_push(x: int, y: int) -> float:
    """How far the right stick is pushed up or down, past its dead zone:
    -1 .. 1, rounded to JS_SCROLL_STEP; 0 while it's held more sideways."""
    if abs(y) <= JS_SCROLL_DEADZONE or abs(x) > abs(y):
        return 0.0
    push = min(1.0, (abs(y) - JS_SCROLL_DEADZONE) / (32767 - JS_SCROLL_DEADZONE))
    push = max(JS_SCROLL_STEP, round(push / JS_SCROLL_STEP) * JS_SCROLL_STEP)
    return push if y > 0 else -push


def _resolve_direction(x: int, y: int) -> Optional[str]:
    if abs(x) > abs(y) and abs(x) > JS_AXIS_DEADZONE:
        return "right" if x > 0 else "left"
    if abs(y) > JS_AXIS_DEADZONE:
        return "down" if y > 0 else "up"
    return None


class JoystickManager:

    def __init__(self, on_event, on_family_change=None):
        self.on_event = on_event
        self.on_family_change = on_family_change
        self._known_devices: set = set()
        # path -> ctime of a device node that couldn't be opened; retried
        # only once the node changes (e.g. udev grants access), instead
        # of spawning a doomed reader thread every 2s scan forever.
        self._unopenable: Dict[str, float] = {}
        self._stop = False
        # the last action and press per device, to tell one press seen by
        # two devices (Steam's virtual pad + the real one) - reader threads
        self._lock = threading.Lock()
        self._last_action: Dict[str, Tuple[float, str]] = {}
        self._last_press: Dict[str, Tuple[float, str]] = {}
        # the kind of each controller being read
        self._families: Dict[str, str] = {}
        self._scan()
        GLib.timeout_add(2000, self._scan)

    def stop(self):
        self._stop = True

    def is_connected(self) -> bool:
        return bool(self._known_devices)

    def pad_family(self) -> Optional[str]:
        """The kind of controller plugged in ("xbox" / "sony" - the one
        pressed last when there are several), None with none plugged in."""
        with self._lock:
            if not self._families:
                return None
            last = max(self._families, key=lambda p: self._last_press.get(p, (0.0, ""))[0])
            return self._families[last]

    def _scan(self) -> bool:
        if self._stop:
            return False
        try:
            devices = sorted(Path("/dev/input").glob("js*"))
        except Exception:
            return True
        for dev in devices:
            path = str(dev)
            if path in self._known_devices:
                continue
            if path in self._unopenable:
                try:
                    if dev.stat().st_ctime == self._unopenable[path]:
                        continue
                except OSError:
                    continue
                del self._unopenable[path]
            self._known_devices.add(path)
            if _not_a_pad(path):
                logger.info(f"{path} is a pointer, not a controller - left alone")
                continue
            threading.Thread(target=self._read_loop, args=(path,), daemon=True).start()
        return True

    def _family_of(self, path: str, family: str, virtual: bool, now: float) -> str:
        """The kind of controller in the hand: for Steam's virtual pad, the
        real one that was pressed at the same moment - else, when every
        real controller plugged in is of one kind, that kind."""
        if not virtual:
            return family
        with self._lock:
            for other, (when, kind) in self._last_press.items():
                if other != path and abs(now - when) <= SAME_PRESS_S:
                    return kind
        kinds = _physical_families()
        return kinds.pop() if len(kinds) == 1 else family

    def _send(self, path: str, action: str, family: str, virtual: bool, value=None):
        """Hands action to the app - once, though Steam's virtual pad and the
        real controller under it both report the press - and which kind of
        controller made it, for the on-screen buttons."""
        now = time.monotonic()
        with self._lock:
            if not virtual:
                self._last_press[path] = (now, family)
            last = self._last_action.get(action)
            self._last_action[action] = (now, path)
        # letting the stick go always counts - a dropped one kept the page scrolling
        if (last is not None and last[1] != path and now - last[0] <= SAME_PRESS_S
                and not (action == "scroll" and value == 0)):
            return
        if self.on_family_change is not None and action != "release" and value != 0:
            GLib.idle_add(self.on_family_change, self._family_of(path, family, virtual, now))
        GLib.idle_add(self.on_event, action, value)

    def _read_loop(self, path: str):
        if os.environ.get("PLUNDERHUB_JS_DEBUG") == "1":
            logger.setLevel(logging.DEBUG)
        family = _detect_controller_family(path)
        virtual = _is_virtual(path)
        if family == "sony":
            btn_activate, btn_back, btn_sort = PS_JS_BTN_ACTIVATE, PS_JS_BTN_BACK, PS_JS_BTN_SORT
            btn_prev_tab, btn_next_tab = PS_JS_BTN_PREV_TAB, PS_JS_BTN_NEXT_TAB
            btn_settings, btn_toggle = PS_JS_BTN_SETTINGS, PS_JS_BTN_TOGGLE
            btn_favorite = PS_JS_BTN_FAVORITE
        else:
            btn_activate, btn_back, btn_sort = JS_BTN_ACTIVATE, JS_BTN_BACK, JS_BTN_SORT
            btn_prev_tab, btn_next_tab = JS_BTN_PREV_TAB, JS_BTN_NEXT_TAB
            btn_settings, btn_toggle = JS_BTN_SETTINGS, JS_BTN_TOGGLE
            btn_favorite = JS_BTN_FAVORITE
        logger.debug(f"[JS_DEBUG] {path}: detected as '{family}'{' (virtual)' if virtual else ''}")
        last_move = 0.0
        last_direction: Optional[str] = None
        axis_state: Dict[int, int] = {}
        scroll_axis_state: Dict[int, int] = {}
        last_push = 0.0
        # View / Start (Share / Options) held down; since when both are,
        # and whether they were held together (then neither acts alone)
        held: set = set()
        chord_since: Optional[float] = None
        chord_used = False
        try:
            try:
                f = open(path, "rb", buffering=0)
            except OSError as e:
                logger.debug(f"[JS_DEBUG] {path}: couldn't open: {e}")
                try:
                    self._unopenable[path] = os.stat(path).st_ctime
                except OSError:
                    pass
                return
            # Only a controller we can actually read may switch the
            # on-screen button glyphs to its family.
            kind = self._family_of(path, family, virtual, 0.0)
            with self._lock:
                self._families[path] = kind
            if self.on_family_change is not None:
                GLib.idle_add(self.on_family_change, kind)
            with f:
                fd = f.fileno()
                numbers = _button_numbers(fd)
                if numbers and _BTN_SOUTH in numbers:
                    # (a joystick without gamepad codes keeps the usual numbers)
                    def at(code, _usual):
                        return numbers.get(code, -1)     # a button it hasn't: none
                    btn_activate, btn_back = at(_BTN_SOUTH, btn_activate), at(_BTN_EAST, btn_back)
                    btn_prev_tab, btn_next_tab = at(_BTN_TL, btn_prev_tab), at(_BTN_TR, btn_next_tab)
                    btn_settings, btn_toggle = at(_BTN_SELECT, btn_settings), at(_BTN_START, btn_toggle)
                    north, west = (btn_sort, btn_favorite) if family == "sony" else (btn_favorite, btn_sort)
                    north, west = at(_BTN_NORTH, north), at(_BTN_WEST, west)
                    btn_sort, btn_favorite = (north, west) if family == "sony" else (west, north)
                    logger.debug(f"[JS_DEBUG] {path}: buttons by code: activate {btn_activate}, back {btn_back}, "
                                 f"tabs {btn_prev_tab}/{btn_next_tab}, settings {btn_settings}, toggle {btn_toggle}, "
                                 f"sort {btn_sort}, favorite {btn_favorite}")
                while not self._stop:
                    # the short beat only while something is held (a repeat, the
                    # View+Start hold); idle, a second still notices a stop
                    busy = last_direction is not None or chord_since is not None
                    ready, _, _ = select.select([fd], [], [], JS_POLL_INTERVAL_S if busy else 1.0)
                    if ready:
                        data = f.read(8)
                        if not data or len(data) < 8:
                            break
                        _time, value, ev_type, number = struct.unpack("IhBB", data)
                        if ev_type & JS_EVENT_INIT:
                            pass
                        elif ev_type & JS_EVENT_BUTTON and number in (btn_settings, btn_toggle):
                            logger.debug(f"[JS_DEBUG] button {number} = {value}")
                            if value != 0:
                                held.add(number)
                                if held >= {btn_settings, btn_toggle}:
                                    chord_since, chord_used = time.monotonic(), True
                            else:
                                held.discard(number)
                                chord_since = None
                                if not chord_used:
                                    self._send(path, "settings" if number == btn_settings else "toggle",
                                               family, virtual)
                                if not held:
                                    chord_used = False
                        elif ev_type & JS_EVENT_BUTTON:
                            logger.debug(f"[JS_DEBUG] button {number} = {value}")
                            if value != 0:
                                action = {
                                    btn_activate: "activate",
                                    btn_back: "back",
                                    btn_prev_tab: "prev_tab",
                                    btn_next_tab: "next_tab",
                                    btn_sort: "sort",
                                    btn_favorite: "favorite",
                                }.get(number)
                                if action:
                                    self._send(path, action, family, virtual)
                        elif ev_type & JS_EVENT_AXIS and number in JS_AXES_DIRECTIONAL:
                            axis_state[number] = value
                        elif ev_type & JS_EVENT_AXIS and number in JS_AXES_SCROLL:
                            scroll_axis_state[number] = value

                    if chord_since is not None and time.monotonic() - chord_since >= CHORD_HOLD_S:
                        chord_since = None
                        self._send(path, "game_switch", family, virtual)

                    stick_dir = _resolve_direction(axis_state.get(0, 0), axis_state.get(1, 0))
                    dpad_dir = _resolve_direction(axis_state.get(6, 0), axis_state.get(7, 0))
                    direction = dpad_dir or stick_dir
                    now = time.monotonic()
                    if direction != last_direction:
                        last_direction = direction
                        if direction:
                            last_move = now
                            self._send(path, direction, family, virtual)
                        else:
                            # lets a held-down accelerating action (e.g. a
                            # spinbutton) know the stick/dpad centered again
                            self._send(path, "release", family, virtual)
                    elif direction and now - last_move >= JS_NAV_REPEAT_S:
                        last_move = now
                        self._send(path, direction, family, virtual)

                    push = _scroll_push(scroll_axis_state.get(3, 0), scroll_axis_state.get(4, 0))
                    if push != last_push:
                        last_push = push
                        self._send(path, "scroll", family, virtual, push)
        except OSError:
            pass
        finally:
            self._known_devices.discard(path)
            self._unplugged(path)

    def _unplugged(self, path: str):
        """A controller gone: the buttons shown become the keys when it was
        the last one - else those of the one left (pressed last first)."""
        with self._lock:
            if self._families.pop(path, None) is None:
                return
            self._last_press.pop(path, None)
            left = sorted(self._families, key=lambda p: self._last_press.get(p, (0.0, ""))[0])
            family = self._families[left[-1]] if left else "keyboard"
        if self.on_family_change is not None:
            GLib.idle_add(self.on_family_change, family)
