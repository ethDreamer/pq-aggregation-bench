# PQ aggregation benchmark

Measure leanVM’s experimental rate-2 prover with real XMSS committee proofs, recursive slot aggregation, and two proposer rounds that extend an existing aggregate. The image has native Linux AMD64 and ARM64 binaries, Python, and a fixture generator. It runs without a Lighthouse build, a testnet, or network access.

The default matches Lighthouse's **pq-devnet-1** backend: leanVM [`351f7ae421529ce164ed747eee1f78bdeaec3e80`](https://github.com/leanEthereum/leanVM/commit/351f7ae421529ce164ed747eee1f78bdeaec3e80), bindings **v0.19.0**, experimental prover, log inverse rate 2. A small Rust worker links the same in-process `lean_vm` API with `cpu_tiers` and the pinned upstream `tier-rustc` wrapper. It does not build Lighthouse. Its allocator matches Lighthouse's jemalloc 0.6.1 with `abort_conf:true,narenas:16` (Linux also enables background-thread support).

Python uses a local socket to orchestrate the worker, but **proving and verification timings come from inside Rust**, around the same API/proof-conversion boundary as Lighthouse. Proving includes child proof decoding, `lean_vm::aggregate`, the expected-claim check and output proof encoding. Signature/claim input conversion and socket transfers are outside that timer. Verification includes proof decoding and `AggregateProof::verify`. Initialization is reported as setup. Per-case `.rpc.json` files separately record backend and socket roundtrip time for every request. Existing paired/full-sequence columns remain observed orchestration elapsed time and are labeled accordingly. This measures the proving backend; it does not model other beacon-node load.

Build from this checkout before using the new-version commands below. Source changes alone do not update published Docker tags. Images use the leanVM revision in their tag, plus an immutable benchmark-source tag. Old v0.15 results and tags retain their original meaning and RPC timing boundary.

