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

import json
import threading
import math
import os
import time
import zipfile
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.constants import APP_VERSION
from plunderhub.core.persistence import (
    SETTINGS_SCHEMA_VERSION,
    load_hidden, migrate_settings, load_json, save_cover_map, save_favorites, save_first_seen,
    save_game_icon_map, save_hero_map, save_hidden, save_icon_map, save_known_titles,
    save_playtime, save_playtime_history, save_recent, save_settings,
)
from plunderhub.core import launch_options
from plunderhub.gi_setup import GLib, Gtk, Pango
from plunderhub.icons import get_icon_pixbuf
from plunderhub.logging_setup import logger
from plunderhub.ui.progress_card import card_dialog


BACKUP_SCHEMA_VERSION = 1
ART_FOLDERS = ("covers", "icons", "game_icons", "heroes")
# Art is already compressed: stored as is, it zips in a moment.
_STORE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico"}


def _is_finite_number(value) -> bool:
    # JSON allows Infinity/NaN, which int() can't convert - one such value
    # used to abort an import halfway, after earlier parts were already saved.
    return isinstance(value, (int, float)) and math.isfinite(value)


def _art_files(directory: Path):
    """The files directly in an art folder, or none if it doesn't exist
    (Sink the Config removes it while the app keeps running)."""
    try:
        return sorted(f for f in directory.iterdir() if f.is_file())
    except FileNotFoundError:
        return []


def _animation_files(directory: Path):
    """The APNGs kept beside flattened stills (covers_mixin.animation_sidecar)."""
    try:
        return sorted(f for f in (directory / ".animated").iterdir() if f.is_file() and f.suffix == ".apng")
    except (FileNotFoundError, NotADirectoryError):
        return []



def _same_kind(mine, theirs) -> bool:
    """Whether a restored setting can stand in for this one (None either
    way, ints and floats for each other - but never a bool for a number)."""
    if mine is None or theirs is None:
        return True
    if isinstance(mine, bool) or isinstance(theirs, bool):
        return isinstance(mine, bool) and isinstance(theirs, bool)
    if isinstance(mine, (int, float)) and isinstance(theirs, (int, float)):
        return True
    return type(mine) is type(theirs)

