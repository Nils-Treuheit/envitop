#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CARGO_NET_OFFLINE=true cargo build --release --manifest-path "$HERE/Cargo.toml"
printf 'Built %s\n' "$HERE/target/release/omnitop-native"
