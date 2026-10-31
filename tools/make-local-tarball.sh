#!/usr/bin/env bash
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
# Packs this working tree into the tarball makepkg expects, so an Arch
# package can be built from a version that isn't tagged on GitHub yet:
#
#     tools/make-local-tarball.sh && makepkg -si
#
# The PKGBUILD picks up a local tarball in preference to downloading the
# release, so nothing in it needs editing either way. Delete the tarball
# to go back to building the published version.

set -euo pipefail

# the source tree: this script lives in its tools/ folder
cd "$(dirname "$(readlink -f "$0")")/.."

# Read the version from the app itself rather than the PKGBUILD, so a
# mismatch between the two is caught here instead of producing a package
# whose contents don't match its version number.
app_version=$(sed -n 's/^APP_VERSION = "\(.*\)"$/\1/p' plunderhub/constants.py)
if [[ -z "$app_version" ]]; then
    echo "error: couldn't read APP_VERSION from plunderhub/constants.py" >&2
    exit 1
fi

pkgver=$(sed -n 's/^pkgver=//p' PKGBUILD)
tag=$(sed -n 's/^_tag="\(.*\)"$/\1/p' PKGBUILD)

# pacman versions can't contain "-", so the PKGBUILD spells it "_".
expected_pkgver=${app_version//-/_}
if [[ "$pkgver" != "$expected_pkgver" ]]; then
    echo "error: PKGBUILD pkgver is '$pkgver' but the app reports '$app_version'" >&2
    echo "       (expected pkgver=$expected_pkgver)" >&2
    exit 1
fi
if [[ "$tag" != "v$app_version" ]]; then
    echo "error: PKGBUILD _tag is '$tag' but the app reports '$app_version'" >&2
    exit 1
fi

# Must match _srcdir_name in the PKGBUILD, which mirrors how GitHub names
# the directory inside a release archive.
dir_name="PlunderHub-${app_version}"
tarball="plunderhub-${pkgver}.tar.gz"

staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
mkdir "$staging/$dir_name"

# Everything the PKGBUILD installs, plus the files worth having in a
# source archive. Build artefacts, caches and any existing tarball are
# left out.
tar --create --file - \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    --exclude='*.tar.gz' \
    --exclude='*.pkg.tar.*' \
    --exclude='pkg' \
    --exclude='src' \
    . | tar --extract --file - --directory "$staging/$dir_name"

tar --create --gzip --file "$tarball" --directory "$staging" "$dir_name"

echo "wrote $tarball  ($(du -h "$tarball" | cut -f1), version $app_version)"
echo "now run: makepkg -si"
