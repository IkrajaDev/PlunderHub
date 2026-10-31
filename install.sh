#!/bin/sh
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
#
# Installer for PlunderHub that does not use pacman.
# If you'd like it to be removable via pacman instead (Arch Linux only),
# use the PKGBUILD instead: makepkg -si
#
# This script:
#   1. Detects your Linux distribution (via /etc/os-release).
#   2. Checks whether the required dependencies (Python 3, PyGObject/gi
#      with GTK 3 bindings, the GTK 3 runtime) are actually available.
#   3. If not, installs them using YOUR distro's own package manager.
#   4. Installs PlunderHub itself.
#
# Supported distro families for automatic dependency installation:
# Arch (pacman), Debian/Ubuntu (apt), Fedora/RHEL (dnf), openSUSE
# (zypper), Alpine (apk), Void (xbps). On anything else, this prints
# what to install manually and lets you confirm before continuing -
# it never guesses at commands for a distro it doesn't recognize.

set -e

# Plain POSIX sh throughout: bash isn't there on every system (Void's and
# Alpine's sh is dash/BusyBox, and bash isn't always installed beside it).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Step 1: detect the distro -------------------------------------------
# /etc/os-release is the standard, portable way to identify a Linux
# distribution - present on essentially every modern distro (it's part
# of systemd's spec, but distros without systemd ship it too, since
# it's become the de facto standard regardless). ID_LIKE is checked
# too, alongside ID itself, so derivatives (EndeavourOS/Manjaro ->
# arch, Pop!_OS/Linux Mint -> debian/ubuntu, Rocky/Alma -> rhel/fedora,
# etc.) are recognized via their upstream family even if their own ID
# isn't listed explicitly below.
PKG_MANAGER="unknown"
DISTRO_NAME="unknown"
if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    DISTRO_NAME="${PRETTY_NAME:-${NAME:-unknown}}"
    ID_FIELDS="${ID:-} ${ID_LIKE:-}"
    case " $ID_FIELDS " in
        *" arch "*)   PKG_MANAGER="pacman" ;;
        *" fedora "*|*" rhel "*) PKG_MANAGER="dnf" ;;
        *" suse "*|*" sles "*)   PKG_MANAGER="zypper" ;;
        *" debian "*|*" ubuntu "*) PKG_MANAGER="apt" ;;
        *" alpine "*) PKG_MANAGER="apk" ;;
        *" void "*)   PKG_MANAGER="xbps" ;;
    esac
fi

echo "Detected distro: $DISTRO_NAME"
if [ "$PKG_MANAGER" != "unknown" ]; then
    echo "Package manager:  $PKG_MANAGER"
fi
echo ""

# --- Running things as root ----------------------------------------------
# sudo where there is one; doas on systems that use it instead (common on
# Alpine and Void); nothing at all when this script already runs as root.
if [ "$(id -u)" -eq 0 ]; then
    AS_ROOT=""
elif command -v sudo > /dev/null 2>&1; then
    AS_ROOT="sudo"
elif command -v doas > /dev/null 2>&1; then
    AS_ROOT="doas"
else
    echo "This script needs root rights to install packages and PlunderHub itself,"
    echo "but found neither sudo nor doas. Run it as root instead."
    exit 1
fi

# --- Step 2: check whether the dependencies are already there ------------
# The real, definitive check isn't "is a specific package installed" -
# package names vary by distro and don't always map 1:1 - it's "can
# Python actually import gi and load GTK 3" (plus PyYAML, needed for
# reading Bottles' own library.yml - see plunderhub/core/scanner.py),
# which is exactly what plunderhub.py itself needs to run. python3
# --version alone wouldn't be enough - Python can easily be present
# without PyGObject/GTK/PyYAML.
deps_missing() {
    if ! command -v python3 > /dev/null 2>&1; then
        return 0
    fi
    if ! python3 -c "import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk, Gdk, Gio" > /dev/null 2>&1; then
        return 0
    fi
    # pycairo AND its link to GTK (python3-gi-cairo on Debian/Ubuntu - Ubuntu
    # 26.04 ships pycairo without it): without the link nothing the app
    # draws itself shows - icons, check boxes.
    if ! python3 -c "import cairo, gi; gi.require_foreign('cairo')" > /dev/null 2>&1; then
        return 0
    fi
    if ! python3 -c "import yaml" > /dev/null 2>&1; then
        return 0
    fi
    # libsecret: secure storage for the SteamGridDB API key.
    if ! python3 -c "import gi; gi.require_version('Secret', '1'); from gi.repository import Secret" > /dev/null 2>&1; then
        return 0
    fi
    return 1
}

