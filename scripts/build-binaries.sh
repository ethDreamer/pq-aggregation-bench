#!/bin/sh
set -eu
architecture=$1
benchmark_revision=$2
case "$architecture" in
    arm64)
        export JEMALLOC_SYS_WITH_LG_PAGE=16
        export RUSTFLAGS="-C target-cpu=generic -C target-feature=+aes"
        ;;
    amd64)
        export RUSTFLAGS="-C target-cpu=x86-64 -C target-feature=+ssse3,+pclmulqdq"
        ;;
    *)
        echo "Unsupported architecture: $architecture" >&2
        exit 1
        ;;
esac

export CARGO_TARGET_DIR=/target
(
    cd leanvm
    cargo build --locked --release -p leanvm-sidecar --features experimental_prover
)
install -D /target/release/leanvm-sidecar /out/bin/leanvm-sidecar

# The signer and verifier come from the same exact checkout as the sidecar.
cp leanvm/crates/lean_xmss/src/wots.rs generator/src/upstream_wots.rs
cp leanvm/Cargo.lock generator/Cargo.lock
(
    cd generator
    cargo check
    cargo build --locked --release
)
install -D /target/release/pq-geometry-fixtures /out/bin/pq-geometry-fixtures

mkdir -p /out/licenses
cp leanvm/LICENSE-MIT leanvm/LICENSE-APACHE /out/licenses/
python3 - "$architecture" "$benchmark_revision" <<'PY' > /out/build.json
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

def command(*args):
    return subprocess.check_output(args, text=True).strip()

def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

print(json.dumps({
    "leanvm_revision": command("git", "-C", "leanvm", "rev-parse", "HEAD"),
    "benchmark_revision": sys.argv[2],
    "target_platform": "linux/" + sys.argv[1],
    "rustc": command("rustc", "--version"),
    "rustflags": os.environ["RUSTFLAGS"],
    "jemalloc_log2_max_page_size": os.environ.get("JEMALLOC_SYS_WITH_LG_PAGE"),
    "sidecar_cargo_features": ["experimental_prover"],
    "sidecar_cargo_lock_sha256": sha256("leanvm/Cargo.lock"),
    "fixture_cargo_lock_sha256": sha256("generator/Cargo.lock"),
    "fixture_wots_sha256": sha256("generator/src/upstream_wots.rs"),
    "sidecar_sha256": sha256("/out/bin/leanvm-sidecar"),
    "fixture_generator_sha256": sha256("/out/bin/pq-geometry-fixtures"),
}, indent=2))
PY
