# PQ aggregation benchmark

Measure leanVM’s experimental rate-2 prover with real XMSS committee proofs and recursive slot aggregation. The image has native Linux AMD64 and ARM64 binaries, Python, and a fixture generator. It runs without a Lighthouse build, a testnet, or network access.

The default leanVM commit is [`283686575c4e81790591d2c6e3ed21c69c0e3bbe`](https://github.com/leanEthereum/leanVM/commit/283686575c4e81790591d2c6e3ed21c69c0e3bbe). It matches the published `ethpandaops/lighthouse:pq-devnet-0` image at index `sha256:f571048fbeeb9b3e7eaede512f94e74a09dcc73b4f0565dc5afe3365f122f771` and its [Lighthouse source](https://github.com/ethDreamer/lighthouse/blob/9deb902a682900fa9f7b1d0dd6daa260eecf0bbd/beacon_node/pq_prover/src/lib.rs).

## Run

Use the **bundled image** to run the full 11-case sweep without generating fixtures. On a native Linux AMD64 or ARM64 engine, let Docker select the matching image and use `--execution native`. Check the engine architecture with `docker info --format '{{.Architecture}}'`.

```sh
mkdir -p results/full
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/results/full:/results" \
  ethdreamer/pq-aggregation-bench:leanvm-283686575c4e-fixtures \
  --execution native
```

Add `--sizes 64 8192` with a fresh output directory for a quick single-committee proof and real two-child merge. The bundled image reads its 32,768 fixtures directly from a read-only file. An empty or read-only `/fixtures` bind mount can coexist with the bundled fixtures. Docker handles layer compression. Fixture loading and SHA-256 verification are recorded separately from proof timings.

For a smaller download or a different leanVM pin, use the fixture-free image. It generates missing records on demand and can share a writable cache between runs.

```sh
mkdir -p results/quick fixtures
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/results/quick:/results" -v "$PWD/fixtures:/fixtures" \
  ethdreamer/pq-aggregation-bench:leanvm-283686575c4e \
  --execution native --sizes 64 8192
```

Cached records are namespaced by fixture format and leanVM SHA. The runner reports how many are already cached and how many it needs to generate.

The defaults are five timed repeats and one initial request per repeated operation. Set `--threads` for the prover and `--fixture-threads` for fixture preparation. They default to the effective CPU capacity rounded down, with a minimum of one. Fixture workers never exceed the number of keys. `--reps` controls sample count and `--timeout` controls sidecar startup and socket timeouts. For emulated or unverified execution, keep the default `--execution unverified` or declare `--execution emulated` and use the result for correctness checks.

Each output directory holds one run. Repeat the command with `--resume` to skip completed rows after interruption. Resume requires the same kernel boot, hardware limits, executable hashes, benchmark code, settings, and case selection. A different machine, reboot, pin, or settings requires a new directory.

## Results

`results.md` and `results.csv` present the measured rows. `summary.json` includes all samples and run metadata. `cases/` holds per-case JSON and sidecar logs. The report records runtime-visible CPU model or ARM identifiers, logical CPUs, CPU affinity, effective cgroup CPU quota, visible memory and cgroup memory limit, OS and kernel, runtime architecture, build flags, both source revisions, and SHA-256 of both binaries. Bundled runs also record the complete bundle digest and the exact fixture-prefix hash for each case. Docker Desktop reports its Linux VM’s resources. Use `--host-cpu-model` if you want to add the physical host model, and report the VM mode when comparing results across machines.

`results-mean-stdev.md` gives the slot geometry, warmed committee proving, recursive merging and final verification as mean ± sample standard deviation in milliseconds. A second table gives committee verification and sample counts for all four operations. The standard deviation uses n − 1, with a dash when there are fewer than two samples. Single-committee cases show **Reuse** and have no merge samples. `results-mean-stdev.csv` provides separate numeric mean, sample standard deviation and count columns with blanks for unavailable means or deviations. Initial requests, medians, and individual timings remain in the original reports and raw results. The report updates as cases finish. The container prints the aligned terminal table at the end of a successful run or resume. The saved Markdown and CSV contain the same figures.

Print aligned terminal tables from a saved run directory or its `summary.json`. Use `--markdown` for a shareable Markdown report. The renderer uses Python 3's standard library and reads the input without modifying it.

```sh
python3 render_table.py /path/to/saved-run
python3 render_table.py /path/to/saved-run/summary.json --markdown > timings.md
```

To regenerate the CSV and all other reports as well, use the report command. Omit `--output` to write beside the saved results, or give a separate directory to keep the source read-only.

```sh
python3 report.py /path/to/saved-run --output /path/to/reports
```

Either image can also regenerate the report with the saved run mounted read-only.

```sh
mkdir -p reports
docker run --rm --network none --user "$(id -u):$(id -g)" \
  -v "$PWD/saved-run:/source:ro" -v "$PWD/reports:/results" \
  --entrypoint python3 ethdreamer/pq-aggregation-bench:leanvm-283686575c4e \
  /opt/pq-bench/report.py /source --output /results
```

Resume a benchmark with the exact image digest or benchmark-revision tag you started with.

The modeled slot has `C = max(1, min(16, N // 4096))` committees with `K = N / (32 * C)` signatures each. It assumes agreeing votes and full participation. Each case starts a fresh sidecar, builds and verifies one proof per committee, and reproves the first committee for the timed samples. A multi-committee case recursively merges the original children and checks the complete claim union on every merge. A one-committee case reuses its proof. The benchmark also checks that a changed message invalidates the first child and, when present, the merged proof.

Timed requests include Python request construction and local Unix-socket transport. Initial and warmed timings are both recorded. The sidecar performs its own internal initialization and warm-up before accepting the initial request. Fixture generation, signing, networking, consensus state work, and node scheduling are outside the timed intervals.

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

Future revisions must support the experimental prover feature, sidecar wire v1, the rate-2 request, and the XMSS layout and helpers used here. A revision that changes these interfaces requires an update to the harness. Run `./scripts/smoke.sh IMAGE` after changing the pin. GitHub Actions runs the smoke on native AMD64 and ARM64 runners.

### Build the bundled variant

The fixture pack must match all 11 baseline fixture hashes and the selected leanVM revision. To produce the records from scratch, invoke the generator in the fixture-free image. Use a suitable worker count for the build machine.

```sh
mkdir -p fixtures/source
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --entrypoint pq-geometry-fixtures -v "$PWD/fixtures/source:/fixtures" \
  ethdreamer/pq-aggregation-bench:leanvm-283686575c4e /fixtures 32768 4 1
python3 fixture_bundle.py pack fixtures/source fixtures/bundle
FIXTURE_BUNDLE="$PWD/fixtures/bundle" ./scripts/build-image.sh --load
```

`FIXTURE_BUNDLE` selects the two `-fixtures` tags. The fixture payload is passed as a separate build context and stays out of Git. The image records its full SHA-256 and exposes the same bytes on both architectures. An override to a different leanVM revision can use the fixture-free build.

Linux AMD64 uses SSSE3 and PCLMULQDQ. Linux ARM64 uses the ARMv8 AES/PMULL extension and jemalloc configured to support kernels with up to 64 KiB pages. These match the published devnet build flags and work on compatible CPUs without inheriting the builder’s CPU features. Host-tuned builds can select different prover kernels and should be reported separately.

The benchmark is dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE). leanVM and its WOTS code retain their upstream license.
