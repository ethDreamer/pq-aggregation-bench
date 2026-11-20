"""Render one benchmark run from its completed cases."""

import csv
import io
import json
from pathlib import Path
from statistics import fmean, stdev
import shutil
import sys
from textwrap import fill

GEOMETRY_FIELDS = (
    "active_validators", "committees_per_slot", "signatures_per_committee",
    "signatures_per_slot", "merge_operations",
)
WARMED_OPERATIONS = ("committee_prove", "proposer_merge", "final_verify", "committee_verify")
IMPROVEMENT_OPERATIONS = ("proposer_improvement", "paired_first_merge", "two_round_merge", "improved_verify")
IMPROVEMENT_FIELDS = (
    "improvement_fan_in", "improvement_fresh_proofs", "improvement_added_claims",
    "improvement_total_claims", "improvement_message_groups",
)
IMPROVEMENT_NOTES = (
    "Improvement starts from the original result and adds prior-slot proofs. Verify covers the original result.",
    "Two rounds is paired elapsed time for first merge followed by improvement. "
    "Committee proving and verification are outside the pair. "
    "Single-committee cases start from their reused proof and have only one recursive round.",
)


def save_text(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def save_json(path, value):
    save_text(path, json.dumps(value, indent=2, allow_nan=False) + "\n")


def belongs_to_run(row, manifest, size):
    return (
        row.get("run_id") == manifest["run_id"]
        and row.get("active_validators") == size
        and row.get("all_proofs_verified") is True
    )


def measured_rows(directory, manifest):
    rows = []
    for size in manifest["settings"]["sizes"]:
        path = directory / "cases" / f"{size}.json"
        if not path.exists():
            continue
        row = json.loads(path.read_text(encoding="utf-8"))
        if not belongs_to_run(row, manifest, size):
            raise ValueError(f"Case does not belong to this completed run: {path}")
        rows.append(row)
    return rows


def write_csv(path, columns, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerows(rows)
    save_text(path, buffer.getvalue())


def timing_statistics(timing):
    samples = timing["samples_ms"] if timing is not None else ()
    n = len(samples)
    return fmean(samples) if n else None, stdev(samples) if n > 1 else None, n


def format_timing(timing):
    mean, sample_sd, _ = timing
    if mean is None:
        return "—"
    spread = f"{sample_sd:,.1f}" if sample_sd is not None else "—"
    return f"{mean:,.1f} ± {spread}"


def markdown_table(columns, rows):
    yield "| " + " | ".join(columns) + " |"
    yield "| " + " | ".join(["---:"] * len(columns)) + " |"
    for row in rows:
        yield "| " + " | ".join(f"{value:,}" if isinstance(value, int) else value for value in row) + " |"


def terminal_table(columns, rows, width=None):
    cells = [
        tuple(f"{value:,}" if isinstance(value, int) else value for value in row)
        for row in rows
    ]
    widths = [max(map(len, column)) for column in zip(columns, *cells)]
    tables = []
    indices = [0]
    occupied = widths[0]
    for index in range(1, len(columns)):
        if width is not None and len(indices) > 1 and occupied + 2 + widths[index] > width:
            tables.append(indices)
            indices = [0]
            occupied = widths[0]
        indices.append(index)
        occupied += 2 + widths[index]
    tables.append(indices)

    lines = []
    for indices in tables:
        if lines:
            lines.append("")
        lines.append("  ".join(columns[i].rjust(widths[i]) for i in indices))
        lines.append("  ".join("-" * widths[i] for i in indices))
        lines.extend("  ".join(row[i].rjust(widths[i]) for i in indices) for row in cells)
    return lines


def warmed_timings(rows):
    return [
        (
            tuple(row[field] for field in GEOMETRY_FIELDS),
            tuple(timing_statistics(row.get(operation))
                  for operation in WARMED_OPERATIONS + IMPROVEMENT_OPERATIONS),
            tuple(row.get(field) for field in IMPROVEMENT_FIELDS),
        )
        for row in rows
    ]


def render_mean_stdev(manifest, data, *, markdown=False, width=None):
    hardware, build, settings = (manifest[key] for key in ("hardware", "build", "settings"))
    show_improvement = bool(settings.get("improvement_workload"))
    primary, details, improvement_details = [], [], []
    for slot, timing, improvement_geometry in data:
        active_validators, _, _, _, merge_jobs = slot
        prove, merge, final_verify, committee_verify, improvement, _, paired, improved_verify = timing
        primary_row = [*slot, format_timing(prove), format_timing(merge) if merge_jobs else "Reuse",
                       format_timing(final_verify)]
        if show_improvement:
            primary_row.append(format_timing(improvement))
        primary.append(primary_row)
        details.append((
            active_validators, format_timing(committee_verify),
            prove[2], committee_verify[2], merge[2], final_verify[2],
        ))
        if show_improvement:
            improvement_details.append((
                active_validators, *improvement_geometry, format_timing(improved_verify), format_timing(paired),
            ))

    models = ", ".join(hardware.get("cpu_models") or []) or "unknown"
    architecture = hardware.get("architecture") or "unknown architecture"
    execution = settings.get("execution") or "unknown"
    system = hardware.get("os_release") or hardware.get("os") or "unknown"
    visible = hardware.get("logical_cpus_visible")
    capacity = hardware.get("effective_cpu_capacity")
    allocation = f"{capacity:g} CPU equivalents" if capacity is not None else "unknown"
    threads = settings.get("prover_threads") or "unknown"
    memory = hardware.get("memory_visible_bytes")
    ram = f"{memory / 2**30:.1f} GiB" if memory is not None else "unknown"
    limit = hardware.get("memory_cgroup_limit_bytes")
    limit_text = f" Container memory limit {limit / 2**30:.1f} GiB." if limit is not None else ""
    pin = (build.get("leanvm_revision") or "unknown")[:12]
    sample_counts = ", ".join(str(n) for n in sorted({
        n for _, timings, _ in data for _, _, n in timings if n
    }))

    title = "PQ aggregation benchmark timing statistics"
    details_title = "Committee verification and sample counts"
    improvement_title = "Improvement workload and paired merge latency"
    leanvm = f"`{pin}`" if markdown else pin
    metadata = [
        f"Runtime CPU {models} ({architecture}, {execution} execution).",
        f"{visible if visible is not None else 'Unknown number of'} visible logical CPUs. "
        f"Effective CPU allocation {allocation}. Prover threads {threads}.",
        f"Runtime-visible RAM {ram}.{limit_text}",
        f"leanVM {leanvm}. Completed {len(data)}/{len(settings['sizes'])} cases.",
    ]
    if markdown:
        metadata[0] += f" Runtime OS {system}."
    else:
        metadata.insert(1, f"Runtime OS {system}.")
    profile = build.get("sidecar_cpu_profile")
    if profile:
        release = build.get("leanvm_bindings_version")
        bindings = f"Bindings {release}. " if release else ""
        metadata.append(
            f"{bindings}Experimental prover, CPU profile {profile}, log inverse rate {settings['log_inv_rate']}."
        )
    if settings.get("host_cpu_model"):
        metadata.append(f"Operator reports a physical host CPU of {settings['host_cpu_model']}.")
    if sample_counts:
        metadata.append(f"Warmed samples per measured operation (n) {sample_counts}.")

    reuse = "**Reuse**" if markdown else "Reuse"
    notes = [
        "Timings are arithmetic mean ± sample standard deviation in milliseconds. "
        "The standard deviation uses n − 1 in the denominator. " +
        ("All tables" if show_improvement else "Both tables") + " use warmed samples and exclude initial requests.",
        f"Single-committee cases show {reuse} and have zero merge jobs. "
        "A dash marks a missing timing or a standard deviation with fewer than two samples.",
    ]
    if show_improvement:
        notes.extend(IMPROVEMENT_NOTES)
    if markdown:
        improvement_section = []
        if show_improvement:
            improvement_section = ["", f"## {improvement_title}", "", *markdown_table([
                "Active validators", "Fan-in", "Fresh proofs", "New claims", "Final claims",
                "Message groups", "Improved verify (ms)", "Two rounds (ms)",
            ], improvement_details)]
        return "\n".join([
            f"# {title}", "", *(f"- {line}" for line in metadata), "", *notes, "",
            *markdown_table([
                "Active validators", "Committees / slot", "Signatures / committee", "Signatures / slot",
                "Merge jobs", "Prove one committee (ms)", "Merge (ms)", "Verify final (ms)",
                *(["Improvement (ms)"] if show_improvement else []),
            ], primary),
            "", f"## {details_title}", "",
            *markdown_table([
                "Active validators", "Verify one committee (ms)", "Committee prove n",
                "Committee verify n", "Merge n", "Final verify n",
            ], details),
            *improvement_section,
            "",
        ])

    if width is None:
        width = shutil.get_terminal_size(fallback=(100, 24)).columns if sys.stdout.isatty() else 100
    columns = (
        "Validators", "Comm/slot", "Sigs/comm", "Sigs/slot", "Merges",
        "Prove one (ms)", "Merge (ms)", "Verify (ms)",
        *(("Improvement (ms)",) if show_improvement else ()),
    )
    primary_table = terminal_table(columns, primary)
    if len(primary_table[0]) > width:
        primary_table = [
            *terminal_table(columns[:5], [row[:5] for row in primary], width),
            "",
            *terminal_table((columns[0], *columns[5:]), [(row[0], *row[5:]) for row in primary], width),
        ]
    detail_table = terminal_table(
        ("Validators", "Comm verify (ms)", "Prove n", "Comm verify n", "Merge n", "Final verify n"),
        details, width,
    )
    terminal_notes = [
        "Mean ± sample SD (ms) of warmed requests. Sample SD uses n − 1. Initial requests are excluded.",
        "Verify is final verification. Reuse means zero merges. A dash marks missing data or SD (n < 2).",
    ]
    improvement_section = []
    if show_improvement:
        terminal_notes.extend(IMPROVEMENT_NOTES)
        improvement_section = ["", improvement_title, "", *terminal_table(
            ("Validators", "Fan-in", "Fresh proofs", "New claims", "Final claims", "Messages",
             "Improved verify (ms)", "Two rounds (ms)"),
            improvement_details, width,
        )]
    lines = [
        title, "", *(fill(line, width) for line in metadata), "",
        *(fill(line, width) for line in terminal_notes), "",
        *primary_table, "", details_title, "", *detail_table, *improvement_section, "",
    ]
    return "\n".join(lines)


def write_mean_stdev_report(directory, manifest, rows):
    data = warmed_timings(rows)
    save_text(directory / "results-mean-stdev.md", render_mean_stdev(manifest, data, markdown=True))

    show_improvement = bool(manifest["settings"].get("improvement_workload"))
    operations = WARMED_OPERATIONS + (IMPROVEMENT_OPERATIONS if show_improvement else ())
    timing_columns = (
        f"{operation}_{statistic}"
        for operation in operations
        for statistic in ("mean_ms", "sample_stdev_ms", "n")
    )
    geometry_columns = IMPROVEMENT_FIELDS if show_improvement else ()
    numeric = (
        (
            *slot,
            *(value for operation in timing[:len(operations)] for value in operation),
            *(geometry if show_improvement else ()),
        )
        for slot, timing, geometry in data
    )
    write_csv(directory / "results-mean-stdev.csv",
              (*GEOMETRY_FIELDS, *timing_columns, *geometry_columns), numeric)


def write_report(directory, manifest, rows):
    save_json(directory / "summary.json", {"run": manifest, "cases": rows})

    columns = [
        "Active validators", "Committees per slot", "Signatures per committee",
        "Signatures per slot", "First committee proof (ms)", "Committee proof median (ms)",
        "Committee verify median (ms)", "First merge (ms)", "Merge median (ms)",
        "Final verify median (ms)", "Final proof (bytes)", "Samples per operation",
    ]
    show_improvement = bool(manifest["settings"].get("improvement_workload"))
    if show_improvement:
        columns.extend(["Improvement fan-in", "New claims", "Improvement median (ms)",
                        "Two rounds median (ms)"])
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
        if show_improvement:
            paired = row["two_round_merge"]
            table[-1].extend([
                row["improvement_fan_in"], row["improvement_added_claims"],
                round(row["proposer_improvement"]["median_ms"], 2),
                round(paired["median_ms"], 2) if paired else None,
            ])

    write_csv(directory / "results.csv", columns, table)
    write_mean_stdev_report(directory, manifest, rows)

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
    if build.get("sidecar_cpu_profile"):
        lines.append(
            f"- Prover CPU profile `{build['sidecar_cpu_profile']}`. "
            f"Selected binary `{build['sidecar_binary']}`."
        )
    bundle = build.get("fixture_bundle")
    if bundle:
        lines.append(
            f"- Bundled fixtures {bundle['record_count'] + bundle.get('prior_slot', {}).get('record_count', 0):,}, "
            f"SHA-256 `{bundle['sha256']}`. "
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
        *(IMPROVEMENT_NOTES if show_improvement else ()),
        *([""] if show_improvement else []),
        "See [summary.json](summary.json) for runtime and build metadata, initial and warmed timing "
        "samples, fixture hashes, proof sizes and load averages.",
        "",
    ])
    save_text(directory / "results.md", "\n".join(lines))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path, help="existing run with run.json and cases/")
    parser.add_argument("--output", type=Path, help="write reports here (default: run directory)")
    args = parser.parse_args()
    try:
        run = json.loads((args.run_directory / "run.json").read_text(encoding="utf-8"))
        rows = measured_rows(args.run_directory, run)
        output = args.output or args.run_directory
        output.mkdir(parents=True, exist_ok=True)
        write_report(output, run, rows)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"Report failed: {error}\n")
