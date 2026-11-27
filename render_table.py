#!/usr/bin/env python3
"""Print the mean ± sample standard deviation table for completed benchmark cases."""

import argparse
import json
from pathlib import Path

from report import belongs_to_run, measured_rows, render_mean_stdev, warmed_timings


def read_saved_run(path):
    if path.is_dir():
        manifest_path = path / "run.json"
        summary_path = path / "summary.json"
        if manifest_path.is_file() and ((path / "cases").is_dir() or not summary_path.is_file()):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return manifest, measured_rows(path, manifest)
        path = summary_path

    summary = json.loads(path.read_text(encoding="utf-8"))
    manifest = summary["run"]
    if "selected_cases" in manifest["settings"]:
        from selected_cases import valid_case
        plan = {c["case_id"]: c for c in manifest["settings"]["selected_cases"]}
        rows = {}
        for row in summary["cases"]:
            key = row.get("case_id")
            if key not in plan or key in rows or not valid_case(row, manifest, plan[key]):
                raise ValueError(f"Summary contains an invalid selected case: {key}")
            rows[key] = row
        return manifest, [rows[key] for key in plan if key in rows]
    sizes = manifest["settings"]["sizes"]
    rows_by_size = {}
    for row in summary["cases"]:
        size = row["active_validators"]
        if size not in sizes or size in rows_by_size or not belongs_to_run(row, manifest, size):
            raise ValueError(f"Summary contains an invalid completed case for {size} validators")
        rows_by_size[size] = row
    return manifest, [rows_by_size[size] for size in sizes if size in rows_by_size]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path, help="saved run directory or its summary.json")
    parser.add_argument("--markdown", action="store_true", help="print a Markdown report")
    args = parser.parse_args()
    try:
        manifest, rows = read_saved_run(args.results)
        if "selected_cases" in manifest["settings"]:
            from selected_cases import render_selected
            print(render_selected(manifest, rows, markdown=args.markdown), end="")
        else:
            print(render_mean_stdev(manifest, warmed_timings(rows), markdown=args.markdown), end="")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"Unable to render timing table: {error}\n")


if __name__ == "__main__":
    main()
