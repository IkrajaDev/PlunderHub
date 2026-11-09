#!/usr/bin/env python3
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

"""A Windows program's own icon, read straight out of its .exe - no
icoextract, no Pillow, nothing to install on any distro.

The .exe (PE file) keeps its icons as resources: one RT_GROUP_ICON lists
the sizes, each size is an RT_ICON. The biggest of the first group is
taken (the program's main icon comes first): a PNG one as it is, an old
bitmap one wrapped into a one-image .ico that GdkPixbuf reads."""

import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.logging_setup import logger

RT_ICON, RT_GROUP_ICON = 3, 14
MAX_EXE = 512 * 1024 * 1024          # read at most this much (a game's .exe can be huge)


class _PE:
    def __init__(self, data: bytes):
        self.d = data
        if data[:2] != b"MZ":
            raise ValueError("not an exe")
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe:pe + 4] != b"PE\0\0":
            raise ValueError("no PE header")
        coff = pe + 4
        nsect, = struct.unpack_from("<H", data, coff + 2)
        opt_size, = struct.unpack_from("<H", data, coff + 16)
        opt = coff + 20
        magic, = struct.unpack_from("<H", data, opt)
        dirs = opt + (96 if magic == 0x10B else 112)
        self.res_rva, self.res_size = struct.unpack_from("<II", data, dirs + 2 * 8)
        self.sections: List[Tuple[int, int, int, int]] = []
        sec = opt + opt_size
        for i in range(nsect):
            vsize, va, rsize, raw = struct.unpack_from("<IIII", data, sec + i * 40 + 8)
            self.sections.append((va, max(vsize, rsize), raw, rsize))

    def off(self, rva: int) -> int:
        for va, size, raw, _rsize in self.sections:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError("address outside the file")

    def _dir(self, at: int) -> List[Tuple[int, int, bool]]:
        """A resource directory's entries: (id or -1 for a name, offset, is_subdir)."""
        named, ids = struct.unpack_from("<HH", self.d, at + 12)
        out = []
        for i in range(named + ids):
            name, target = struct.unpack_from("<II", self.d, at + 16 + i * 8)
            out.append((-1 if name & 0x80000000 else name, target & 0x7FFFFFFF, bool(target & 0x80000000)))
        return out

    def resources(self, kind: int) -> Dict[int, bytes]:
        """{id or order: data} of one resource type (the first language of each)."""
        if not self.res_rva:
            return {}
        base = self.off(self.res_rva)
        found: Dict[int, bytes] = {}
        for rid, sub, is_dir in self._dir(base):
            if rid != kind or not is_dir:
                continue
            for n, (name_id, sub2, is_dir2) in enumerate(self._dir(base + sub)):
                key = name_id if name_id >= 0 else -(n + 1)
                at = base + sub2
                if is_dir2:                                  # language level
                    langs = self._dir(at)
                    if not langs:
                        continue
                    at = base + langs[0][1]
                rva, size = struct.unpack_from("<II", self.d, at)
                o = self.off(rva)
                found[key] = self.d[o:o + size]
        return found


def icon_bytes(exe: Path) -> Optional[Tuple[bytes, str]]:
    """The exe's main icon at its biggest size: (data, "png" | "ico"), or
    None when it has none (or isn't a Windows program)."""
    try:
        with open(exe, "rb") as f:
            data = f.read(MAX_EXE)
        pe = _PE(data)
        groups = pe.resources(RT_GROUP_ICON)
        if not groups:
            return None
        icons = pe.resources(RT_ICON)
        # named groups first in the file, then by id: the first one is the main icon
        group = groups[sorted(groups, key=lambda k: (k >= 0, k))[0]]
        _res, _typ, count = struct.unpack_from("<HHH", group, 0)
        best = None
        for i in range(count):
            w, h, colors, _r, planes, bits, size, nid = struct.unpack_from("<BBBBHHIH", group, 6 + i * 14)
            img = icons.get(nid)
            if not img:
                continue
            rank = ((w or 256) * (h or 256), bits)
            if best is None or rank > best[0]:
                best = (rank, (w, h, colors, planes, bits), img)
        if best is None:
            return None
        (w, h, colors, planes, bits), img = best[1], best[2]
        if img[:8] == b"\x89PNG\r\n\x1a\n":
            return img, "png"
        header = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", w, h, colors, 0, planes, bits,
                                                              len(img), 6 + 16)
        return header + img, "ico"
    except (OSError, ValueError, struct.error, IndexError, KeyError) as e:
        logger.debug(f"No icon read from {exe}: {e}")
        return None


def save_icon_png(exe: Path, out: Path, size: int = 256) -> bool:
    """The exe's icon as a size x size PNG at out (what Faugus keeps in its
    icons folder). False when there is none."""
    got = icon_bytes(exe)
    if got is None:
        return False
    data, fmt = got
    try:
        from plunderhub.gi_setup import GdkPixbuf
        loader = GdkPixbuf.PixbufLoader.new_with_type(fmt)
        loader.write(data)
        loader.close()
        pb = loader.get_pixbuf()
        if pb is None:
            return False
        if (pb.get_width(), pb.get_height()) != (size, size):
            pb = pb.scale_simple(size, size, GdkPixbuf.InterpType.HYPER)
        out.parent.mkdir(parents=True, exist_ok=True)
        pb.savev(str(out), "png", [], [])
        return True
    except Exception as e:
        logger.debug(f"Icon of {exe} not saved: {e}")
        return False