# The core: PlunderHub can't run without these (mirrored, per dependency,
# in plunderhub/core/dependency_check.py's PACKAGE_TABLE).
# The extras: what parts of it need - WebP and SVG pictures (SteamGridDB's
# animated covers and banners are WebP; the icons are SVG), libwebp's demux
# library for playing long animated banners frugally, the tray icon,
# pkexec for Shipwright's installs, lspci for the graphics card's name.
# Each extra is tried on its own, so one a distro (version) doesn't have
# is skipped instead of stopping the install; "a|b" means a, or b if
# there's no a. Distros whose GTK loads pictures through glycin (newer
# gdk-pixbuf) get WebP and SVG from that instead of webp-pixbuf-loader.
UPDATE_CMD=""
INSTALL=""
CORE=""
EXTRAS=""
case "$PKG_MANAGER" in
    pacman)
        INSTALL="pacman -S --needed --noconfirm"
        CORE="python python-gobject python-cairo gtk3 python-yaml libsecret"
        EXTRAS="libwebp librsvg webp-pixbuf-loader libayatana-appindicator polkit pciutils"
        ;;
    apt)
        UPDATE_CMD="apt-get update"
        # --no-remove: -y would also let apt take out a conflicting package
        INSTALL="apt-get install -y --no-remove"
        CORE="python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-yaml gir1.2-secret-1"
        EXTRAS="libwebpdemux2 librsvg2-common webp-pixbuf-loader
                gir1.2-ayatanaappindicator3-0.1|gir1.2-appindicator3-0.1 pkexec|policykit-1 pciutils"
        ;;
    dnf)
        INSTALL="dnf install -y"
        CORE="python3 python3-gobject python3-cairo gtk3 python3-pyyaml libsecret"
        EXTRAS="libwebp librsvg2 webp-pixbuf-loader|glycin-loaders libayatana-appindicator-gtk3|libappindicator-gtk3
                polkit pciutils"
        ;;
    zypper)
        INSTALL="zypper --non-interactive install"
        CORE="python3 python3-gobject python3-gobject-cairo python3-cairo gtk3 typelib-1_0-Gtk-3_0
              typelib-1_0-Pango-1_0 typelib-1_0-GdkPixbuf-2_0 python3-PyYAML libsecret-1-0 typelib-1_0-Secret-1"
        EXTRAS="libwebpdemux2 webp-pixbuf-loader|glycin-loaders gdk-pixbuf-loader-rsvg|glycin-loaders
                typelib-1_0-AyatanaAppIndicator3-0_1|typelib-1_0-AppIndicator3-0_1 polkit pkexec pciutils"
        ;;
    apk)
        UPDATE_CMD="apk update"
        INSTALL="apk add"
        CORE="python3 py3-gobject3 py3-cairo gtk+3.0 py3-yaml libsecret"
        EXTRAS="libwebpdemux gdk-pixbuf-loaders webp-pixbuf-loader|glycin-loaders librsvg|glycin-svg
                libayatana-appindicator polkit pciutils"
        ;;
    xbps)
        INSTALL="xbps-install -Sy"
        CORE="python3 python3-gobject python3-cairo gtk+3 python3-yaml libsecret"
        EXTRAS="libwebp librsvg webp-pixbuf-loader libayatana-appindicator polkit pciutils"
        ;;
esac

