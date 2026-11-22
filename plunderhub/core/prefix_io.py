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

"""How fast a Wine prefix's programs write to disk - the Loading Dock's
speed for an install that happens inside a prefix (a store launcher's
installer and its own update, a game a store launcher downloads): what
they download lands on disk, so it's the download speed near enough. Read
from /proc/<pid>/io (write_bytes) of the processes whose WINEPREFIX is
the prefix (or its pfx) - the same on every distro, no root needed for
one's own processes."""

import os
import time
from typing import Dict, Optional


class PrefixWriteRate:
    def __init__(self, prefix: str):
        root = os.path.realpath(prefix)
        self._roots = {root, os.path.join(root, "pfx")}
        self._ours: Dict[int, bool] = {}           # pid -> runs in the prefix (env read once)
        self._last: Dict[int, int] = {}            # pid -> write_bytes last time
        self._at: Optional[float] = None
        self.total = 0                             # bytes written since the first poll

    def _in_prefix(self, pid: int) -> bool:
        known = self._ours.get(pid)
        if known is not None:
            return known
        ours = False
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                for item in f.read().split(b"\0"):
                    if item.startswith(b"WINEPREFIX="):
                        value = item[len(b"WINEPREFIX="):].decode("utf-8", "replace").rstrip("/")
                        ours = os.path.realpath(value) in self._roots
                        break
        except OSError:
            pass
        self._ours[pid] = ours
        return ours

    def poll(self) -> float:
        """Bytes a second since the last poll (0 the first time)."""
        now = time.monotonic()
        seen: Dict[int, int] = {}
        try:
            pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return 0.0
        grown = 0
        for pid in pids:
            if not self._in_prefix(pid):
                continue
            try:
                with open(f"/proc/{pid}/io", "rb") as f:
                    line = next((l for l in f.read().split(b"\n") if l.startswith(b"write_bytes:")), b"")
                written = int(line.split(b":")[1]) if line else 0
            except (OSError, ValueError):
                continue
            seen[pid] = written
            grown += max(0, written - self._last.get(pid, written))
        self._ours = {pid: v for pid, v in self._ours.items() if pid in pids}
        self._last = seen
        first = self._at is None
        elapsed = now - self._at if self._at else 0.0
        self._at = now
        if first or elapsed <= 0:
            return 0.0
        self.total += grown
        return grown / elapsed
