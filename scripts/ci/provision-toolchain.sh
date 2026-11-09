#!/usr/bin/env bash
# Cache only upstream archives; verify them again and rebuild on every run.
set -euo pipefail
cd "$(dirname "$0")/../.."
test "$(uname -sm)" = 'Linux x86_64'
# Leave room for the distribution extraction and at least 1 GiB of host reserve.
available_kib=$(df -Pk . | awk 'NR == 2 {print $4}')
if (( available_kib < 6 * 1024 * 1024 )); then
  printf '%s\n' 'Toolchain provisioning requires 6 GiB free disk space.' >&2
  exit 1
fi
mkdir -p .tmp/ci-downloads .tmp/ci-tools bend-spec/.cache

download() {
  local url="$1" name="$2" digest="$3"
  local archive="$PWD/.tmp/ci-downloads/$name"
  if [[ ! -f "$archive" ]]; then
    curl --fail --location --retry 3 --connect-timeout 30 --max-time 900 \
      "$url" --output "$archive.part"
    printf '%s  %s\n' "$digest" "$archive.part" | sha256sum --check --strict
    mv "$archive.part" "$archive"
  fi
  printf '%s  %s\n' "$digest" "$archive" | sha256sum --check --strict
  tar -xf "$archive" -C .tmp/ci-tools
}

# Digests from the official Lean release asset API and Node SHASUMS256.txt.
download https://github.com/leanprover/lean4/releases/download/v4.34.0/lean-4.34.0-linux.tar.zst \
  lean-4.34.0-linux.tar.zst caaa98356098c85dc0fcbbd28e1ec66f39eb6551829972b752ff20e1286b646b
download https://nodejs.org/download/release/v22.23.4/node-v22.23.4-linux-x64.tar.xz \
  node-v22.23.4-linux-x64.tar.xz 1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce

python3 bend-spec/scripts/setup-toolchain.py \
  --lean-home "$PWD/.tmp/ci-tools/lean-4.34.0-linux" \
  --node "$PWD/.tmp/ci-tools/node-v22.23.4-linux-x64/bin/node" \
  --clang /usr/bin/clang