if deps_missing; then
    echo "Missing dependency: Python 3 with PyGObject (GTK 3), pycairo, PyYAML and/or libsecret."
    if [ -n "$CORE" ]; then
        echo "Installing it with: $INSTALL" $CORE
        echo ""
        if [ -n "$UPDATE_CMD" ]; then
            $AS_ROOT $UPDATE_CMD
        fi
        $AS_ROOT $INSTALL $CORE
        echo ""
        if deps_missing; then
            echo "Dependency installation finished, but Python still can't load"
            echo "every dependency - please check the output above for errors,"
            echo "then re-run this script."
            exit 1
        fi
        echo "Dependencies installed successfully."
    else
        echo ""
        echo "Your distro wasn't recognized automatically, so this script can't"
        echo "install the dependency for you. Please install Python 3 with"
        echo "PyGObject (GTK 3 bindings), pycairo, PyYAML and libsecret using your"
        echo "distro's package manager - look for packages along the lines of"
        echo "'python3-gi'/'python-gobject' plus 'gtk3' plus 'python3-yaml'/"
        echo "'python-yaml' (the exact names vary by distro), then re-run this script."
        echo "For WebP covers and the tray icon, also install a WebP loader for"
        echo "gdk-pixbuf (webp-pixbuf-loader), libwebp and libayatana-appindicator."
        exit 1
    fi
    echo ""
else
    echo "Dependencies OK (Python 3, PyGObject/GTK 3, pycairo, PyYAML and libsecret already available)."
    echo ""
    # the extras' names are only good with fresh package lists
    if [ -n "$UPDATE_CMD" ] && [ -n "$EXTRAS" ]; then
        $AS_ROOT $UPDATE_CMD > /dev/null 2>&1 || true
    fi
fi

# --- Step 2b: the extras, one by one ---------------------------------------
# Already there: nothing to install (and on a read-only system - Bazzite,
# Silverblue, Kinoite - the package manager couldn't install it anyway).
pkg_installed() {
    case "$PKG_MANAGER" in
        pacman) pacman -Q "$1" > /dev/null 2>&1 ;;
        apt)    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "ok installed" ;;
        dnf|zypper) rpm -q --whatprovides "$1" > /dev/null 2>&1 ;;
        apk)    apk info -e "$1" > /dev/null 2>&1 ;;
        xbps)   xbps-query "$1" > /dev/null 2>&1 ;;
        *)      return 1 ;;
    esac
}
READ_ONLY=0
[ -e /run/ostree-booted ] && READ_ONLY=1
if [ -n "$EXTRAS" ]; then
    echo "Installing what PlunderHub's covers, banners, tray icon and Shipwright use..."
    for item in $EXTRAS; do
        done_item=""
        old_ifs=$IFS
        IFS='|'
        for pkg in $item; do
            if pkg_installed "$pkg"; then
                done_item="$pkg"
                break
            fi
        done
        if [ -z "$done_item" ] && [ "$READ_ONLY" = 0 ]; then
            for pkg in $item; do
                IFS=$old_ifs
                if $AS_ROOT $INSTALL "$pkg" > /dev/null 2>&1; then
                    done_item="$pkg"
                    break
                fi
            done
        fi
        IFS=$old_ifs
        if [ -n "$done_item" ]; then
            echo "  ok       $done_item"
        elif [ "$READ_ONLY" = 1 ]; then
            echo "  missing  $(echo "$item" | sed 's/|/ or /g') (read-only system: rpm-ostree install ${item%%|*})"
        else
            echo "  skipped  $(echo "$item" | sed 's/|/ or /g') (not available here)"
        fi
    done
    echo ""
fi

# What the pictures actually need, tried for real: GTK loading a WebP and an
# SVG. Only reported - PlunderHub runs without them, with fewer pictures.
python3 - "$SCRIPT_DIR" <<'PYCHECK' || true
import sys
from pathlib import Path
try:
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
except Exception:
    sys.exit(0)
formats = {f.get_name() for f in GdkPixbuf.Pixbuf.get_formats()}
svg = Path(sys.argv[1]) / "plunderhub" / "assets" / "PlunderHub_ikon.svg"
try:
    GdkPixbuf.Pixbuf.new_from_file_at_size(str(svg), 32, 32)
    svg_ok = True
