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

"""Stores the SteamGridDB API key in the OS keyring (via libsecret)
instead of as plain text in settings.json.

Every function here is guarded and NEVER raises - libsecret's typelib
might genuinely be missing on an install that hasn't picked up that
packaging dependency yet, or there might be no keyring daemon running
at all (some minimal window managers, some containers). In either
case, every function here just returns None/False, and the caller
falls back to plain settings.json storage - a key that's readable in
a config file beats a key that silently vanished."""

import time
from typing import Optional

from plunderhub.logging_setup import logger

_SCHEMA_NAME = "com.plunderhub.SteamGridDB"
_ATTRIBUTE_NAME = "account"
_ATTRIBUTE_VALUE = "steamgriddb_api_key"
_LABEL = "PlunderHub - SteamGridDB API key"

_Secret = None
_schema = None
_import_attempted = False


def _get_secret_module():
    """Lazily imports gi.repository.Secret exactly once per process -
    only tried again is pointless since a missing typelib won't appear
    mid-run, and this keeps every call after the first cheap."""
    global _Secret, _schema, _import_attempted
    if _import_attempted:
        return _Secret
    _import_attempted = True
    try:
        import gi
        gi.require_version("Secret", "1")
        from gi.repository import Secret
        schema = Secret.Schema.new(
            _SCHEMA_NAME,
            Secret.SchemaFlags.NONE,
            {_ATTRIBUTE_NAME: Secret.SchemaAttributeType.STRING},
        )
        _Secret = Secret
        _schema = schema
    except Exception as e:
        logger.info(f"libsecret isn't available - the API key will live in settings.json instead: {e}")
        _Secret = None
    return _Secret


def is_available() -> bool:
    return _get_secret_module() is not None


def store_api_key(value: str) -> bool:
    """True if the key actually ended up in the OS keyring. An empty
    value CLEARS the stored key rather than storing an empty string,
    matching what "the user emptied the field and saved" should mean."""
    Secret = _get_secret_module()
    if Secret is None:
        return False
    if not value:
        return clear_api_key()
    try:
        return bool(Secret.password_store_sync(
            _schema, {_ATTRIBUTE_NAME: _ATTRIBUTE_VALUE},
            Secret.COLLECTION_DEFAULT, _LABEL, value, None,
        ))
    except Exception as e:
        logger.warning(f"Couldn't store the API key in the OS keyring: {e}")
        return False


def load_api_key() -> Optional[str]:
    """None if there's nothing stored, or the keyring genuinely can't
    be reached at all. Retried a few times with a short pause first,
    though, rather than giving up on the very first failure - this is
    the fix for a real, reported bug: right after a reboot/login, the
    keyring daemon (accessed over D-Bus) sometimes isn't fully up yet
    if PlunderHub starts fast (e.g. as an autostart app), so the very
    first lookup can transiently fail with a connection error even
    though a key IS stored and the keyring would happily answer a
    second later. That distinction matters a lot here specifically:
    _load_api_key_with_migration treats a failed lookup exactly like
    "no key was ever stored" and, since the one-time migration already
    wiped the settings.json fallback copy on a past successful run,
    a single transient failure at boot made an already-saved key look
    completely gone until the user re-entered it - this is what the
    user was actually hitting, not any actual data loss in the
    keyring itself. Retrying a few times here, before the caller ever
    sees a None, closes that window without adding a real delay in
    the overwhelmingly common case where the first attempt succeeds."""
    Secret = _get_secret_module()
    if Secret is None:
        return None
    global _first_lookup_done
    # only the first lookup after start waits out the keyring's boot race:
    # without a keyring service every later one would sleep on the GTK thread
    attempts = 1 if _first_lookup_done else 3
    _first_lookup_done = True
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return Secret.password_lookup_sync(_schema, {_ATTRIBUTE_NAME: _ATTRIBUTE_VALUE}, None)
        except Exception as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(0.4)
    logger.warning(f"Couldn't read the API key from the OS keyring after {attempts} attempts: {last_error}")
    return None


_first_lookup_done = False


def clear_api_key() -> bool:
    """True if the keyring no longer holds a key afterwards (including
    when there was nothing to clear). False if the keyring couldn't be
    reached - the caller must not then assume the old key is gone, or
    it would quietly come back on the next start."""
    Secret = _get_secret_module()
    if Secret is None:
        return False
    try:
        Secret.password_clear_sync(_schema, {_ATTRIBUTE_NAME: _ATTRIBUTE_VALUE}, None)
        return True
    except Exception as e:
        logger.warning(f"Couldn't clear the API key from the OS keyring: {e}")
        return False
