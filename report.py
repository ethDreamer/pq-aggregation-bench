"""Render one benchmark run from its completed cases."""

import csv
import io
import json
from pathlib import Path
from statistics import fmean, stdev
import shutil
import sys
from textwrap import fill

from fixture_bundle import PRIOR_SLOT_NAMES

GEOMETRY_FIELDS = (
    "active_validators", "committees_per_slot", "signatures_per_committee",
    "signatures_per_slot", "merge_operations",
)
WARMED_OPERATIONS = ("committee_prove", "proposer_merge", "final_verify", "committee_verify")
IMPROVEMENT_OPERATIONS = ("proposer_improvement", "paired_first_merge", "two_round_merge", "improved_verify")
ROUND3_OPERATIONS = ("round3_merge", "round3_verify", "merge_sequence")
PRUNE_OPERATIONS = ("prune", "prune_verify")
ALL_OPERATIONS = WARMED_OPERATIONS + IMPROVEMENT_OPERATIONS + ROUND3_OPERATIONS + PRUNE_OPERATIONS
IMPROVEMENT_FIELDS = (
    "improvement_fan_in", "improvement_fresh_proofs", "improvement_added_claims",
    "improvement_total_claims", "improvement_message_groups",
)
ROUND3_FIELDS = (
    "round3_fan_in", "round3_fresh_proofs", "round3_added_claims",
    "round3_total_claims", "round3_message_groups", "merge_sequence_requests",
)
PRUNE_FIELDS = (
    "prune_input_proofs", "prune_input_claims", "prune_retained_claims",
    "prune_removed_claims", "prune_input_messages", "prune_retained_messages",
)
ALL_FIELDS = IMPROVEMENT_FIELDS + ROUND3_FIELDS + PRUNE_FIELDS
PRUNE_NOTES = (
    "Prune is a separate one-input recursive request: take the round 3 proof and retain only "
    "the current and first prior-slot claims, dropping the oldest message. No raw signatures "
    "are supplied. Pruning and its verification are outside Full sequence.",
)
IMPROVEMENT_NOTES = (
    "Round 2 extends the original proof with fresh prior-slot proofs. Verify original covers the "
    "proof after round 1 or the reused committee proof.",
    "Two rounds is observed elapsed time for consecutive round 1 and round 2 requests. "
    "Fixture preparation, input proving and verification are outside the timed sequence.",
)
ROUND3_NOTES = (
    "Round 3 extends the round 2 proof with fresh proofs for a different prior-slot message. "
    "Full sequence is observed elapsed time across all listed merge RPCs.",
    "Single-committee cases reuse their original proof in round 1. Their full sequence contains "
    "two merge RPCs, in rounds 2 and 3.",
)


def report_extensions(settings, measured=()):
    workload = settings.get("improvement_workload")
    show_prune = bool(settings.get("pruning_workload")) or "prune" in measured
    show_round3 = workload == "prior-slot-append-v2" or "round3_merge" in measured or show_prune
    show_improvement = bool(workload) or "proposer_improvement" in measured or show_round3
    return show_improvement, show_round3, show_prune


def recorded_operations(rows):
    return {operation for row in rows for operation in ALL_OPERATIONS if row.get(operation)}


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
        yield "| " + " | ".join("—" if value is None else f"{value:,}" if isinstance(value, int) else value for value in row) + " |"


def terminal_table(columns, rows, width=None):
    cells = [
        tuple("—" if value is None else f"{value:,}" if isinstance(value, int) else value for value in row)
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
                  for operation in ALL_OPERATIONS),
            tuple(row.get(field) for field in ALL_FIELDS),
        )
        for row in rows
    ]


