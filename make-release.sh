#!/usr/bin/env bash
# Build release/live-captions-<version>.tar.gz and its checksum for download.
set -euo pipefail

project="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$project"

version="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)"
name="live-captions-$version"
mkdir -p release
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

mkdir "$staging/$name"
cp -p install.sh uninstall.sh pyproject.toml README.md LICENSE "$staging/$name/"
mkdir "$staging/$name/live_captions"
cp -p live_captions/*.py "$staging/$name/live_captions/"

# Fixed owner and order, so the same sources give the same archive.
tar -C "$staging" --sort=name --owner=0 --group=0 --numeric-owner \
    --mode='u+rwX,go+rX,go-w' -cf - "$name" | gzip -n -9 >"release/$name.tar.gz"
(cd release && sha256sum "$name.tar.gz" >"$name.tar.gz.sha256")

echo "release/$name.tar.gz"
cat "release/$name.tar.gz.sha256"
