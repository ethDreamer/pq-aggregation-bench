#!/bin/sh
set -eu
architecture=$1
benchmark_revision=$2
profile_list=/tmp/pq-sidecar-profiles
: > "$profile_list"

build_sidecar() {
    profile=$1
    binary=$2
    flags=$3
    (
        cd leanvm
        CARGO_TARGET_DIR="/target/$profile" RUSTFLAGS="$flags" \
            cargo build --target "$target" --locked --release -p leanvm-sidecar --features experimental_prover
    )
    install -D "/target/$profile/$target/release/leanvm-sidecar" "/out/bin/$binary"
    printf '%s\t%s\t%s\n' "$profile" "$binary" "$flags" >> "$profile_list"
}

# Ascending priority. The runner chooses from the kernel's usable CPU flags
# after the image starts, so these binaries never depend on the builder's CPU.
# An explicit Cargo target keeps ISA flags out of host build scripts and proc macros.
case "$architecture" in
    arm64)
        target=aarch64-unknown-linux-gnu
        export JEMALLOC_SYS_WITH_LG_PAGE=16
        generator_flags="-C target-cpu=generic -C target-feature=+aes"
        build_sidecar portable leanvm-sidecar "$generator_flags"
        ;;
    amd64)
        target=x86_64-unknown-linux-gnu
        generator_flags="-C target-cpu=x86-64"
        baseline="$generator_flags -C target-feature=+ssse3,+pclmulqdq"
        avx2="$baseline,+avx2,+bmi2"
        avx2_gfni="$avx2,+gfni,+vpclmulqdq"
        avx512="$avx2_gfni,+avx512f,+avx512bw,+avx512vbmi"
        build_sidecar generic leanvm-sidecar-generic "$generator_flags"
        build_sidecar portable leanvm-sidecar "$baseline"
        build_sidecar avx2 leanvm-sidecar-avx2 "$avx2"
        build_sidecar avx2-gfni leanvm-sidecar-avx2-gfni "$avx2_gfni"
        build_sidecar avx512 leanvm-sidecar-avx512 "$avx512"
        ;;
    *)
        echo "Unsupported architecture: $architecture" >&2
        exit 1
        ;;
esac

# The signer and verifier come from the same exact checkout as the sidecar.
cp leanvm/crates/lean_xmss/src/wots.rs generator/src/upstream_wots.rs
cp leanvm/Cargo.lock generator/Cargo.lock
(
    cd generator
    export RUSTFLAGS="$generator_flags"
    export CARGO_TARGET_DIR=/target/fixtures
    cargo check
    cargo build --locked --release
)
install -D /target/fixtures/release/pq-geometry-fixtures /out/bin/pq-geometry-fixtures

mkdir -p /out/licenses
cp leanvm/LICENSE-MIT leanvm/LICENSE-APACHE /out/licenses/
python3 - "$architecture" "$benchmark_revision" "$profile_list" "$generator_flags" "$target" <<'PY' > /out/build.json
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tomllib

def command(*args):
    return subprocess.check_output(args, text=True).strip()

def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

architecture, benchmark_revision, profile_list, generator_flags, target = sys.argv[1:]
linux_x86_aliases = {"sse3": "pni", "sse4.1": "sse4_1", "sse4.2": "sse4_2"}
sidecars = []
for item in Path(profile_list).read_text().splitlines():
    name, binary, flags = item.split("\t")
    cfg = command("rustc", *shlex.split(flags), "--target", target, "--print", "cfg")
    features = sorted(line.split('"')[1] for line in cfg.splitlines() if line.startswith("target_feature="))
    if architecture == "amd64" and name != "generic":
        required = sorted(linux_x86_aliases.get(feature, feature) for feature in features)
    else:
        required = []
    sidecars.append({
        "profile": name,
        "binary": binary,
        "rustflags": flags,
        "target_features": features,
        "required_linux_cpu_flags": required,
        "sidecar_sha256": sha256(Path("/out/bin") / binary),
    })

bindings = Path("leanvm/bindings/Cargo.toml")
bindings_version = (
    tomllib.loads(bindings.read_text())["workspace"]["package"]["version"] if bindings.is_file() else None
)
print(json.dumps({
    "leanvm_revision": command("git", "-C", "leanvm", "rev-parse", "HEAD"),
    "leanvm_bindings_version": bindings_version,
    "benchmark_revision": benchmark_revision,
    "target_platform": "linux/" + architecture,
    "rustc": command("rustc", "--version"),
    "fixture_generator_rustflags": generator_flags,
    "jemalloc_log2_max_page_size": os.environ.get("JEMALLOC_SYS_WITH_LG_PAGE"),
    "sidecar_cargo_features": ["experimental_prover"],
    "sidecar_wire_version": 1,
    "sidecar_verifier": "leanvm-v1",
    "sidecar_variants": sidecars,
    "sidecar_cargo_lock_sha256": sha256("leanvm/Cargo.lock"),
    "fixture_cargo_lock_sha256": sha256("generator/Cargo.lock"),
    "fixture_wots_sha256": sha256("generator/src/upstream_wots.rs"),
    "fixture_generator_sha256": sha256("/out/bin/pq-geometry-fixtures"),
}, indent=2))
PY
