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
"""Giving freed memory back to the system.

Animation frames are a few hundred KB each and freed by the hundred - a
loop let go, a decoder done with. glibc keeps most of that in its heaps
for later instead of returning it, so the app looked a quarter of a GB
bigger than what it held. release_soon() asks it to hand the free pages
back, once, a moment after a batch of frees.

And glibc raises its own mmap threshold each time a big block is freed:
from then on the 1.6 MB frames of a playing hero came out of the heaps,
wedged between the covers' frames, and the app grew by ~90 MB steps for
as long as it played. Kept at 256 KB (configure), every frame-sized
block is mapped on its own and goes straight back when freed.

Where the C library isn't glibc (musl) there's nothing to ask, and
nothing happens.
"""

import ctypes
import ctypes.util
from typing import Optional

from plunderhub.gi_setup import GLib
from plunderhub.logging_setup import logger

M_MMAP_THRESHOLD = -3
MMAP_THRESHOLD = 256 << 10

_libc = None
_pending: Optional[int] = None


def _glibc():
    global _libc
    if _libc is None:
        _libc = False
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
            libc.malloc_trim.argtypes = [ctypes.c_size_t]
            libc.mallopt.argtypes = [ctypes.c_int, ctypes.c_int]
            _libc = libc
        except (OSError, AttributeError) as e:
            logger.debug(f"Not glibc - freed memory stays with the C library: {e}")
    return _libc or None


def configure():
    """Frame-sized blocks mapped on their own, from the start (see above)."""
    libc = _glibc()
    if libc is not None:
        libc.mallopt(M_MMAP_THRESHOLD, MMAP_THRESHOLD)


def _run() -> bool:
    global _pending
    _pending = None
    libc = _glibc()
    if libc is not None:
        libc.malloc_trim(0)
    return False


def release_soon(delay_ms: int = 1500):
    """Gives freed memory back to the system shortly - one call for a whole
    batch of frees."""
    global _pending
    if _pending is None:
        _pending = GLib.timeout_add(delay_ms, _run)
