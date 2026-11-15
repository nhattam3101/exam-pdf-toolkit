#!/usr/bin/env bash
set -euo pipefail

repo_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
binary="$repo_dir/tools/lightpanda/lightpanda"
url="https://github.com/lightpanda-io/browser/releases/download/nightly/lightpanda-x86_64-linux"

mkdir -p "$(dirname -- "$binary")"
curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
  --output "$binary.tmp" "$url"
chmod 0755 "$binary.tmp"
LIGHTPANDA_DISABLE_TELEMETRY=true "$binary.tmp" version
mv -f "$binary.tmp" "$binary"
LIGHTPANDA_DISABLE_TELEMETRY=true "$binary" version

