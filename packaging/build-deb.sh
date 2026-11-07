#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUTPUT_DIR=${1:-"$ROOT/dist"}
PACKAGE_DIR=$(mktemp -d)
trap 'rm -rf "$PACKAGE_DIR"' EXIT HUP INT TERM

install -d "$OUTPUT_DIR"
install -d "$PACKAGE_DIR/DEBIAN"
install -d "$PACKAGE_DIR/usr/bin"
install -d "$PACKAGE_DIR/usr/lib/python3/dist-packages/health_check"
install -d "$PACKAGE_DIR/usr/share/doc/health-check-desktop"
install -d "$PACKAGE_DIR/usr/share/applications"
install -d "$PACKAGE_DIR/usr/share/icons/hicolor/scalable/apps"
install -d "$PACKAGE_DIR/usr/share/metainfo"
install -d "$PACKAGE_DIR/usr/share/locale"

install -m 0644 "$ROOT/packaging/control" "$PACKAGE_DIR/DEBIAN/control"
install -m 0644 "$ROOT/LICENSE" "$PACKAGE_DIR/usr/share/doc/health-check-desktop/LICENSE"
install -m 0644 "$ROOT/README.md" "$PACKAGE_DIR/usr/share/doc/health-check-desktop/README.md"
install -m 0755 "$ROOT/packaging/health-check-desktop" \
    "$PACKAGE_DIR/usr/bin/health-check-desktop"
install -m 0644 "$ROOT/src/health_check/"*.py \
    "$PACKAGE_DIR/usr/lib/python3/dist-packages/health_check/"
install -m 0644 "$ROOT/io.github.dariocostanzo.DesktopCheckup.desktop" \
    "$PACKAGE_DIR/usr/share/applications/io.github.dariocostanzo.DesktopCheckup.desktop"
install -m 0644 "$ROOT/data/io.github.dariocostanzo.DesktopCheckup.svg" \
    "$PACKAGE_DIR/usr/share/icons/hicolor/scalable/apps/io.github.dariocostanzo.DesktopCheckup.svg"
install -m 0644 "$ROOT/io.github.dariocostanzo.DesktopCheckup.metainfo.xml" \
    "$PACKAGE_DIR/usr/share/metainfo/io.github.dariocostanzo.DesktopCheckup.metainfo.xml"
if [ -d "$ROOT/locale" ]; then
    cp -a "$ROOT/locale/." "$PACKAGE_DIR/usr/share/locale/"
fi

dpkg-deb --build --root-owner-group "$PACKAGE_DIR" \
    "$OUTPUT_DIR/health-check-desktop_0.1.0-1_all.deb"
printf 'Built %s\n' "$OUTPUT_DIR/health-check-desktop_0.1.0-1_all.deb"
