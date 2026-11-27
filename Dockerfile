# syntax=docker/dockerfile:1
ARG RUST_IMAGE=rust:1.99.1-bookworm@sha256:e6f2cff35d0f309acf8433b4f05878ed77092602c5065cc81801177da0bb3a9c
ARG LEANVM_REV=351f7ae421529ce164ed747eee1f78bdeaec3e80
FROM ${RUST_IMAGE} AS binaries
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
ENV CARGO_NET_GIT_FETCH_WITH_CLI=true
WORKDIR /src
ARG LEANVM_REV
RUN set -eu; \
    revision="$LEANVM_REV"; \
    printf '%s\n' "$revision" | grep -Eq '^[0-9a-f]{40}$'; \
    git init -q leanvm; \
    git -C leanvm fetch -q --depth=1 https://github.com/leanEthereum/leanVM "$revision"; \
    git -C leanvm checkout -q --detach FETCH_HEAD; \
    test "$(git -C leanvm rev-parse HEAD)" = "$revision"
COPY generator/ generator/
COPY worker/ worker/
COPY scripts/build-binaries.sh scripts/build-legacy-binaries.sh ./
ARG TARGETARCH
ARG BENCH_REV=unrecorded
RUN --mount=type=cache,id=pq-aggregation-registry,target=/usr/local/cargo/registry \
    --mount=type=cache,id=pq-aggregation-git,target=/usr/local/cargo/git \
    --mount=type=cache,id=pq-aggregation-target-${TARGETARCH},target=/target,sharing=locked \
    ./build-binaries.sh "${TARGETARCH}" "${BENCH_REV}"

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS runtime
ARG LEANVM_REV
ARG BENCH_REV=unrecorded
LABEL org.opencontainers.image.source="https://github.com/ethDreamer/pq-aggregation-bench" \
    org.opencontainers.image.revision="${BENCH_REV}" \
    io.leanethereum.leanvm.revision="${LEANVM_REV}"
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=binaries /out/bin/ /usr/local/bin/
COPY --from=binaries /out/build.json /opt/pq-bench/build.json
COPY --from=binaries /out/licenses/ /usr/share/doc/pq-aggregation-bench/leanvm/
COPY LICENSE-APACHE LICENSE-MIT /usr/share/doc/pq-aggregation-bench/
COPY run.py environment.py fixture_bundle.py report.py render_table.py selected_cases.py /opt/pq-bench/
RUN mkdir -m 1777 /results /fixtures
USER 65532:65532
ENV BENCH_OUTPUT=/results BENCH_FIXTURES=/fixtures PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python3", "/opt/pq-bench/run.py"]

FROM runtime AS bundled
ARG LEANVM_REV
ARG FIXTURE_BUNDLE_SHA256
LABEL io.leanethereum.pq-bench.fixtures.sha256="${FIXTURE_BUNDLE_SHA256}"
USER 0:0
RUN mkdir -m 0555 /opt/pq-bench/bundled-fixtures
COPY --from=fixture_bundle --chmod=0444 fixtures.bin manifest.json /opt/pq-bench/bundled-fixtures/
USER 65532:65532
RUN --network=none test -n "$FIXTURE_BUNDLE_SHA256" \
    && python3 /opt/pq-bench/fixture_bundle.py verify \
        /opt/pq-bench/bundled-fixtures "$LEANVM_REV" --sha256 "$FIXTURE_BUNDLE_SHA256" --require-round3 \
    && pq-geometry-fixtures --verify-pack /opt/pq-bench/bundled-fixtures/fixtures.bin 4

FROM runtime AS fixture-free
