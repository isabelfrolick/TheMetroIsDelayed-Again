#!/usr/bin/env python3
"""Sanity-check the pipeline outputs before they are committed and deployed.

Exits non-zero on failure, which stops the GitHub Actions job: nothing is
committed and the live site keeps serving the last good data.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "data"
HISTORY = ROOT / "data" / "raw" / "incidents_history.csv.gz"
GEO = ROOT / "data" / "geo" / "metro_lines.geojson"

EXPECTED_LINES = {"green", "orange", "yellow", "blue"}
MIN_LATEST_ROWS = 1_000        # the STM file has tens of thousands of rows
MAX_ISSUE_SHARE = 0.05         # max share of train incidents we fail to parse

errors: list[str] = []
warnings: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        errors.append(msg)


def load(name: str) -> dict:
    path = OUT / name
    if not path.exists():
        errors.append(f"missing {path.relative_to(ROOT)}")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    meta, stats, lik = load("meta.json"), load("stats.json"), load("likelihood.json")
    check(HISTORY.exists(), "history file missing")
    if errors:
        report()

    # --- source file looks healthy ----------------------------------------------
    check(meta["latest_rows"] >= MIN_LATEST_ROWS,
          f"latest file only has {meta['latest_rows']} rows (expected >= {MIN_LATEST_ROWS})")
    check(meta["history_rows"] >= meta["previous_history_rows"],
          "history shrank: " f"{meta['previous_history_rows']} -> {meta['history_rows']}")
    if meta.get("data_through") and meta.get("previous_data_through"):
        check(meta["data_through"] >= meta["previous_data_through"],
              f"data_through went backwards: {meta['previous_data_through']} -> {meta['data_through']}")
    if not meta.get("data_through"):
        warnings.append("could not read 'Données jusqu'à …' banner; using latest month in data")

    issues = meta["issues"]
    n = max(issues["train_incidents"], 1)
    for key in ("unknown_line", "unparseable_time"):
        check(issues[key] / n <= MAX_ISSUE_SHARE,
              f"{issues[key]} of {n} train incidents have {key.replace('_', ' ')} "
              f"(> {MAX_ISSUE_SHARE:.0%}); has the CSV format changed?")

    # --- stats.json ---------------------------------------------------------------
    check(set(stats.get("lines", {})) == EXPECTED_LINES, f"lines are {set(stats.get('lines', {}))}")
    for k, s in stats.get("lines", {}).items():
        check(s["incidents"] > 0, f"{k}: no delays in the {stats['window']['months']}-month window")
        check(s["avg_delay_min"] is None or 0 < s["avg_delay_min"] < 720, f"{k}: odd average {s['avg_delay_min']}")
        check(0 <= s["pct_hours_delayed"] <= 100, f"{k}: pct_hours_delayed out of range")
    months = [m["month"] for m in stats.get("monthly", [])]
    check(len(months) >= 12, f"only {len(months)} months of history")
    check(bool(months) and months[-1] == stats.get("latest_month"),
          f"last month in table ({months[-1] if months else None}) != latest_month ({stats.get('latest_month')})")
    check(months == sorted(months), "monthly table not sorted")

    # --- stations ----------------------------------------------------------------
    latest = stats.get("monthly", [{}])[-1]
    check(len(latest.get("top3_stations", [])) == 3,
          f"latest month has {len(latest.get('top3_stations', []))} ranked stations (expected 3)")
    match = issues.get("station_match_pct")
    if GEO.exists():
        check(len(stats.get("stations", [])) >= 20,
              f"only {len(stats.get('stations', []))} stations matched to the map (expected ~68)")
        if match is not None and match < 90:
            warnings.append(f"only {match}% of station names matched the map; "
                            f"unmatched: {issues.get('unmatched_station_names')}")

    # --- likelihood.json ----------------------------------------------------------
    n_hours = len(lik.get("hours", []))
    for k, g in {**lik.get("lines", {}), "any": lik.get("any")}.items():
        check(g is not None and len(g) == 7 and all(len(row) == n_hours for row in g),
              f"likelihood grid for {k} has wrong shape")
        vals = [v for row in (g or []) for v in row if v is not None]
        check(all(0 <= v <= 1 for v in vals), f"likelihood for {k} has values outside [0, 1]")

    if not GEO.exists():
        warnings.append("line geometry missing: run `python pipeline/fetch.py --geo` once")
    report(meta, stats)


def report(meta: dict | None = None, stats: dict | None = None) -> None:
    for w in warnings:
        print(f"WARNING: {w}")
    if errors:
        for e in errors:
            print(f"ERROR: {e}")
        print(f"\nValidation FAILED ({len(errors)} error(s)); nothing will be published.")
        sys.exit(1)
    print(f"Validation passed: {meta['history_rows']:,} incidents in history, "
          f"data through {stats['latest_month']}, top 3 that month: "
          f"{', '.join(stats['monthly'][-1]['top3'])} | top stations: "
          f"{', '.join(x['name'] for x in stats['monthly'][-1]['top3_stations'])}")


if __name__ == "__main__":
    main()