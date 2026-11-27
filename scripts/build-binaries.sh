#!/bin/sh
set -eu
architecture=$1
benchmark_revision=$2
if [ ! -f leanvm/crates/lean_vm/src/in_process.rs ]; then
    exec ./build-legacy-binaries.sh "$@"
fi
case "$architecture" in
    arm64) target=aarch64-unknown-linux-gnu; flags='-C target-cpu=generic' ;;
    amd64) target=x86_64-unknown-linux-gnu; flags='-C target-cpu=x86-64' ;;
    *) echo "Unsupported architecture: $architecture" >&2; exit 1 ;;
esac
export RUSTFLAGS="$flags"
export JEMALLOC_SYS_WITH_MALLOC_CONF="abort_conf:true,narenas:16"
if [ "$architecture" = arm64 ]; then export JEMALLOC_SYS_WITH_LG_PAGE=16; fi
export RUSTC_WRAPPER="$PWD/leanvm/scripts/tier-rustc"
export CARGO_TARGET_DIR=/target/in-process
cp leanvm/Cargo.lock worker/Cargo.lock
cargo build --release --manifest-path worker/Cargo.toml --target "$target"
cargo build --release --locked --manifest-path worker/Cargo.toml --target "$target"
install -D "/target/in-process/$target/release/pq-bench-worker" /out/bin/pq-bench-worker
leanvm/scripts/check-tiers /out/bin/pq-bench-worker
cp leanvm/crates/lean_xmss/src/wots.rs generator/src/upstream_wots.rs
cp leanvm/Cargo.lock generator/Cargo.lock
cargo check --manifest-path generator/Cargo.toml --target "$target"
cargo build --locked --release --manifest-path generator/Cargo.toml --target "$target"
install -D "/target/in-process/$target/release/pq-geometry-fixtures" /out/bin/pq-geometry-fixtures
mkdir -p /out/licenses
cp leanvm/LICENSE-MIT leanvm/LICENSE-APACHE /out/licenses/
python3 - "$architecture" "$benchmark_revision" "$flags" <<'PY' > /out/build.json
import hashlib,json,subprocess,sys,tomllib
from pathlib import Path
architecture, benchmark_revision, flags = sys.argv[1:]
def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
print(json.dumps({
    'leanvm_revision': subprocess.check_output(['git','-C','leanvm','rev-parse','HEAD'],text=True).strip(),
    'leanvm_bindings_version': tomllib.loads(Path('leanvm/bindings/Cargo.toml').read_text())['workspace']['package']['version'],
    'benchmark_revision': benchmark_revision,
    'target_platform': 'linux/'+architecture,
    'backend': 'leanvm-in-process',
    'allocator': 'tikv-jemallocator0.6.1; abort_conf:true,narenas:16',
    'rustc': subprocess.check_output(['rustc','--version'],text=True).strip(),
    'rustflags': flags, 'fixture_generator_rustflags': flags,
    'sidecar_cargo_features': ['experimental_prover','cpu_tiers'],
    'sidecar_wire_version': 2, 'sidecar_verifier': 'leanvm-v1',
    'sidecar_variants': [{'profile':'auto','binary':'pq-bench-worker','rustflags':flags,'target_features':[],
                         'required_linux_cpu_flags':[],'sidecar_sha256':digest('/out/bin/pq-bench-worker')}],
    'fixture_generator_sha256':digest('/out/bin/pq-geometry-fixtures'),
    'fixture_cargo_lock_sha256':digest('generator/Cargo.lock'),
    'worker_cargo_lock_sha256':digest('worker/Cargo.lock'),
    'fixture_wots_sha256':digest('generator/src/upstream_wots.rs'),
    'tier_wrapper_sha256':digest('leanvm/scripts/tier-rustc'),
},indent=2))
PY
