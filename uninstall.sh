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
# Removes the files installed by install.sh.
# (If you installed via PKGBUILD/pacman, use this instead:
#  $AS_ROOT pacman -R plunderhub)

set -e

# plain POSIX sh, as install.sh; sudo, doas, or nothing when run as root
if [ "$(id -u)" -eq 0 ]; then
    AS_ROOT=""
elif command -v sudo > /dev/null 2>&1; then
    AS_ROOT="sudo"
elif command -v doas > /dev/null 2>&1; then
    AS_ROOT="doas"
else
    echo "This script needs root rights, but found neither sudo nor doas. Run it as root instead."
    exit 1
fi

echo "Uninstalling PlunderHub..."

$AS_ROOT rm -f /usr/local/bin/plunderhub
$AS_ROOT rm -rf /usr/local/lib/plunderhub
$AS_ROOT rm -f /usr/local/share/applications/io.github.ikraja.PlunderHub.desktop
# Pre-0.5.0 installs used this plain filename instead - removed here too
# so upgrading past the rename doesn't leave an orphaned duplicate entry.
$AS_ROOT rm -f /usr/local/share/applications/PlunderHub.desktop
$AS_ROOT rm -f /usr/local/share/icons/hicolor/scalable/apps/PlunderHub.svg
$AS_ROOT rm -f /usr/local/share/plunderhub/PlunderHub.svg
# The PNG icon/logo older installs used, in case they were never replaced.
$AS_ROOT rm -f /usr/local/share/icons/hicolor/512x512/apps/PlunderHub.png
$AS_ROOT rm -f /usr/local/share/plunderhub/PlunderHub_1024.png
$AS_ROOT rmdir /usr/local/share/plunderhub 2>/dev/null || true

# "Run on startup" lives in the user's own autostart folder - left
# behind, the session would try to start the removed program at every login.
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/plunderhub.desktop"

if command -v gtk-update-icon-cache > /dev/null 2>&1; then
    $AS_ROOT gtk-update-icon-cache /usr/local/share/icons/hicolor 2>/dev/null || true
fi
if command -v update-desktop-database > /dev/null 2>&1; then
    $AS_ROOT update-desktop-database /usr/local/share/applications 2>/dev/null || true
fi

echo ""
echo "PlunderHub has been uninstalled. Your settings/saves (~/.config/plunderhub)"
echo "were kept - if you'd like to remove those too:"
echo "  rm -rf ~/.config/plunderhub"
echo ""
echo "The Proton versions it downloaded (~/.local/share/plunderhub) were kept"
echo "too - your launchers can keep using them. Not needed? Remove them with:"
echo "  rm -rf ~/.local/share/plunderhub"