Historical results use [`283686575c4e81790591d2c6e3ed21c69c0e3bbe`](https://github.com/leanEthereum/leanVM/commit/283686575c4e81790591d2c6e3ed21c69c0e3bbe). Their original images remain available as `leanvm-283686575c4e` and `leanvm-283686575c4e-fixtures`.

## Run

A **bundled image**, when built for the selected revision, runs the full 11-case sweep without generating fixtures. On a native Linux AMD64 or ARM64 engine, let Docker select the matching image and use `--execution native`. Check the engine architecture with `docker info --format '{{.Architecture}}'`.

```sh
mkdir -p results/full
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/results/full:/results" \
  ethdreamer/pq-aggregation-bench:leanvm-351f7ae42152-fixtures \
  --execution native
```

Add `--sizes 64 8192` with a fresh output directory for a quick single-committee and two-committee check, including improvement. Add `65536` to check 16-input merges in all three rounds. The bundled image reads 94,208 fixtures from a read-only file, with 32,768 for the current slot and 30,720 for each of two prior slots. An empty or read-only `/fixtures` bind mount can coexist with the bundled fixtures. Docker handles layer compression. Fixture loading and SHA-256 verification are recorded separately from proof timings.

For a smaller download or a different leanVM pin, use the fixture-free image. It generates missing records on demand and can share a writable cache between runs.

```sh
mkdir -p results/quick fixtures
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/results/quick:/results" -v "$PWD/fixtures:/fixtures" \
  ethdreamer/pq-aggregation-bench:leanvm-351f7ae42152 \
  --execution native --sizes 64 8192
```

Cached records are namespaced by fixture format, leanVM SHA, and slot workload. The runner reports how many are already cached and how many it needs to generate.

The defaults are five timed repeats and one initial request per repeated operation. Set `--threads` for the prover and `--fixture-threads` for fixture preparation. They default to the effective CPU capacity rounded down, with a minimum of one. Fixture workers never exceed the number of keys. `--reps` controls sample count and `--timeout` controls sidecar startup and socket timeouts. For emulated or unverified execution, keep the default `--execution unverified` or declare `--execution emulated` and use the result for correctness checks.

The default backend uses leanVM runtime CPU dispatch on both architectures. Leave `--cpu-profile auto`; old forced profiles and `LEANVM_TIER` overrides are rejected for in-process runs. Reports record the actual upstream tier, available tiers, protocol version, compiler flags, wrapper digest and binary digests. Legacy sidecar builds still support their old CPU-profile flags. Keep hardware and thread counts fixed for comparisons.

Each output directory holds one run. Repeat the command with `--resume` to skip completed rows after interruption. Resume requires the same kernel boot, hardware limits, executable hashes, benchmark code, settings, and case selection. A different machine, reboot, pin, or settings requires a new directory.

## Results

`results.md` and `results.csv` present the measured rows. `summary.json` includes all samples and run metadata. `cases/` holds per-case JSON and sidecar logs. The report records runtime-visible CPU model or ARM identifiers, logical CPUs, CPU affinity, effective cgroup CPU quota, visible memory and cgroup memory limit, OS and kernel, runtime architecture, build flags, both source revisions, and SHA-256 of both binaries. Bundled runs also record the complete bundle digest and the exact fixture-prefix hash for each case. Docker Desktop reports its Linux VM’s resources. Use `--host-cpu-model` if you want to add the physical host model, and report the VM mode when comparing results across machines.

`results-mean-stdev.md` puts slot geometry and all merge timings in the same table. It shows warmed committee proving, the original merge, rounds 2 and 3, the two-round prefix, the full sequence, and verification of the original and final proofs. Each timing is an arithmetic mean ± sample standard deviation in milliseconds. The details give sample counts, verification after round 2 and claim counts. The standard deviation uses n − 1, with a dash when there are fewer than two samples. Single-committee cases show **Reuse** in round 1, have no two-round prefix timing, and report two merge RPCs in the full sequence. `results-mean-stdev.csv` provides numeric means, sample standard deviations and counts. The raw results retain initial requests, medians and individual samples. The reports update as cases finish. The container prints the aligned main table and its details at the end of a successful run or resume. A narrow interactive terminal may split the main table into panels keyed by validator count.

Print aligned terminal tables from a saved run directory or its `summary.json`. Use `--markdown` for a shareable Markdown report. The renderer uses Python 3's standard library and reads the input without modifying it.

```sh
python3 render_table.py /path/to/saved-run
python3 render_table.py /path/to/saved-run/summary.json --markdown > timings.md
```

The renderer detects recorded recursive timings even when the run's workload metadata is absent.
Older runs show only operations they measured; rendering cannot recover an unmeasured round.
The image also includes the renderer at `/opt/pq-bench/render_table.py` for use with
`--entrypoint python3`. Keep the renderer updated along with the benchmark.

The main table includes **Prune (ms)** for new runs. Its details list input, retained and
removed claim counts, verification time and sample counts. All terminal, Markdown and CSV
reports include the recursive rounds and pruning measurements recorded by the run.

To regenerate the CSV and all other reports as well, use the report command. Omit `--output` to write beside the saved results, or give a separate directory to keep the source read-only.

```sh
python3 report.py /path/to/saved-run --output /path/to/reports
```

Either image can also regenerate the report with the saved run mounted read-only.

```sh
mkdir -p reports
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/saved-run:/source:ro" -v "$PWD/reports:/results" \
  --entrypoint python3 ethdreamer/pq-aggregation-bench:leanvm-351f7ae42152 \
  /opt/pq-bench/report.py /source --output /results
```

Resume a benchmark with the exact image digest or benchmark-revision tag you started with. Use the renderer for original results from before the improvement workload, or repeat the benchmark in a fresh output directory to measure all three rounds.

## Workload

The modeled slot has `C = max(1, min(16, N // 4096))` committees with `K = N / (32 * C)` signatures each. It assumes agreeing votes and full participation. Each case starts a fresh sidecar, builds and verifies one proof per committee, and reproves the first committee for the timed samples. A multi-committee case recursively merges the original children and checks the complete claim union on every merge. A one-committee case reuses its proof. The benchmark also checks that a changed message invalidates the first child and, when present, the merged proof.

In round 2, the benchmark merges the original result with `min(C, 15)` fresh full committees from one prior slot. In round 3, it merges the round 2 result with the same number of fresh full committees from a second prior slot. The three slots use disjoint validator keys, distinct signed messages, and different signing leaves. This models still-eligible, uncredited votes from disjoint same-epoch committees using synthetic fixtures and full participation. The harness checks the exact claim union in every round. Both extension rounds reject changed messages and the previous round's incomplete claim set. In each 16-committee case, both improvement rounds have 16 inputs. The 262,144-validator case has 8,192, 15,872, then 23,552 claims. At 1,048,576 validators those counts are 32,768, 63,488, and 94,208.

The round 1 column keeps the standalone timing from the original workload. **Two rounds** times the consecutive round 1 and round 2 aggregate RPCs in each multi-committee repetition. **Full sequence** extends the same timer through round 3. Single-committee cases start with their reused proof, so their full sequence measures rounds 2 and 3. All fresh proofs are ready before timing begins, and claim checking and proof verification follow the sequence. This measures local execution with inputs ready. Allow separately for collection, propagation and other proposer work when comparing to a deadline.

Timed requests include Python request construction and local Unix-socket transport. Initial and warmed timings are both recorded. The sidecar performs its own internal initialization and warm-up before accepting the initial request. Fixture generation, signing, networking, consensus state work, and node scheduling are outside the timed intervals.

After each recursive sequence, a separate **Prune** request passes the round 3 proof with
its full claims as the only child, no raw signatures, and `OutputClaims::Only` retaining
the current and first prior-slot claims. This drops the entire oldest message and creates
a new proof. Its time is excluded from **Two rounds** and **Full sequence**. The harness
checks the exact retained claims and verifies the new proof; the initial pruned proof must
reject both a changed message and the original unpruned claim set. Every validator size
measures this same pattern, so the existing signatures-per-committee column plus the
pruning claim counts describe its size. At 262,144 validators it retains 15,872 of 23,552
claims; at 1,048,576 it retains 63,488 of 94,208. Different retention patterns may have
different costs. These synthetic groups do not implement an on-chain inclusion filter.

The generator makes one-leaf XMSS keys using public deterministic seeds. Each signature has the full 2,404-byte wire layout and both layers of verification work. Unused subtrees are opaque commitments, so only the selected leaf is signable. The fixture generator checks each new signature with the pinned upstream verifier and rejects a mutated message. **Use these fixtures only for benchmarks.** They do not measure production key generation or signing.

The [historical macOS M3 Max baseline](baseline/results.md) contains the original 11-case sweep, raw timings, and fixture hashes. Its binaries were built for the M3 Max directly. The container uses fixed CPU features, and its Linux prover can select different code paths. Keep those differences visible when comparing runs. The historical two largest cases each combine two complete five-sample series.

## Build

```sh
git clone https://github.com/ethDreamer/pq-aggregation-bench.git
cd pq-aggregation-bench
./scripts/build-image.sh --load
```

Set `LEANVM_REV` to a full SHA or an upstream branch or tag. The helper resolves it to an exact SHA before building, and the build checks out that commit once for both binaries. It copies the WOTS implementation from that checkout and uses its XMSS and Keccak crates for fixture generation. The sidecar uses the upstream lockfile. The fixture lock starts with the same upstream dependencies, and the build saves both lockfile hashes and the WOTS source hash.

```sh
LEANVM_REV=refs/heads/my-upstream-branch ./scripts/build-image.sh --load
```

The helper emits `leanvm-<12-character-sha>` and `leanvm-<12-character-sha>-bench-<12-character-harness-sha>` tags. With a registry login, authorized repository, and a two-platform Buildx builder, use `IMAGE=your-registry/your-repo ./scripts/build-image.sh --push` to publish one index for Linux AMD64 and ARM64. Use the registry digest when you need an immutable image reference. The build labels store the full SHAs. `RUST_IMAGE` can override the Rust base if a newer revision needs a different toolchain.

Revision overrides must support the selected in-process or legacy backend interface and the XMSS layout/helpers described under Version and CPU compatibility below. A revision that changes these interfaces requires a harness update. Run `./scripts/smoke.sh IMAGE` and `./scripts/smoke-selected.sh IMAGE` after changing the pin. GitHub Actions runs both on native AMD64 and ARM64 runners.

### Build the bundled variant

The three-round bundle keeps the original 32,768 current-slot fixtures and 30,720 first prior-slot fixtures byte-for-byte, and adds 30,720 second prior-slot records. Its manifest binds all three sets to the full leanVM revision. The bundled build checks all 11 historical current-slot reference hashes and verifies every signature, including a wrong-message negative test for each. To produce the records from scratch, invoke the fixture-free generator. Use a suitable worker count for the build machine.

```sh
mkdir -p fixtures/source fixtures/prior-source fixtures/second-prior-source
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --entrypoint pq-geometry-fixtures -v "$PWD/fixtures/source:/fixtures" \
  ethdreamer/pq-aggregation-bench:leanvm-351f7ae42152 /fixtures 32768 4 1
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --entrypoint pq-geometry-fixtures -v "$PWD/fixtures/prior-source:/fixtures" \
  ethdreamer/pq-aggregation-bench:leanvm-351f7ae42152 /fixtures 30720 4 1 --prior-slot
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --entrypoint pq-geometry-fixtures -v "$PWD/fixtures/second-prior-source:/fixtures" \
  ethdreamer/pq-aggregation-bench:leanvm-351f7ae42152 /fixtures 30720 4 1 --second-prior-slot
python3 fixture_bundle.py pack fixtures/source fixtures/bundle \
  --prior-slot-source fixtures/prior-source --second-prior-slot-source fixtures/second-prior-source \
  --leanvm-revision 351f7ae421529ce164ed747eee1f78bdeaec3e80
FIXTURE_BUNDLE="$PWD/fixtures/bundle" ./scripts/build-image.sh --load
```

`FIXTURE_BUNDLE` selects the two `-fixtures` tags. The fixture payload is passed as a separate build context and stays out of Git. The image records its full SHA-256 and exposes the same bytes on both architectures. An override to a different leanVM revision can use the fixture-free build.

### Version and CPU compatibility

`LEANVM_REV=<full SHA> ./scripts/build-image.sh --load` (or `--push`) preserves revision selection. The default v0.19 build uses the in-process API; an override must expose the compatible `lean_vm` aggregate/verify/claims API, `cpu_tiers`, upstream tier wrapper and compatible XMSS fixture interfaces. Compile failures are deliberate for incompatible future APIs; no fallback changes the requested revision. Revisions before the in-process API use the existing legacy sidecar builder and record `legacy-rpc-v1` timings. Tested legacy pins are `c5ee9d239e42d0bc8113eb4aee3e2cae5faf1764` and the historical `283686575c4e81790591d2c6e3ed21c69c0e3bbe`; use their existing immutable images to reproduce published runs.

The v0.19 binary contains upstream CPU tiers and selects at runtime. Its baseline is generic ARM64 or x86-64, without forcing build-host CPU features. Legacy AMD64 builds instead contain `generic`, `portable`, `avx2`, `avx2-gfni` and `avx512` binaries selected by Linux flags. Historical and new timing boundaries are different and explicitly recorded; do not relabel old results as v0.19.

Fixture caches are version-isolated. Reusing an older fixture bundle at a new version requires rebuilding its manifest and verifying **every signature with the new pinned verifier**; matching sizes or protocol labels alone are not sufficient. Multi-message fixtures use a separate cache namespace keyed by message count and total signatures.

The benchmark is dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE). leanVM and its WOTS code retain their upstream license.

