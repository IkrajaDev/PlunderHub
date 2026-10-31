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


import logging
import sys
import threading
from logging.handlers import RotatingFileHandler

from plunderhub.constants import CONFIG_DIR, LOG_FILE

LOG_MAX_BYTES = 1 * 1024 * 1024
LOG_BACKUP_COUNT = 3
# written once the log files from before the privacy filter are cleaned
_SCRUBBED_MARK = CONFIG_DIR / ".log-private"


class PrivateFormatter(logging.Formatter):
    """Every line - tracebacks too - goes through privacy.scrub before it
    reaches the file: no user name, home folder, account id or key."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        try:
            from plunderhub.privacy import scrub
            return scrub(text)
        except Exception:
            # never write a line the filter couldn't clean
            return f"{self.formatTime(record)} [{record.levelname}] (line left out: privacy filter failed)"


def _scrub_old_logs() -> None:
    """Logs written before the filter existed are cleaned once."""
    if _SCRUBBED_MARK.exists():
        return
    from plunderhub.privacy import scrub
    for i in range(LOG_BACKUP_COUNT + 1):
        path = LOG_FILE if i == 0 else LOG_FILE.with_name(f"{LOG_FILE.name}.{i}")
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                path.write_text(scrub(text), encoding="utf-8")
        except OSError:
            return
    try:
        _SCRUBBED_MARK.write_text("1\n", encoding="utf-8")
    except OSError:
        pass


def _setup_logging() -> logging.Logger:
    log = logging.getLogger("plunderhub")
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        log.setLevel(logging.INFO)
        if not log.handlers:
            try:
                _scrub_old_logs()
            except Exception:
                pass
            handler = RotatingFileHandler(
                str(LOG_FILE),
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(PrivateFormatter("%(asctime)s [%(levelname)s] %(message)s"))
            log.addHandler(handler)
    except Exception:
        if not log.handlers:
            log.addHandler(logging.NullHandler())
    return log


def _log_unhandled(exc_type, exc_value, exc_traceback, thread_name=None):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    where = f" in thread {thread_name}" if thread_name else ""
    logger.error(f"Unhandled {exc_type.__name__}{where}: {exc_value}",
                 exc_info=(exc_type, exc_value, exc_traceback))
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


def install_exception_logging() -> None:
    sys.excepthook = _log_unhandled

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        _log_unhandled(args.exc_type, args.exc_value, args.exc_traceback,
                       getattr(args.thread, "name", None))
    threading.excepthook = thread_hook

    try:
        from gi.repository import GLib

        def glib_hook(exc_type, exc_value, exc_traceback):
            _log_unhandled(exc_type, exc_value, exc_traceback, "GTK callback")

        GLib.install_custom_exception_hook(glib_hook)
    except Exception as e:
        logger.debug(f"Couldn't hook GTK callback exceptions: {e}")


logger = _setup_logging()