except Exception:
    svg_ok = False
missing = []
if "webp" not in formats:
    missing.append("WebP pictures (animated covers and banners from SteamGridDB) - install a WebP loader for gdk-pixbuf, e.g. webp-pixbuf-loader")
if not svg_ok:
    missing.append("SVG pictures (icons and launcher logos) - install librsvg's gdk-pixbuf loader")
for line in missing:
    print("Note: GTK can't load " + line)
if missing:
    print("")
PYCHECK

# --- Step 3: install PlunderHub itself ------------------------------------
echo "Installing PlunderHub..."

$AS_ROOT install -Dm755 "$SCRIPT_DIR/plunderhub.py" /usr/local/bin/plunderhub

# The plunderhub/ Python package itself (ui/, core/, launchers/, input/,
# plus its own assets/) - installed to /usr/local/lib/plunderhub/, which
# the launcher script above finds via the sys.path candidates at its own
# top (see plunderhub.py). Removed first so re-running this script as an
# update doesn't leave stale .py files behind from a previous version
# that no longer exist in the current one.
$AS_ROOT rm -rf /usr/local/lib/plunderhub
$AS_ROOT install -d /usr/local/lib/plunderhub
$AS_ROOT cp -r "$SCRIPT_DIR/plunderhub" /usr/local/lib/plunderhub/plunderhub
$AS_ROOT find /usr/local/lib/plunderhub -type d -exec chmod 755 {} \;
$AS_ROOT find /usr/local/lib/plunderhub -type f -exec chmod 644 {} \;

# Pre-0.5.0 installs used a plain "PlunderHub.desktop" filename here -
# removed first so re-running this script as an update doesn't leave a
# stale, duplicate menu entry alongside the new, reverse-DNS-named one.
$AS_ROOT rm -f /usr/local/share/applications/PlunderHub.desktop

# A menu folder that didn't exist before: some desktops (Cinnamon) only
# watch the folders that were there when the session started, so the
# entry shows up after the next login - said at the end.
NEW_MENU_DIR=0
[ -d /usr/local/share/applications ] || NEW_MENU_DIR=1
$AS_ROOT install -Dm644 "$SCRIPT_DIR/PlunderHub.desktop" \
    /usr/local/share/applications/io.github.ikraja.PlunderHub.desktop

# The small app icon (window/taskbar/tray/app-menu) - registered in the
# system icon theme under the name "PlunderHub" (see _app_icon_name()
# in plunderhub/theme.py).
$AS_ROOT install -Dm644 "$SCRIPT_DIR/plunderhub/assets/PlunderHub_ikon.svg" \
    /usr/local/share/icons/hicolor/scalable/apps/PlunderHub.svg
# Older installs registered a PNG icon here - removed so the theme
# doesn't keep serving the old logo alongside the new SVG.
$AS_ROOT rm -f /usr/local/share/icons/hicolor/512x512/apps/PlunderHub.png

# Older installs kept a big logo here; the app only uses PlunderHub_ikon.svg
$AS_ROOT rm -f /usr/local/share/plunderhub/PlunderHub.svg /usr/local/share/plunderhub/PlunderHub_1024.png
$AS_ROOT rmdir /usr/local/share/plunderhub 2>/dev/null || true

if command -v gtk-update-icon-cache > /dev/null 2>&1; then
    $AS_ROOT gtk-update-icon-cache /usr/local/share/icons/hicolor 2>/dev/null || true
fi

if command -v update-desktop-database > /dev/null 2>&1; then
    $AS_ROOT update-desktop-database /usr/local/share/applications 2>/dev/null || true
fi

echo ""
if [ "$NEW_MENU_DIR" = 1 ]; then
    echo "Done! PlunderHub is in your application menu from your next login"
    echo "(log out and back in once). Until then, start it from a terminal"
    echo "with the 'plunderhub' command."
else
    echo "Done! PlunderHub will now appear in your application menu, or you can"
    echo "launch it from a terminal with the 'plunderhub' command."
fi