## Run individual workloads

`--test` is repeatable. With no `--test`, the historical full suite is unchanged
(committee proving, initial merge, rounds 2/3 and pruning at all 11 sizes).
`--sizes` filters modeled validator geometries for the selected operations.
Fixed `merge-16x2048-*` cases run once regardless of `--sizes`; they report the
actual proof/message/claim counts, not an invented active-validator count.

```sh
# Use an image built from this source revision; older published tags lack --test.
IMAGE=ethdreamer/pq-aggregation-bench:leanvm-351f7ae42152
mkdir -p results fixtures

docker run --rm "$IMAGE" --list-tests

# Only the 16-message merge; fixture generation and input proving are setup.
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/results:/results" -v "$PWD/fixtures:/fixtures" "$IMAGE" \
  --execution native --test merge-16x2048-multi-message --output /results/16-messages

# Matched single-message control, independently selectable.
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/results:/results" -v "$PWD/fixtures:/fixtures" "$IMAGE" \
  --execution native --test merge-16x2048-same-message --output /results/one-message

# A subset: committee proving + round 2, at two validator geometries.
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/results:/results" -v "$PWD/fixtures:/fixtures" "$IMAGE" \
  --execution native --test committee-proof --test round2 \
  --sizes 8192 262144 --output /results/subset

# Original suite.
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/results:/results" -v "$PWD/fixtures:/fixtures" "$IMAGE" \
  --execution native --output /results/full

# Source equivalents, with previously built binaries/build.json available.
python3 run.py --list-tests
python3 run.py --test merge-16x2048-multi-message --execution native --output results/16-messages
python3 run.py --test committee-proof --test round2 --sizes 8192 262144 --output results/subset
python3 run.py --output results/full
python3 render_table.py results/16-messages
python3 render_table.py results/16-messages --markdown
```

