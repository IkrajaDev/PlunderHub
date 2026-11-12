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
"""One lock per file PlunderHub reads, changes and writes back that another
of its own threads may change too - a launcher's list (Faugus' games.json,
Heroic's installed.json and GamesConfig, Steam's localconfig.vdf). Two at
once (a bulk uninstall's worker and an install finishing, a launch option
saved meanwhile) read the same old file and the second write dropped the
first one's change. Within one process: the launchers themselves are closed
for these writes (see the callers).

    with held(path):
        data = read(path); change(data); write(path, data)
"""
import os
import threading
from contextlib import contextmanager
from typing import Dict

_guard = threading.Lock()
_locks: Dict[str, threading.RLock] = {}


def _lock_for(path) -> threading.RLock:
    key = os.path.realpath(os.fspath(path))
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


@contextmanager
def held(path):
    """The file's lock for a read-change-write (re-entrant: a caller may hold it)."""
    lock = _lock_for(path)
    with lock:
        yield
