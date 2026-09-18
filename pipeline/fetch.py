#!/usr/bin/env python3
"""Download the STM métro incidents CSV and detect whether it changed.

Usage:
    python pipeline/fetch.py                 # download; skip if unchanged
    python pipeline/fetch.py --force         # treat as changed even if identical
    python pipeline/fetch.py --from-file X   # use a manually downloaded CSV
    python pipeline/fetch.py --geo           # (one-off) build line geometry from STM GTFS

In GitHub Actions, writes `changed=true|false` to $GITHUB_OUTPUT.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
LATEST = RAW_DIR / "latest.csv"            # not committed (see .gitignore)
HASH_FILE = RAW_DIR / "latest.sha256"      # committed: lets the next run detect changes
GEO_FILE = ROOT / "data" / "geo" / "metro_lines.geojson"

INCIDENTS_URL = "https://donneesouvertes.stm.info/fichiers/Incidents%20m%C3%A9tro.csv"
GTFS_URL = "https://www.stm.info/sites/default/files/gtfs/gtfs_stm.zip"

# GTFS route_short_name / route_id -> our line key
ROUTE_KEYS = {"1": "green", "2": "orange", "4": "yellow", "5": "blue"}
HEADERS = {"User-Agent": "stm-delay-tracker (portfolio project; GitHub Actions)"}


def set_output(name: str, value: str) -> None:
    """Expose a value to later GitHub Actions steps (no-op locally)."""
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")
    print(f"[output] {name}={value}")


def download(url: str, dest: Path, timeout: int = 120, attempts: int = 3) -> None:
    for i in range(1, attempts + 1):
        try:
            with requests.get(url, headers=HEADERS, timeout=timeout, stream=True) as r:
                r.raise_for_status()
                with open(dest, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
            return
        except requests.RequestException as exc:
            print(f"Download attempt {i}/{attempts} failed: {exc}", file=sys.stderr)
            if i == attempts:
                raise
            time.sleep(10 * i)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def looks_like_incident_csv(path: Path) -> bool:
    """Guard against saving an HTML error page or an empty file as data."""
    if path.stat().st_size < 10_000:
        return False
    head = path.read_bytes()[:4000].decode("utf-8", errors="ignore").lower()
    return "numero d" in head and ";" in head and "<html" not in head


def fetch_incidents(force: bool, from_file: str | None) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LATEST.with_suffix(".tmp")
    if from_file:
        shutil.copyfile(from_file, tmp)
    else:
        print(f"Downloading {INCIDENTS_URL}")
        download(INCIDENTS_URL, tmp)

    if not looks_like_incident_csv(tmp):
        tmp.unlink(missing_ok=True)
        sys.exit("Downloaded file does not look like the STM incidents CSV. Aborting.")

    new_hash = sha256(tmp)
    old_hash = HASH_FILE.read_text().strip() if HASH_FILE.exists() else ""
    changed = force or new_hash != old_hash

    tmp.replace(LATEST)
    HASH_FILE.write_text(new_hash + "\n")
    print(f"Size: {LATEST.stat().st_size / 1e6:.1f} MB | sha256 {new_hash[:12]}… | "
          f"{'CHANGED' if new_hash != old_hash else 'unchanged'}{' (forced)' if force else ''}")
    set_output("changed", "true" if changed else "false")


# --------------------------------------------------------------------------- #
# One-off: métro line geometry from the STM GTFS feed (~70 MB download)
# --------------------------------------------------------------------------- #
def build_geo(out: Path = GEO_FILE) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / "gtfs.zip"
        print(f"Downloading GTFS {GTFS_URL} (large, one-off)…")
        download(GTFS_URL, zpath, timeout=600)
        with zipfile.ZipFile(zpath) as zf:
            names = {Path(n).name.lower(): n for n in zf.namelist()}

            def read(name: str, **kw) -> pd.DataFrame | pd.io.parsers.TextFileReader:
                return pd.read_csv(zf.open(names[name]), dtype=str, keep_default_na=False, **kw)

            routes = read("routes.txt")
            metro = routes[pd.to_numeric(routes["route_type"], errors="coerce") == 1].copy()
            label = metro.get("route_short_name", metro["route_id"]).where(
                lambda s: s.isin(ROUTE_KEYS.keys()), metro["route_id"])
            metro["key"] = label.map(ROUTE_KEYS)
            metro = metro.dropna(subset=["key"])
            if metro.empty:
                sys.exit("No métro routes (route_type=1) found in GTFS.")

            trips = read("trips.txt")
            trips = trips[trips["route_id"].isin(metro["route_id"])]
            trip_ids = set(trips["trip_id"])
            st = pd.concat(
                c[c["trip_id"].isin(trip_ids)]
                for c in read("stop_times.txt", usecols=["trip_id", "stop_id", "stop_sequence"],
                              chunksize=500_000)
            )
            st["stop_sequence"] = st["stop_sequence"].astype(int)
            stops = read("stops.txt").set_index("stop_id")

            # Representative trip per route = the one serving the most stops (direction 0 if available)
            n_stops = st.groupby("trip_id").size().rename("n")
            trips = trips.join(n_stops, on="trip_id").dropna(subset=["n"])
            if "direction_id" in trips:
                trips = trips.sort_values(["direction_id", "n"], ascending=[True, False])
            else:
                trips = trips.sort_values("n", ascending=False)
            rep = trips.groupby("route_id").head(1)

            shapes = None
            if "shape_id" in rep and "shapes.txt" in names and (rep["shape_id"] != "").all():
                need = set(rep["shape_id"])
                shapes = pd.concat(c[c["shape_id"].isin(need)]
                                   for c in read("shapes.txt", chunksize=500_000))
                shapes["shape_pt_sequence"] = shapes["shape_pt_sequence"].astype(int)

            features = []
            for _, trip in rep.iterrows():
                route = metro[metro["route_id"] == trip["route_id"]].iloc[0]
                color = "#" + route["route_color"] if route.get("route_color") else None
                seq = st[st["trip_id"] == trip["trip_id"]].sort_values("stop_sequence")
                stn = stops.loc[seq["stop_id"]]
                # use the parent station's coordinates/name when present
                if "parent_station" in stn.columns:
                    ids = [p if p and p in stops.index else sid
                           for sid, p in zip(stn.index, stn["parent_station"])]
                    stn = stops.loc[ids]

                if shapes is not None:
                    pts = shapes[shapes["shape_id"] == trip["shape_id"]].sort_values("shape_pt_sequence")
                    coords = pts[["shape_pt_lon", "shape_pt_lat"]].astype(float).round(5).values.tolist()
                else:
                    coords = stn[["stop_lon", "stop_lat"]].astype(float).round(5).values.tolist()

                features.append({
                    "type": "Feature",
                    "properties": {"kind": "line", "line": route["key"],
                                   "name": route.get("route_long_name", ""), "color": color},
                    "geometry": {"type": "LineString", "coordinates": coords},
                })
                for sid, s in stn[~stn.index.duplicated()].iterrows():
                    features.append({
                        "type": "Feature",
                        "properties": {"kind": "station", "line": route["key"], "name": s["stop_name"]},
                        "geometry": {"type": "Point",
                                     "coordinates": [round(float(s["stop_lon"]), 5),
                                                     round(float(s["stop_lat"]), 5)]},
                    })

    out.write_text(json.dumps({"type": "FeatureCollection", "features": features},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n_lines = sum(f["properties"]["kind"] == "line" for f in features)
    print(f"Wrote {out.relative_to(ROOT)}: {n_lines} lines, {len(features) - n_lines} stations "
          f"({'GTFS shapes' if shapes is not None else 'station-to-station'} geometry)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="mark as changed even if identical")
    ap.add_argument("--from-file", help="use a local CSV instead of downloading")
    ap.add_argument("--geo", action="store_true", help="build métro line GeoJSON from GTFS and exit")
    args = ap.parse_args()
    if args.geo:
        build_geo()
    else:
        fetch_incidents(args.force, args.from_file)


if __name__ == "__main__":
    main()