class BackupMixin:
    """Backup and restore: one .zip with everything PlunderHub keeps of
    its own - settings, lists, playtime, the art and where it came from.
    Not in it: the SteamGridDB key (a zip anyone can read; the key lives
    in the keyring), the log, and launch options (Steam's and Heroic's
    own files hold those)."""

    # --- what a backup holds ------------------------------------------------------

    def _backup_bundle(self) -> Dict:
        # copies of the art maps, taken under the lock the download threads
        # write them under (iterated live, one could change mid-export)
        with self._cover_map_lock:
            art_maps = {name: json.loads(json.dumps(getattr(self, name)))
                        for name in ("cover_map", "icon_map", "game_icon_map", "hero_map")}
        return {
            # The SteamGridDB API key is left out on purpose: a backup
            # is a plain zip anyone holding it can read, while the key
            # itself lives in the OS keyring. Without the keyring it
            # sits in settings.json - so that copy is dropped too.
            "settings": {k: v for k, v in self.settings.items() if k != "steamgriddb_api_key"},
            "hidden": self.scanner.hidden if self.scanner else load_hidden(),
            "favorites": sorted(self.favorites),
            "recent": self.recent,
            "playtime": self.playtime,
            "playtime_history": self.playtime_history,
            "first_seen": self.first_seen,
            "cover_map": art_maps["cover_map"],
            "icon_map": art_maps["icon_map"],
            "game_icon_map": art_maps["game_icon_map"],
            "hero_map": art_maps["hero_map"],
            "known_titles": self.known_titles,
            # which Steam games run natively / had Proton set for them
            "steam_native": launch_options._load_native_record(),
        }

    def _art_dirs(self) -> Dict[str, Path]:
        return {"covers": self.covers_dir, "icons": self.icons_dir,
                "game_icons": self.game_icons_dir, "heroes": self.heroes_dir}

    @staticmethod
    def _bundle_counts(bundle: Dict, art: int) -> Dict[str, int]:
        hidden = bundle.get("hidden") if isinstance(bundle.get("hidden"), dict) else {}
        playtime = bundle.get("playtime") if isinstance(bundle.get("playtime"), dict) else {}
        return {"favorites": len(bundle.get("favorites") or []),
                "hidden": sum(len(v) for v in hidden.values() if isinstance(v, list)),
                "playtime": sum(1 for v in playtime.values() if _is_finite_number(v) and v > 0),
                "art": art}

    def _backup_when(self, ts: float) -> str:
        """"today 18:51", "yesterday", "4 days ago"."""
        text = self._home_ago_text(ts)
        if int((time.time() - ts) // 86400) == 0:
            text += " " + time.strftime("%H:%M", time.localtime(ts))
        return text

    def _backup_status_text(self) -> str:
        last = self.settings.get("last_backup")
        if isinstance(last, dict) and _is_finite_number(last.get("ts")):
            return self._("backup_last").format(when=self._backup_when(last["ts"]))
        return self._("backup_never")

    @staticmethod
    def _default_backup_dir() -> Path:
        documents = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOCUMENTS)
        return Path(documents) if documents and Path(documents).is_dir() else Path.home()

    @staticmethod
    def _free_backup_name(directory: Path) -> Path:
        base = f"plunderhub-backup-{date.today().isoformat()}"
        target, n = directory / f"{base}.zip", 2
        while target.exists():
            target, n = directory / f"{base}-{n}.zip", n + 1
        return target

    def _contents_list(self, counts: Dict[str, int]) -> Gtk.Box:
        """What's in a backup, a line each with an accent icon."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.get_style_context().add_class("welcome-feature")
        accent = self._home_accent_hex()
        for icon_name, text in (
                ("preferences-system-symbolic", self._("backup_item_settings")),
                ("starred-symbolic", self._("backup_item_lists").format(fav=counts["favorites"], hidden=counts["hidden"])),
                ("clock-symbolic", self._("backup_item_playtime").format(n=counts["playtime"])),
                ("image-x-generic-symbolic", self._("backup_item_art").format(n=counts["art"]))):
            line = Gtk.Box(spacing=12)
            pb = get_icon_pixbuf(icon_name, 18, accent)
            if pb is not None:
                line.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
            label = Gtk.Label(label=text, xalign=0)
            label.set_line_wrap(True)
            line.pack_start(label, True, True, 0)
            box.pack_start(line, False, False, 0)
        return box

    def _card_esc(self, dlg: Gtk.Dialog):
        def on_key(_w, event) -> bool:
            if event.keyval == 0xff1b:     # Escape
                dlg.response(Gtk.ResponseType.CANCEL)
                return True
            return False
        dlg.connect("key-press-event", on_key)
        dlg.plunderhub_nav = lambda action: action == "back" and dlg.response(Gtk.ResponseType.CANCEL)

    # --- export -------------------------------------------------------------------------

    def _write_backup(self, target: Path) -> None:
        bundle = self._backup_bundle()
        art = {folder: _art_files(directory) for folder, directory in self._art_dirs().items()}
        counts = self._bundle_counts(bundle, sum(len(files) for files in art.values()))
        # Written next to the target and only renamed over it once
        # complete: a failure half-way (disk full, art folders gone
        # after Sink the Config) would otherwise leave a truncated zip
        # in place of a previous, working backup.
        tmp_target = target.with_name(f".{target.name}.partial")
        try:
            with zipfile.ZipFile(tmp_target, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("manifest.json", json.dumps({
                    "backup_schema": BACKUP_SCHEMA_VERSION,
                    "app_version": APP_VERSION,
                    "settings_schema": SETTINGS_SCHEMA_VERSION,
                    "created": time.time(),
                    "counts": counts,
                }, indent=2))
                zf.writestr("data.json", json.dumps(bundle, indent=2, ensure_ascii=False))
                for folder, files in art.items():
                    for f in files:
                        stored = f.suffix.lower() in _STORE_SUFFIXES
                        zf.write(f, arcname=f"{folder}/{f.name}",
                                 compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
                # the animations of flattened APNG stills, else a restore keeps them still
                for folder, directory in self._art_dirs().items():
                    for f in _animation_files(directory):
                        zf.write(f, arcname=f"{folder}/.animated/{f.name}", compress_type=zipfile.ZIP_STORED)
            os.replace(tmp_target, target)
        except BaseException:
            tmp_target.unlink(missing_ok=True)
            raise

    def _export_settings(self, parent: Optional[Gtk.Window] = None) -> bool:
        """The backup card: what goes in, where it goes, Back up. True
        once a backup is written."""
        _ = self._
        parent = parent or self._dialog_transient_parent()
        bundle = self._backup_bundle()
        art = sum(len(_art_files(d)) for d in self._art_dirs().values())
        last = self.settings.get("last_backup")
        last_dir = Path(last["path"]).parent if isinstance(last, dict) and isinstance(last.get("path"), str) else None
        place = {"dir": last_dir if last_dir is not None and last_dir.is_dir() else self._default_backup_dir()}

        dlg, column = card_dialog(parent, _("backup_export_title"))
        column.set_spacing(10)
        title = Gtk.Label(label=_("backup_export_title"), xalign=0)
        title.get_style_context().add_class("home-dialog-title")
        column.pack_start(title, False, False, 0)
        head = Gtk.Label(label=_("backup_in_it").upper(), xalign=0)
        head.get_style_context().add_class("settings-group-title")
        head.set_margin_top(6)
        column.pack_start(head, False, False, 0)
        column.pack_start(self._contents_list(self._bundle_counts(bundle, art)), False, False, 0)
        note = Gtk.Label(label=_("backup_key_note"), xalign=0)
        note.set_line_wrap(True)
        note.set_max_width_chars(52)
        note.get_style_context().add_class("dim-label")
        note.get_style_context().add_class("welcome-small")
        column.pack_start(note, False, False, 0)

        head = Gtk.Label(label=_("backup_place").upper(), xalign=0)
        head.get_style_context().add_class("settings-group-title")
        head.set_margin_top(8)
        column.pack_start(head, False, False, 0)
        where = Gtk.Box(spacing=10)
        where.get_style_context().add_class("welcome-feature")
        path_label = Gtk.Label(xalign=0)
        path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        path_label.set_hexpand(True)
        path_label.set_width_chars(1)
        where.pack_start(path_label, True, True, 0)
        change = self._dialog_button(_("backup_change"), "ghost")
        where.pack_end(change, False, False, 0)
        column.pack_start(where, False, False, 0)

        def show_place():
            target = self._free_backup_name(place["dir"])
            place["target"] = target
            path_label.set_text(str(target).replace(str(Path.home()), "~", 1))
            path_label.set_tooltip_text(str(target))
        show_place()

        def pick_folder(*_a):
            chooser = Gtk.FileChooserNative.new(_("backup_place"), dlg, Gtk.FileChooserAction.SELECT_FOLDER,
                                                None, None)
            chooser.set_current_folder(str(place["dir"]))
            if chooser.run() == Gtk.ResponseType.ACCEPT and chooser.get_filename():
                place["dir"] = Path(chooser.get_filename())
                show_place()
            chooser.destroy()
        change.connect("clicked", pick_folder)

        buttons = Gtk.Box(spacing=8)
        buttons.set_halign(Gtk.Align.END)
        buttons.set_margin_top(12)
        cancel = self._dialog_button(_("dialog_cancel"), "ghost", "back", "Esc")
        cancel.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.CANCEL))
        go = self._dialog_button(_("backup_save_btn"), "accent")
        go.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.OK))
        buttons.pack_start(cancel, False, False, 0)
        buttons.pack_start(go, False, False, 0)
        column.pack_start(buttons, False, False, 0)
        self._card_esc(dlg)
        dlg.get_content_area().show_all()
        self._enter_dimmed_dialog()
        try:
            response = dlg.run()
        finally:
            dlg.destroy()
            self._exit_dimmed_dialog()
        if response != Gtk.ResponseType.OK:
            return False

        target = place["target"]
        try:
            self._busy_card(_("backup_busy"), parent, lambda: self._write_backup(target))
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            self._show_info_dialog(f"{_('export_failed')}: {e}", message_type=Gtk.MessageType.ERROR,
                                   transient_for=parent)
            return False
        self.settings["last_backup"] = {"ts": time.time(), "path": str(target)}
        save_settings(self.settings)
        self._refresh_backup_status()
        if self._card_dialog(_("backup_saved_title"), str(target).replace(str(Path.home()), "~", 1),
                             [(_("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost"),
                              (_("backup_show_folder"), Gtk.ResponseType.APPLY, "accent")],
                             tone="success", default=Gtk.ResponseType.CLOSE,
                             transient_for=parent) == Gtk.ResponseType.APPLY:
            self._show_folder(target.parent, transient_for=parent)
        return True

    def _refresh_backup_status(self):
        label = getattr(self, "backup_status_label", None)
        if label is not None:
            try:
                label.set_text(self._backup_status_text())
            except Exception:
                pass

    # --- import -------------------------------------------------------------------------

    def _backup_is_readable(self, zf: zipfile.ZipFile, parent: Optional[Gtk.Window] = None) -> bool:
        version = BACKUP_SCHEMA_VERSION
        if "manifest.json" in zf.namelist():
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                if isinstance(manifest.get("backup_schema"), int):
                    version = manifest["backup_schema"]
            except Exception as e:
                logger.warning(f"Backup manifest.json couldn't be read: {e}")
        if version > BACKUP_SCHEMA_VERSION:
            logger.warning(f"Backup is schema v{version}, newer than v{BACKUP_SCHEMA_VERSION} - refusing.")
            self._show_info_dialog(self._("import_too_new").format(version=version), Gtk.MessageType.WARNING,
                                   transient_for=parent)
            return False
        return True

    def _found_backups(self) -> List[Path]:
        """Backups in the usual places, newest first."""
        places = {self._default_backup_dir(), Path.home()}
        downloads = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if downloads:
            places.add(Path(downloads))
        last = self.settings.get("last_backup")
        if isinstance(last, dict) and isinstance(last.get("path"), str):
            places.add(Path(last["path"]).parent)
        found = set()
        for place in places:
            try:
                found.update(f for f in place.glob("plunderhub-backup*.zip") if f.is_file())
            except OSError:
                pass
        return sorted(found, key=lambda f: f.stat().st_mtime, reverse=True)[:6]

    @staticmethod
    def _peek_backup(path: Path) -> Optional[Tuple[Dict, Dict]]:
        """(manifest, data) of a backup, or None if it isn't one."""
        try:
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as zf:
                    names = zf.namelist()
                    manifest = json.loads(zf.read("manifest.json")) if "manifest.json" in names else {}
                    data = json.loads(zf.read("data.json")) if "data.json" in names else None
                    art = sum(1 for n in names if n.split("/", 1)[0] in ART_FOLDERS and not n.endswith("/"))
                    if isinstance(manifest, dict) and not isinstance(manifest.get("counts"), dict):
                        manifest["art"] = art
            else:
                manifest, data = {}, load_json(path)
        except Exception as e:
            logger.debug(f"Not a readable backup: {path}: {e}")
            return None
        if not isinstance(data, dict):
            return None
        return (manifest if isinstance(manifest, dict) else {}), data

    def _import_settings(self, options_dialog: Gtk.Dialog):
        """The restore card: the backups found nearby (or another file),
        what the picked one holds, and Restore."""
        _ = self._
        dlg, column = card_dialog(options_dialog, _("backup_import_title"))
        column.set_spacing(10)
        title = Gtk.Label(label=_("backup_import_title"), xalign=0)
        title.get_style_context().add_class("home-dialog-title")
        column.pack_start(title, False, False, 0)
        head = Gtk.Label(label=_("backup_found").upper(), xalign=0)
        head.get_style_context().add_class("settings-group-title")
        head.set_margin_top(6)
        column.pack_start(head, False, False, 0)
        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        column.pack_start(listing, False, False, 0)
        details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        column.pack_start(details, False, False, 0)
        warn = Gtk.Label(label=_("backup_restore_warn"), xalign=0)
        warn.set_line_wrap(True)
        warn.set_max_width_chars(52)
        warn.get_style_context().add_class("dim-label")
        warn.get_style_context().add_class("welcome-small")
        column.pack_start(warn, False, False, 0)

        buttons = Gtk.Box(spacing=8)
        buttons.set_margin_top(12)
        browse = self._dialog_button(_("backup_browse"), "ghost")
        buttons.pack_start(browse, False, False, 0)
        go = self._dialog_button(_("backup_restore_btn"), "accent")
        go.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.OK))
        cancel = self._dialog_button(_("dialog_cancel"), "ghost", "back", "Esc")
        cancel.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.CANCEL))
        buttons.pack_end(go, False, False, 0)
        buttons.pack_end(cancel, False, False, 0)
        column.pack_start(buttons, False, False, 0)

        state: Dict = {"path": None, "tiles": []}

        def show_details(path: Optional[Path]):
            for child in details.get_children():
                child.destroy()
            peek = self._peek_backup(path) if path is not None else None
            state["path"] = path if peek is not None else None
            go.set_sensitive(peek is not None)
            if peek is None:
                return
            manifest, data = peek
            counts = manifest.get("counts") if isinstance(manifest.get("counts"), dict) else \
                self._bundle_counts(data, int(manifest.get("art", 0)))
            created = manifest.get("created")
            if not _is_finite_number(created):
                created = path.stat().st_mtime
            made = Gtk.Label(label=_("backup_made").format(when=self._backup_when(created),
                                                           version=manifest.get("app_version", "?")), xalign=0)
            made.get_style_context().add_class("dim-label")
            made.set_margin_top(6)
            details.pack_start(made, False, False, 0)
            details.pack_start(self._contents_list({k: int(counts.get(k, 0)) for k in
                                                    ("favorites", "hidden", "playtime", "art")}), False, False, 0)
            details.show_all()

        def tile_for(path: Path) -> Gtk.ToggleButton:
            tile = Gtk.ToggleButton()
            tile.get_style_context().add_class("install-choice")
            box = Gtk.Box(spacing=12)
            pb = get_icon_pixbuf("drive-multidisk-symbolic", 22, self._home_accent_hex())
            if pb is not None:
                box.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            name = Gtk.Label(label=path.name, xalign=0)
            name.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            name.get_style_context().add_class("home-update-title")
            texts.pack_start(name, False, False, 0)
            where = str(path.parent).replace(str(Path.home()), "~", 1)
            size = path.stat().st_size / (1 << 20)
            sub = Gtk.Label(label=f"{self._backup_when(path.stat().st_mtime)}  ·  {size:.1f} MB  ·  {where}", xalign=0)
            sub.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            sub.get_style_context().add_class("dim-label")
            sub.get_style_context().add_class("welcome-small")
            texts.pack_start(sub, False, False, 0)
            box.pack_start(texts, True, True, 0)
            tile.add(box)

            def toggled(button):
                if state.get("switching"):
                    return                      # the others let go below
                if button.get_active():
                    state["switching"] = True
                    try:
                        for other in state["tiles"]:
                            if other is not button and other.get_active():
                                other.set_active(False)
                    finally:
                        state["switching"] = False
                    show_details(path)
                elif state["path"] == path:
                    button.set_active(True)     # one stays picked
            tile.connect("toggled", toggled)
            state["tiles"].append(tile)
            return tile

        found = self._found_backups()
        for path in found:
            listing.pack_start(tile_for(path), False, False, 0)
        if not found:
            none = Gtk.Label(label=_("backup_none_found"), xalign=0)
            none.get_style_context().add_class("dim-label")
            listing.pack_start(none, False, False, 0)

        def pick_file(*_a):
            chooser = Gtk.FileChooserNative.new(_("backup_import_title"), dlg, Gtk.FileChooserAction.OPEN, None, None)
            bundle_filter = Gtk.FileFilter()
            bundle_filter.set_name("PlunderHub (.zip, .json)")
            bundle_filter.add_pattern("*.zip")
            bundle_filter.add_pattern("*.json")
            chooser.add_filter(bundle_filter)
            chooser.set_current_folder(str(self._default_backup_dir()))
            if chooser.run() == Gtk.ResponseType.ACCEPT and chooser.get_filename():
                path = Path(chooser.get_filename())
                for child in listing.get_children():
                    if isinstance(child, Gtk.Label):
                        child.destroy()
                tile = tile_for(path)
                listing.pack_start(tile, False, False, 0)
                listing.reorder_child(tile, 0)
                tile.show_all()
                tile.set_active(True)
            chooser.destroy()
        browse.connect("clicked", pick_file)

        go.set_sensitive(False)
        self._card_esc(dlg)
        dlg.get_content_area().show_all()
        if state["tiles"]:
            state["tiles"][0].set_active(True)
        response = dlg.run()
        src = state["path"]
        dlg.destroy()
        if response != Gtk.ResponseType.OK or src is None:
            return
        if self._restore_backup(src, options_dialog):
            self._card_dialog(_("backup_restored_title"), _("import_done"),
                              [(_("dialog_ok"), Gtk.ResponseType.CLOSE, "accent")], tone="success",
                              transient_for=options_dialog)
            # reopen Settings so its widgets pick up the restored values
            options_dialog.response(Gtk.ResponseType.CLOSE)
            GLib.idle_add(self.show_options, "system")

    def _restore_cached_file(self, dest_dir: Path, filename: str, data: bytes,
                             listing: Optional[Dict[Path, Dict[str, List[Path]]]] = None) -> None:
        if not filename or filename.startswith(".") or "/" in filename or "\\" in filename:
            logger.warning(f"Skipping backup entry with an unusable file name: {filename!r}")
            return
        dest_dir.mkdir(parents=True, exist_ok=True)
        # Clear any stale file with the same key but a different extension.
        # Compared by name rather than glob(): a backup entry whose name
        # holds * ? or [ ] would otherwise match - and delete - other files.
        # The folder is listed once per restore (stem -> files), not once
        # per entry: that was quadratic in the number of covers.
        listing = {} if listing is None else listing
        if dest_dir not in listing:
            stems: Dict[str, List[Path]] = {}
            for f in _art_files(dest_dir):
                stems.setdefault(f.stem, []).append(f)
            listing[dest_dir] = stems
        key = Path(filename).stem
        for stale in listing[dest_dir].pop(key, []):
            stale.unlink(missing_ok=True)
        target = dest_dir / filename
        target.write_bytes(data)
        listing[dest_dir][key] = [target]

    def _busy_card(self, heading: str, parent: Optional[Gtk.Window], work):
        """work() on a worker thread behind a card with a running bar: a
        backup of hundreds of MB of art froze the window. Its result, or
        its exception raised here."""
        result: Dict[str, object] = {}
        bar = Gtk.ProgressBar()
        bar.set_pulse_step(0.04)
        bar.get_style_context().add_class("accent-progress")
        pulse_id = GLib.timeout_add(30, lambda: (bar.pulse(), True)[1])

        def run():
            try:
                result["value"] = work()
            except BaseException as e:      # handed to the GTK thread as it is
                result["error"] = e

            def done():
                dialog = bar.get_toplevel()
                if isinstance(dialog, Gtk.Dialog):
                    dialog.response(Gtk.ResponseType.OK)
                return False
            GLib.idle_add(done)
        thread = threading.Thread(target=run, daemon=True, name="plunderhub-backup")
        thread.start()
        self._card_dialog(heading, self._("backup_busy_sub"), [], icon_name="clock-symbolic",
                          transient_for=parent, extra=bar, other_responses=(Gtk.ResponseType.OK,))
        GLib.source_remove(pulse_id)
        thread.join()       # closed with Esc: the files are still being written
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def _restore_backup(self, src: Path, parent: Gtk.Window) -> bool:
        data = None
        try:
            if zipfile.is_zipfile(src):
                with zipfile.ZipFile(src, "r") as zf:
                    if not self._backup_is_readable(zf, parent):
                        return False
                dirs = self._art_dirs()
                data = self._busy_card(self._("restore_busy"), parent, lambda: self._unpack_backup(src, dirs))
            else:
                data = load_json(src)
        except Exception as e:
            logger.error(f"Settings import failed (reading file): {e}")
            self._show_info_dialog(f"{self._('import_failed')}: {e}", message_type=Gtk.MessageType.ERROR,
                                   transient_for=parent)
            return False
        if not isinstance(data, dict):
            self._show_info_dialog(self._("import_failed"), message_type=Gtk.MessageType.ERROR, transient_for=parent)
            return False
        return self._apply_backup_data(data, parent)

    def _unpack_backup(self, src: Path, dirs: Dict[str, Path]):
        """The backup's data.json, and its art written into place (on a
        worker: nothing of GTK here)."""
        data = None
        with zipfile.ZipFile(src, "r") as zf:
            if "data.json" in zf.namelist():
                data = json.loads(zf.read("data.json").decode("utf-8"))
            # Art is only restored once the backup's data has
            # proven usable - otherwise a broken backup would
            # still overwrite every cover before the import
            # reported failure, leaving a half-restored state.
            if isinstance(data, dict):
                listing: Dict[Path, Dict[str, List[Path]]] = {}
                animations = []
                for name in zf.namelist():
                    parts = name.split("/")
                    if name.endswith("/") or parts[0] not in dirs:
                        continue
                    if len(parts) == 3 and parts[1] == ".animated":
                        animations.append((parts[0], parts[2], name))
                    elif len(parts) == 2:
                        self._restore_cached_file(dirs[parts[0]], parts[1], zf.read(name), listing)
                # after the stills: an animation older than its still isn't used
                for folder, filename, name in animations:
                    if filename.endswith(".apng") and not filename.startswith(".") \
                            and "\\" not in filename:
                        (dirs[folder] / ".animated").mkdir(parents=True, exist_ok=True)
                        (dirs[folder] / ".animated" / filename).write_bytes(zf.read(name))
        return data

    def _apply_backup_data(self, data: Dict, parent: Gtk.Window) -> bool:
        try:
            # A backup's plain-text key (older builds, or one saved while
            # the keyring was down) goes through _save_api_key like the
            # "api_key" entry below, never straight into self.settings:
            # there, even an empty value means "the user cleared the key"
            # and would wipe a working key from the keyring.
            legacy_key = ""
            if isinstance(data.get("settings"), dict):
                # their own schema's migration first (save_settings stamps
                # the current one, so the reload would never run it)
                imported_settings = migrate_settings(dict(data["settings"]))
                imported_settings.pop("schema", None)
                legacy_key = imported_settings.pop("steamgriddb_api_key", "")
                # when this machine last made a backup stays this machine's
                imported_settings.pop("last_backup", None)
                # a value of another kind than this machine's (hand-edited,
                # an odd old build) would stop the app from starting - left out
                for key in list(imported_settings):
                    if key in self.settings and not _same_kind(self.settings[key], imported_settings[key]):
                        logger.warning(f"Backup setting {key!r} skipped: {imported_settings[key]!r} "
                                       f"isn't a {type(self.settings[key]).__name__}")
                        del imported_settings[key]
                self.settings.update(imported_settings)
                save_settings(self.settings)
            if isinstance(data.get("hidden"), dict):
                save_hidden(data["hidden"])
            if isinstance(data.get("favorites"), list):
                self.favorites = set(str(u) for u in data["favorites"])
                save_favorites(self.favorites)
            if isinstance(data.get("recent"), dict):
                self.recent = {str(k): float(v) for k, v in data["recent"].items()
                               if _is_finite_number(v)}
                save_recent(self.recent)
            if isinstance(data.get("playtime"), dict):
                self.playtime = {str(k): int(v) for k, v in data["playtime"].items()
                                 if _is_finite_number(v)}
                save_playtime(self.playtime)
            if isinstance(data.get("playtime_history"), dict):
                history: Dict[str, Dict[str, int]] = {}
                for date_str, day in data["playtime_history"].items():
                    if not isinstance(day, dict):
                        continue
                    day_out = {str(uid): int(minutes) for uid, minutes in day.items()
                               if _is_finite_number(minutes)}
                    if day_out:
                        history[str(date_str)] = day_out
                self.playtime_history = history
                save_playtime_history(self.playtime_history)
            if isinstance(data.get("first_seen"), dict):
                self.first_seen = {str(k): float(v) for k, v in data["first_seen"].items()
                                   if _is_finite_number(v)}
                save_first_seen(self.first_seen)
            for key, attr, save in (("cover_map", "cover_map", save_cover_map),
                                    ("icon_map", "icon_map", save_icon_map),
                                    ("game_icon_map", "game_icon_map", save_game_icon_map),
                                    ("hero_map", "hero_map", save_hero_map),
                                    ("known_titles", "known_titles", save_known_titles)):
                if isinstance(data.get(key), dict):
                    value = {str(k): v for k, v in data[key].items() if isinstance(v, dict)}
                    setattr(self, attr, value)
                    save(value)
            native = data.get("steam_native")
            if isinstance(native, dict):
                launch_options._save_native_record(
                    {k: [str(a) for a in native.get(k, []) if str(a).isdigit()] for k in ("native", "defaulted")})
            # Only backups from older builds carry a key - new ones leave
            # it out, and then the current key simply stays.
            restored_key = data.get("api_key") or legacy_key
            if isinstance(restored_key, str) and restored_key:
                self._save_api_key(restored_key)
                save_settings(self.settings)
            had_tray = self.use_tray
            self._reload_settings_from_disk()
            # Run on startup / the tray as the backup has them: Settings
            # only acts on a switch that changes, and these now match it
            from plunderhub.core.autostart import disable_autostart, enable_autostart, is_autostart_enabled
            if self.run_on_startup != is_autostart_enabled():
                if self.run_on_startup:
                    enable_autostart()
                else:
                    disable_autostart()
            if self.use_tray != had_tray:
                if self.use_tray:
                    self._setup_tray_icon()
                else:
                    self._teardown_tray_icon()
            return True
        except Exception as e:
            logger.error(f"Settings import failed: {e}")
            self._show_info_dialog(f"{self._('import_failed')}: {e}", message_type=Gtk.MessageType.ERROR,
                                   transient_for=parent)
            return False
