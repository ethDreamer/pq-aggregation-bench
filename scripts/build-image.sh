#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

mode=${1:---load}
if [[ $# -gt 1 || ( $mode != --load && $mode != --push ) ]]; then
    echo "Usage: $0 [--load|--push]" >&2
    exit 2
fi
if [[ -n $(git status --porcelain) ]]; then
    echo "Commit source changes before building a revision-labeled image." >&2
    exit 1
fi

upstream=https://github.com/leanEthereum/leanVM
requested=${LEANVM_REV:-$(sed -n 's/^ARG LEANVM_REV=//p' Dockerfile)}
if [[ $requested =~ ^[0-9a-f]{40}$ ]]; then
    revision=$requested
else
    case "$requested" in
        refs/heads/*) patterns=("$requested") ;;
        refs/tags/*) patterns=("$requested" "$requested^{}") ;;
        *) patterns=("refs/heads/$requested" "refs/tags/$requested" "refs/tags/$requested^{}") ;;
    esac
    revision=$(
        git ls-remote "$upstream" "${patterns[@]}" | awk '
            $2 ~ /^refs\/heads\// { branch = $1 }
            $2 ~ /\^\{\}$/ { peeled = $1 }
            $2 ~ /^refs\/tags\// && $2 !~ /\^\{\}$/ { tag = $1 }
            END {
                if (branch && (tag || peeled)) exit 1;
                print branch ? branch : (peeled ? peeled : tag);
            }
        '
    ) || { echo "Failed to resolve an unambiguous leanVM ref: $requested" >&2; exit 1; }
fi
if [[ ! $revision =~ ^[0-9a-f]{40}$ ]]; then
    echo "Use a full leanVM commit SHA, branch, or tag from $upstream." >&2
    exit 1
fi

source_revision=$(git rev-parse HEAD)
image=${IMAGE:-ethdreamer/pq-aggregation-bench}
tag="$image:leanvm-${revision:0:12}"
options=("$mode")
if [[ -n ${FIXTURE_BUNDLE:-} ]]; then
    bundle_directory=$(cd "$FIXTURE_BUNDLE" && pwd -P)
    fixture_digest=$(python3 fixture_bundle.py verify "$bundle_directory" "$revision" --require-round3)
    tag="$tag-fixtures"
    options+=(--target bundled --build-context "fixture_bundle=$bundle_directory")
    options+=(--build-arg "FIXTURE_BUNDLE_SHA256=$fixture_digest")
fi
source_tag="$tag-bench-${source_revision:0:12}"
options+=(--tag "$tag" --tag "$source_tag")
if [[ $mode == --push ]]; then
    options+=(--platform linux/amd64,linux/arm64)
fi
if [[ -n ${BUILD_METADATA:-} ]]; then
    options+=(--metadata-file "$BUILD_METADATA")
fi
if [[ -n ${RUST_IMAGE:-} ]]; then
    options+=(--build-arg "RUST_IMAGE=$RUST_IMAGE")
fi
echo "Building $tag and $source_tag from leanVM $revision" >&2
exec docker buildx build "${options[@]}" \
    --build-arg "LEANVM_REV=$revision" --build-arg "BENCH_REV=$source_revision" .
