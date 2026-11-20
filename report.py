"""Render one benchmark run from its completed cases."""

import csv
import io
import json
from pathlib import Path


def save_text(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def save_json(path, value):
    save_text(path, json.dumps(value, indent=2, allow_nan=False) + "\n")


def measured_rows(directory, manifest):
    rows = []
    for size in manifest["settings"]["sizes"]:
        path = directory / "cases" / f"{size}.json"
        if not path.exists():
            continue
        row = json.loads(path.read_text(encoding="utf-8"))
        if (
            row.get("run_id") != manifest["run_id"]
            or row.get("active_validators") != size
            or row.get("all_proofs_verified") is not True
        ):
            raise ValueError(f"Case does not belong to this completed run: {path}")
        rows.append(row)
    return rows


def write_report(directory, manifest, rows):
    save_json(directory / "summary.json", {"run": manifest, "cases": rows})

    columns = [
        "Active validators", "Committees per slot", "Signatures per committee",
        "Signatures per slot", "First committee proof (ms)", "Committee proof median (ms)",
        "Committee verify median (ms)", "First merge (ms)", "Merge median (ms)",
        "Final verify median (ms)", "Final proof (bytes)", "Samples per operation",
    ]
    table = []
    for row in rows:
        merge = row["proposer_merge"]
        table.append([
            row["active_validators"],
            row["committees_per_slot"],
            row["signatures_per_committee"],
            row["signatures_per_slot"],
            round(row["initial_child_prove_ms"][0], 2),
            round(row["committee_prove"]["median_ms"], 2),
            round(row["committee_verify"]["median_ms"], 2),
            round(row["first_merge_ms"], 2) if merge else None,
            round(merge["median_ms"], 2) if merge else None,
            round(row["final_verify"]["median_ms"], 2),
            row["final_proof_bytes"],
            len(row["committee_prove"]["samples_ms"]),
        ])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerows(table)
    save_text(directory / "results.csv", buffer.getvalue())

    hardware, build, settings = (manifest[key] for key in ("hardware", "build", "settings"))
    cpu_models = ", ".join(hardware["cpu_models"]) or "Unknown"
    cpu_capacity = hardware["effective_cpu_capacity"]
    cpu_quota = hardware["cpu_quota_cores"]

    def gib(value):
        return f"{value / 2**30:.2f} GiB" if value is not None else "Unknown or unlimited"

    lines = [
        "# PQ aggregation benchmark",
        "",
        f"Run `{manifest['run_id']}` began at {manifest['utc_start']}. "
        f"{len(rows)}/{len(settings['sizes'])} cases completed.",
        "",
        f"- leanVM `{build['leanvm_revision']}`, experimental prover, log inverse rate "
        f"{settings['log_inv_rate']}. Benchmark revision `{build['benchmark_revision']}`.",
        f"- Runtime {hardware['os']} {hardware['os_release'] or ''}, kernel "
        f"{hardware['kernel_release']}, {hardware['architecture']}. "
        f"Image target `{build['target_platform']}`, execution **{settings['execution']}**.",
        f"- Runtime CPU model {cpu_models}. Logical CPUs {hardware['logical_cpus_visible']}, "
        f"effective capacity {cpu_capacity:g}, cgroup CPU quota "
        f"{f'{cpu_quota:g} cores' if cpu_quota is not None else 'unknown or unlimited'}.",
        f"- Runtime-visible memory {gib(hardware['memory_visible_bytes'])}. "
        f"Cgroup memory limit {gib(hardware['memory_cgroup_limit_bytes'])}.",
        f"- Prover threads {settings['prover_threads']}, fixture workers {settings['fixture_threads']}, "
        f"{settings['repetitions']} timed repetitions and one initial request per repeated operation.",
        f"- Rust build `{build['rustc']}` with `{build['rustflags']}`.",
        f"- Sidecar SHA-256 `{build['sidecar_sha256']}`.",
        f"- Fixture generator SHA-256 `{build['fixture_generator_sha256']}`.",
    ]
    bundle = build.get("fixture_bundle")
    if bundle:
        lines.append(
            f"- Bundled fixtures {bundle['record_count']:,}, SHA-256 `{bundle['sha256']}`. "
            "The image reads the fixtures without generating them."
        )
    if settings["host_cpu_model"]:
        supplied_model = settings["host_cpu_model"].replace("|", "\\|")
        lines.append(f"- Operator reports a physical host CPU of {supplied_model}.")
    if settings["execution"] != "native":
        lines.append("- Use these results for validation only. Comparative timings require native execution.")

    lines.extend([
        "",
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---:"] * len(columns)) + " |",
    ])
    for row in table:
        lines.append("| " + " | ".join("—" if v is None else f"{v:,}" for v in row) + " |")
    lines.extend([
        "",
        "The first committee request uses a new sidecar process. The first recursive merge follows "
        "child proof generation and verification. Single-committee cases reuse the committee proof. "
        "Times include Python request construction and local socket transport. Fixture creation, "
        "signing, network propagation, consensus state and node scheduling are outside the timed workload.",
        "",
        "See [summary.json](summary.json) for runtime and build metadata, initial and warmed timing "
        "samples, fixture hashes, proof sizes and load averages.",
        "",
    ])
    save_text(directory / "results.md", "\n".join(lines))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    args = parser.parse_args()
    run = json.loads((args.run_directory / "run.json").read_text(encoding="utf-8"))
    write_report(args.run_directory, run, measured_rows(args.run_directory, run))
