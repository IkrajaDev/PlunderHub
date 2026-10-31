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


import threading
import time
from contextlib import contextmanager
from typing import Dict

from plunderhub.logging_setup import logger

_enabled = False
_start_time = 0.0

_STALL_THRESHOLD_MS = 120

_WATCHDOG_INTERVAL_MS = 100

_section_totals: Dict[str, float] = {}
_section_counts: Dict[str, int] = {}
_lock = threading.Lock()

_worst_stall_ms = 0.0
_stall_count = 0


def is_enabled() -> bool:
    return _enabled


def enable() -> None:
    global _enabled, _start_time
    if _enabled:
        return
    _enabled = True
    _start_time = time.monotonic()
    from plunderhub.constants import APP_NAME, APP_VERSION
    logger.info("=" * 60)
    logger.info(f"=== DIAGNOSTICS ON - {APP_NAME} {APP_VERSION}")
    logger.info(f"=== main-loop stalls over {_STALL_THRESHOLD_MS}ms will be logged")
    logger.info("=" * 60)

    from gi.repository import GLib

    state = {"last": time.monotonic()}

    def tick():
        global _worst_stall_ms, _stall_count
        now = time.monotonic()
        late_ms = (now - state["last"]) * 1000 - _WATCHDOG_INTERVAL_MS
        state["last"] = now
        if late_ms >= _STALL_THRESHOLD_MS:
            _stall_count += 1
            _worst_stall_ms = max(_worst_stall_ms, late_ms)
            logger.info(f"[stall] main loop blocked {late_ms:7.0f}ms   "
                        f"(t+{now - _start_time:.1f}s)")
        return True

    GLib.timeout_add(_WATCHDOG_INTERVAL_MS, tick)
    GLib.timeout_add_seconds(5, _report)


def _report() -> bool:
    if not _enabled:
        return False
    elapsed = time.monotonic() - _start_time
    with _lock:
        rows = sorted(_section_totals.items(), key=lambda kv: -kv[1])[:8]
        counts = dict(_section_counts)
    logger.info(f"[summary] t+{elapsed:.0f}s  stalls={_stall_count}  "
                f"worst={_worst_stall_ms:.0f}ms")
    for name, total in rows:
        count = counts.get(name, 0)
        average = (total / count * 1000) if count else 0.0
        logger.info(f"[summary]   {name:<34} {total * 1000:9.0f}ms total  "
                    f"{count:6d} calls  {average:7.1f}ms avg")
    return True


@contextmanager
def section(name: str):
    if not _enabled:
        yield
        return
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        with _lock:
            _section_totals[name] = _section_totals.get(name, 0.0) + elapsed
            _section_counts[name] = _section_counts.get(name, 0) + 1
        if elapsed * 1000 >= _STALL_THRESHOLD_MS:
            logger.info(f"[slow]  {name} took {elapsed * 1000:.0f}ms   "
                        f"(t+{time.monotonic() - _start_time:.1f}s)")


def note(message: str) -> None:
    if _enabled:
        logger.info(f"[note]  t+{time.monotonic() - _start_time:6.1f}s  {message}")


def summary_now() -> None:
    if _enabled:
        _report()