def render_mean_stdev(manifest, data, *, markdown=False, width=None):
    hardware, build, settings = (manifest[key] for key in ("hardware", "build", "settings"))
    measured = {operation for _, timings, _ in data
                for operation, (_, _, n) in zip(ALL_OPERATIONS, timings) if n}
    show_improvement, show_round3, show_prune = report_extensions(settings, measured)
    primary, details, improvement_details, prune_details = [], [], [], []
    for slot, timing, geometry in data:
        active_validators, _, _, _, merge_jobs = slot
        (
            prove, merge, final_verify, committee_verify, improvement, _, paired, improved_verify,
            round3, round3_verify, sequence, prune, prune_verify,
        ) = timing
        round2_geometry = geometry[:len(IMPROVEMENT_FIELDS)]
        round3_start = len(IMPROVEMENT_FIELDS)
        prune_start = round3_start + len(ROUND3_FIELDS)
        _, _, _, round3_claims, round3_messages, merge_requests = geometry[round3_start:prune_start]
        primary_row = [*slot, format_timing(prove), format_timing(merge) if merge_jobs else "Reuse"]
        if show_improvement:
            primary_row.extend((format_timing(improvement), format_timing(paired)))
        if show_round3:
            primary_row.extend((format_timing(round3), merge_requests, format_timing(sequence)))
        if show_prune:
            primary_row.append(format_timing(prune))
        primary_row.append(format_timing(final_verify))
        if show_round3:
            primary_row.append(format_timing(round3_verify))
        primary.append(primary_row)
        details.append((
            active_validators, format_timing(committee_verify),
            prove[2], committee_verify[2], merge[2], final_verify[2],
        ))
        if show_improvement:
            detail_row = [active_validators, *round2_geometry, format_timing(improved_verify)]
            if show_round3:
                detail_row.extend((round3_claims, round3_messages))
            improvement_details.append(detail_row)
        if show_prune:
            prune_details.append((active_validators, *geometry[prune_start:],
                                  format_timing(prune_verify), prune[2], prune_verify[2]))

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
    improvement_title = "Recursive workload and round 2 verification"
    prune_title = "One-proof pruning workload"
    prune_columns = ("Validators", "Inputs", "Input claims", "Kept claims", "Removed claims",
                     "Input messages", "Kept messages", "Prune verify (ms)", "Prune n", "Verify n")
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
    shared_notes = []
    if show_improvement:
        shared_notes.extend(IMPROVEMENT_NOTES)
        if show_round3:
            shared_notes.extend(ROUND3_NOTES)
        else:
            shared_notes.append("Single-committee cases have one recursive merge, in round 2.")
    if show_prune:
        shared_notes.extend(PRUNE_NOTES)
    notes = [
        "Timings are arithmetic mean ± sample standard deviation in milliseconds. "
        "The standard deviation uses n − 1 in the denominator. " +
        ("All tables" if show_improvement else "Both tables") + " use warmed samples and exclude initial requests.",
        f"Single-committee cases show {reuse} and have zero round 1 merge jobs. "
        "A dash marks a missing timing or a standard deviation with fewer than two samples.",
        *shared_notes,
    ]
    if markdown:
        columns = [
            "Active validators", "Committees / slot", "Signatures / committee", "Signatures / slot",
            "Merge jobs", "Prove one committee (ms)", "Round 1 merge (ms)" if show_improvement else "Merge (ms)",
        ]
        if show_improvement:
            columns.extend(("Round 2 merge (ms)", "Two rounds (ms)"))
        if show_round3:
            columns.extend(("Round 3 merge (ms)", "Merge RPCs", "Full sequence (ms)"))
        if show_prune:
            columns.append("Prune (ms)")
        columns.append("Verify original (ms)" if show_improvement else "Verify final (ms)")
        if show_round3:
            columns.append("Verify round 3 (ms)")
        improvement_section = []
        if show_improvement:
            geometry_columns = [
                "Active validators", "Fan-in", "Fresh proofs", "New claims", "Round 2 claims",
                "Round 2 messages", "Round 2 verify (ms)",
            ]
            if show_round3:
                geometry_columns.extend(("Round 3 claims", "Round 3 messages"))
            improvement_section = [
                "", f"## {improvement_title}", "",
                *markdown_table(geometry_columns, improvement_details),
            ]
        return "\n".join([
            f"# {title}", "", *(f"- {line}" for line in metadata), "", *notes, "",
            *markdown_table(columns, primary),
            "", f"## {details_title}", "",
            *markdown_table([
                "Active validators", "Verify one committee (ms)", "Committee prove n",
                "Committee verify n", "Merge n", "Final verify n",
            ], details),
            *improvement_section,
            *(["", f"## {prune_title}", "", *markdown_table(prune_columns, prune_details)] if show_prune else []),
            "",
        ])

    if width is None and (sys.stdout.isatty() or not show_improvement):
        width = shutil.get_terminal_size(fallback=(100, 24)).columns if sys.stdout.isatty() else 100
    columns = [
        "Validators", "Comm/slot", "Sigs/comm", "Sigs/slot", "Merges", "Prove one (ms)",
        "Round 1 (ms)" if show_improvement else "Merge (ms)",
    ]
    if show_improvement:
        columns.extend(("Round 2 (ms)", "Two rounds (ms)"))
    if show_round3:
        columns.extend(("Round 3 (ms)", "Merge RPCs", "Full sequence (ms)"))
    if show_prune:
        columns.append("Prune (ms)")
    columns.append("Verify orig. (ms)" if show_improvement else "Verify (ms)")
    if show_round3:
        columns.append("Verify R3 (ms)")
    primary_table = terminal_table(columns, primary)
    if width is not None and len(primary_table[0]) > width:
        geometry_panel = terminal_table(columns[:5], [row[:5] for row in primary], width)
        timing_panel = terminal_table((columns[0], *columns[5:]), [(row[0], *row[5:]) for row in primary], width)
        if show_improvement:
            primary_table = ["Slot geometry", "", *geometry_panel, "", "Timings by validator count", "", *timing_panel]
        else:
            primary_table = [*geometry_panel, "", *timing_panel]
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
        terminal_notes[-1] = "Reuse means zero round 1 merges. A dash marks missing data or SD (n < 2)."
        terminal_notes.extend(shared_notes)
        geometry_columns = [
            "Validators", "Fan-in", "Fresh proofs", "New claims", "R2 claims", "R2 messages", "R2 verify (ms)",
        ]
        if show_round3:
            geometry_columns.extend(("R3 claims", "R3 messages"))
        improvement_section = [
            "", improvement_title, "", *terminal_table(geometry_columns, improvement_details, width),
        ]
    lines = [
        title, "", *(fill(line, width) if width is not None else line for line in metadata), "",
        *(fill(line, width) if width is not None else line for line in terminal_notes), "",
        *primary_table, "", details_title, "", *detail_table, *improvement_section,
        *(["", prune_title, "", *terminal_table(prune_columns, prune_details, width)] if show_prune else []), "",
    ]
    return "\n".join(lines)