Both fixed cases use 16 actual input proofs, each proving 2,048 signatures by
**disjoint keys**, for 32,768 claims in one timed merge request. The multi-message
case assigns a different 32-byte signed message to each group, representing
16 consecutive slots in one epoch (the same XMSS leaf). Its matched control uses
the same key seeds and leaf but one common message. These are cryptographic
workloads; they do not simulate gossip, proposer eligibility or a whole network.
The shape can represent 16 slots sampled from a 65,536-validator epoch with one
2,048-validator committee per slot. No validators repeat between input proofs.

Only the selected operation is repeatedly timed. Necessary dependency proofs are
prepared once, with setup elapsed time and per-input proof times recorded
separately. Every output is checked against the exact expected claim set;
wrong-message and missing-claim verification must fail. Pruning additionally
rejects the original unpruned claim set. The first request is excluded from the
warmed statistics, as in the historical suite. A single-committee `slot-merge`
reuses its input and reports no merge timing.

Multi-message signatures need a new fixture cache; even a bundled image must
have a writable `/fixtures` mount for this case. Existing bundles still accelerate
the one-message control. Setup can take longer than the merge. Use `--reps 1`
for a correctness smoke or the default five warmed measurements for comparisons.
To resume, repeat the **same selection and settings** with `--resume`. Changing
tests, geometry, binary/version, hardware or harness requires a new output directory.

Selected runs export `summary.json`, `results.csv`, `results-mean-stdev.csv`,
`results.md` and `results-mean-stdev.md`. Their tables identify the workload and
input/output proof, message and claim counts. Historical reports remain readable.
The default leanVM pin is the devnet-1 v0.19 revision; `LEANVM_REV=<full-sha> ./scripts/build-image.sh`
still builds a different revision with that revision recorded in its reports.