def write_mean_stdev_report(directory, manifest, rows):
    data = warmed_timings(rows)
    save_text(directory / "results-mean-stdev.md", render_mean_stdev(manifest, data, markdown=True))

    show_improvement, show_round3, show_prune = report_extensions(manifest["settings"], recorded_operations(rows))
    operations = WARMED_OPERATIONS + (IMPROVEMENT_OPERATIONS if show_improvement else ())
    if show_round3:
        operations += ROUND3_OPERATIONS
    if show_prune:
        operations += PRUNE_OPERATIONS
    timing_columns = (
        f"{operation}_{statistic}"
        for operation in operations
        for statistic in ("mean_ms", "sample_stdev_ms", "n")
    )
    geometry_columns = IMPROVEMENT_FIELDS if show_improvement else ()
    if show_round3:
        geometry_columns += ROUND3_FIELDS
    if show_prune:
        geometry_columns += PRUNE_FIELDS
    numeric = (
        (
            *slot,
            *(value for operation in timing[:len(operations)] for value in operation),
            *geometry[:len(geometry_columns)],
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
    show_improvement, show_round3, show_prune = report_extensions(manifest["settings"], recorded_operations(rows))
    if show_improvement:
        columns.extend(["Improvement fan-in", "New claims", "Improvement median (ms)",
                        "Two rounds median (ms)"])
    if show_round3:
        columns.extend(["Round 3 median (ms)", "Merge RPCs", "Full sequence median (ms)",
                        "Round 3 verify median (ms)", "Round 3 total claims"])
    if show_prune:
        columns.extend(["Prune median (ms)", "Prune verify median (ms)", "Prune input claims",
                        "Prune retained claims", "Prune removed claims"])
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
        if show_round3:
            table[-1].extend([
                round(row["round3_merge"]["median_ms"], 2),
                row["merge_sequence_requests"],
                round(row["merge_sequence"]["median_ms"], 2),
                round(row["round3_verify"]["median_ms"], 2),
                row["round3_total_claims"],
            ])
        if show_prune:
            table[-1].extend([
                round(row["prune"]["median_ms"], 2), round(row["prune_verify"]["median_ms"], 2),
                row["prune_input_claims"], row["prune_retained_claims"], row["prune_removed_claims"],
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
        fixture_count = bundle["record_count"] + sum(
            bundle[name]["record_count"] for name in PRIOR_SLOT_NAMES if name in bundle
        )
        lines.append(
            f"- Bundled fixtures {fixture_count:,}, "
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
        *(ROUND3_NOTES if show_round3 else ()),
        *(PRUNE_NOTES if show_prune else ()),
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
