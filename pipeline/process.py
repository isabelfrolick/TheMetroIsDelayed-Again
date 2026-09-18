#!/usr/bin/env python3
"""Clean the STM métro incidents CSV, merge it into the stored history,
and compute the statistics the static site needs.

Inputs : data/raw/latest.csv (from fetch.py), data/raw/incidents_history.csv.gz (if any)
Outputs: data/raw/incidents_history.csv.gz
         site/data/stats.json       per-line stats, global stats, monthly rankings
         site/data/likelihood.json  P(delay) by line x weekday x hour
         site/data/meta.json        provenance + numbers used by validate.py

Metric definitions (also shown on the site):
  * Only train incidents (type "T") count; station incidents don't affect service.
  * A "delay" is a train incident whose service interruption lasted >= MIN_DELAY minutes
    (the STM's own reliability indicators ignore interruptions under 5 minutes).
  * Duration = "Heure de reprise" - "Heure de l'incident" (wrapping past midnight).
  * "% of hours delayed" = share of service hours (line x date x hour) during which
    at least one delay was in progress on that line.
  * Station rankings count ALL incidents (train + station) located at a station and
    starting during service hours (05:00-00:59); transfer stations count once.
"""
from __future__ import annotations

import difflib
import io
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "data" / "raw" / "latest.csv"
HISTORY = ROOT / "data" / "raw" / "incidents_history.csv.gz"
OUT_DIR = ROOT / "site" / "data"
GEO = ROOT / "data" / "geo" / "metro_lines.geojson"
SOURCE_URL = "https://donnees.montreal.ca/dataset/incidents-du-reseau-du-metro"

MIN_DELAY = 5                        # minutes
MAX_PLAUSIBLE = 12 * 60              # longer durations are treated as data errors
WINDOW_MONTHS = 24                   # rolling window for hover stats + likelihood
SERVICE_HOURS = list(range(5, 24))   # 05:00–23:59 slots (métro runs ~05:30–01:00)
STATION_ACTIVE_HOURS = set(range(5, 24)) | {0}   # skip overnight work while the métro is closed
STATION_RANK_BY = "incidents"        # or "delays" / "delay_min"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

LINES = {  # key: (label, French name, colour)
    "green": ("Green line", "Ligne verte", "#008E4F"),
    "orange": ("Orange line", "Ligne orange", "#EF8122"),
    "yellow": ("Yellow line", "Ligne jaune", "#FFD200"),
    "blue": ("Blue line", "Ligne bleue", "#0083C9"),
}

# Source column -> our name. Matching ignores case, accents and whitespace.
COLUMNS = {
    "Numero d'incident": "incident_id",
    "Type d'incident": "type",
    "Cause primaire": "cause_primary",
    "Cause secondaire": "cause_secondary",
    "Symptome": "symptom",
    "Ligne": "line_raw",
    "Heure de l'incident": "time_start",
    "Heure de reprise": "time_end",
    "Incident en minutes": "duration_band",
    "Code de lieu": "location",
    "Jour calendaire": "date",
}
FR_MONTHS = ["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet",
             "aout", "septembre", "octobre", "novembre", "decembre"]


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #
def norm(s: str) -> str:
    s = str(s).replace("\u2019", "'")
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).strip().lower()


def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def read_stm_csv(path: Path) -> tuple[pd.DataFrame, str | None]:
    """Return (dataframe with our column names, data-through month 'YYYY-MM' or None)."""
    lines = decode(path.read_bytes()).splitlines()
    try:
        hdr = next(i for i, l in enumerate(lines[:20]) if norm(l).startswith("numero d'incident"))
    except StopIteration:
        raise SystemExit("Could not find the header row ('Numero d'incident;…') in the CSV.")

    data_through = None
    m = re.search(r"jusqu'a\s+([a-z]+)\s+(\d{4})", norm(" ".join(lines[:hdr])))
    if m and m.group(1) in FR_MONTHS:
        data_through = f"{m.group(2)}-{FR_MONTHS.index(m.group(1)) + 1:02d}"

    sep = ";" if lines[hdr].count(";") >= lines[hdr].count(",") else ","
    df = pd.read_csv(io.StringIO("\n".join(lines[hdr:])), sep=sep, dtype=str,
                     keep_default_na=False, on_bad_lines="warn")
    df.columns = [c.strip() for c in df.columns]
    lookup = {norm(k): v for k, v in COLUMNS.items()}
    df = df.rename(columns={c: lookup[norm(c)] for c in df.columns if norm(c) in lookup})
    missing = [v for v in COLUMNS.values() if v not in df.columns]
    if missing:
        raise SystemExit(f"Missing expected columns {missing}. Found: {list(df.columns)}")

    df = df[list(COLUMNS.values())].apply(lambda s: s.str.strip())
    return df[df["incident_id"] != ""], data_through


def merge_history(new: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Upsert new rows into the history by incident_id (newest version wins)."""
    prev_rows = 0
    if HISTORY.exists():
        old = pd.read_csv(HISTORY, dtype=str, keep_default_na=False)
        prev_rows = len(old)
        new = pd.concat([old, new], ignore_index=True)
    merged = (new.drop_duplicates("incident_id", keep="last")
                 .sort_values(["date", "time_start", "incident_id"], ignore_index=True))
    return merged, prev_rows


# --------------------------------------------------------------------------- #
# Cleaning
# --------------------------------------------------------------------------- #
def to_timedelta(t: pd.Series) -> pd.Series:
    t = t.where(t.str.count(":") == 2, t + ":00")          # HH:MM -> HH:MM:SS
    return pd.to_timedelta(t, errors="coerce")


LINE_NUMBERS = {"1": "green", "2": "orange", "4": "yellow", "5": "blue"}   # no line 3 since the 1960s
LINE_WORDS = {"verte": "green", "vert": "green", "green": "green", "orange": "orange",
              "jaune": "yellow", "yellow": "yellow", "bleue": "blue", "bleu": "blue", "blue": "blue"}
NOT_AFFECTED = {"non affecte", "not affected", "#", ""}


def parse_lines(raw: str) -> list[str] | None:
    """'Ligne verte' -> ['green']; 'Ligne 2, 1, 4' -> ['orange', 'green', 'yellow'];
    'Non affecté' -> [] (no line affected); unrecognised -> None."""
    s = norm(raw)
    if s in NOT_AFFECTED:
        return []
    found = []
    for tok in re.findall(r"[a-z]+|\d+", s):
        key = LINE_NUMBERS.get(tok) or LINE_WORDS.get(tok)
        if key is None and tok.isdigit():
            return None                       # a line number we don't know (e.g. 3): flag it
        if key and key not in found:
            found.append(key)
    return found or None


def enrich(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    d = df.copy()
    parsed = {v: parse_lines(v) for v in d["line_raw"].unique()}
    d["lines"] = d["line_raw"].map(parsed)
    d["day"] = pd.to_datetime(d["date"], errors="coerce")
    d["start"] = d["day"] + to_timedelta(d["time_start"])
    d["end"] = d["day"] + to_timedelta(d["time_end"])
    wrap = d["end"] < d["start"]
    d.loc[wrap, "end"] += pd.Timedelta(days=1)
    d["minutes"] = (d["end"] - d["start"]).dt.total_seconds() / 60

    d["month"] = d["day"].dt.strftime("%Y-%m")
    trains = d["type"].str.upper() == "T"
    d["is_delay"] = trains & d["minutes"].between(MIN_DELAY, MAX_PLAUSIBLE)
    unknown = trains & d["lines"].isna()
    n_lines = d["lines"].map(lambda x: len(x) if isinstance(x, list) else 0)
    issues = {
        "train_incidents": int(trains.sum()),
        "unknown_line": int(unknown.sum()),
        "not_affected_line": int((trains & (n_lines == 0) & ~unknown).sum()),
        "multi_line_incidents": int((trains & (n_lines > 1)).sum()),
        "unparseable_time": int((trains & d["minutes"].isna()).sum()),
        "implausible_duration": int((trains & (d["minutes"] > MAX_PLAUSIBLE)).sum()),
        "unknown_line_values": d.loc[unknown, "line_raw"].value_counts().head(10).to_dict(),
    }
    # one row per (incident, affected line); network-wide stats de-duplicate on incident_id
    delays = (d[d["is_delay"] & (n_lines > 0)]
              .explode("lines").rename(columns={"lines": "line"}).copy())
    return d, delays, issues


# --------------------------------------------------------------------------- #
# Stations: match "Code de lieu" (e.g. "STATION ST-LAURENT") to GTFS stations
# --------------------------------------------------------------------------- #
STATION_PREFIX = r"^(station|stat\.?)\s+"
# Manual fallbacks for names that neither exact nor fuzzy matching resolve:
# "<key of name as written in the CSV>": "<key of the map station>"  (keys = station_key(...))
STATION_ALIASES: dict[str, str] = {
    "parc": "duparc",                                              # GTFS: "Du Parc"
    "longueuiluniversitedesherbrooke": "longueuiludesherbrooke",   # GTFS: "LONGUEUIL- U. de SHERBROOKE"
}
# Clean display names for stations whose GTFS name reads badly (keyed by map station key)
DISPLAY_OVERRIDES = {
    "duparc": "Parc",
    "longueuiludesherbrooke": "Longueuil–Université-de-Sherbrooke",
}


def station_key(name: str) -> str:
    s = re.sub(STATION_PREFIX, "", norm(name))
    s = re.sub(r"\bsainte\b", "ste", s)
    s = re.sub(r"\bsaint\b", "st", s)
    return re.sub(r"[^a-z0-9]", "", s)


NAME_FIXES = {"Uqam": "UQAM", "Oaci": "OACI", "Mcgill": "McGill", "-Ix": "-IX", "D'i": "D'I",
              "L'a": "L'A", "-De-": "-de-", "-Du-": "-du-", "-Des-": "-des-", "-D'": "-d'"}


def display_name(name: str) -> str:
    s = re.sub(r"^\s*(station|stat\.?)\s+", "", str(name), flags=re.IGNORECASE).strip()
    if s.isupper():
        s = s.title()
        for bad, good in NAME_FIXES.items():
            s = s.replace(bad, good)
    return s


def load_station_index() -> dict:
    """key -> {name, lines, coords} for every métro station in the GeoJSON."""
    if not GEO.exists():
        return {}
    idx: dict = {}
    for f in json.loads(GEO.read_text(encoding="utf-8"))["features"]:
        p = f["properties"]
        if p.get("kind") != "station":
            continue
        e = idx.setdefault(station_key(p["name"]), {"name": display_name(p["name"]), "lines": [],
                                                    "coords": f["geometry"]["coordinates"]})
        if e["name"].isupper() or e["name"] == display_name(p["name"]).title():
            e["name"] = display_name(p["name"])      # prefer a mixed-case spelling if any line has one
    for k, name in DISPLAY_OVERRIDES.items():
        if k in idx:
            idx[k]["name"] = name
        if p["line"] not in e["lines"]:
            e["lines"].append(p["line"])
    return idx


def resolve_station(key: str, idx: dict) -> str | None:
    key = STATION_ALIASES.get(key, key)
    if key in idx:
        return key
    # tolerate suffixes/abbreviations, e.g. "squarevictoria" vs "squarevictoriaoaci"
    hits = [k for k in idx if len(key) >= 4 and (k.startswith(key) or key.startswith(k))]
    if len(hits) == 1:
        return hits[0]
    # last resort: close spelling (e.g. accents/hyphens handled differently); strict cutoff
    close = difflib.get_close_matches(key, idx.keys(), n=1, cutoff=0.85)
    return close[0] if close else None


def station_frame(d: pd.DataFrame, idx: dict) -> tuple[pd.DataFrame, dict]:
    """Incidents (train + station) located at a station and starting during service hours."""
    in_service = d["start"].dt.hour.isin(STATION_ACTIVE_HOURS)
    looks_like_station = d["location"].map(norm).str.match(STATION_PREFIX)
    keys = d["location"].map(station_key)
    resolved = pd.Series({k: resolve_station(k, idx) for k in keys.unique()})
    matched = keys.map(resolved)

    s = d[in_service & (matched.notna() | looks_like_station)].copy()
    s["station_key"] = matched[s.index].fillna(keys[s.index])
    s["station"] = [idx[k]["name"] if k in idx else display_name(loc)
                    for k, loc in zip(s["station_key"], s["location"])]
    s["delay_min"] = s["minutes"].where(s["is_delay"], 0)

    station_like = d[in_service & looks_like_station]
    unmatched = station_like.loc[matched[station_like.index].isna(), "location"]
    diag = {
        "station_rows": int(len(s)),
        "station_match_pct": r(100 * (1 - len(unmatched) / max(len(station_like), 1))) if idx else None,
        "unmatched_station_names": unmatched.value_counts().head(15).to_dict(),
        "map_stations_without_incidents": sorted(idx[k]["name"] for k in set(idx) - set(s["station_key"])),
        "top_non_station_locations": d.loc[in_service & ~d.index.isin(s.index)
                                            & ~d["location"].isin(["", "#"]), "location"]
                                     .value_counts().head(10).to_dict(),
    }
    return s, diag


def rank_stations(g: pd.DataFrame, idx: dict, n: int | None = 3) -> list[dict]:
    agg = (g.groupby("station_key")
             .agg(station=("station", "first"), incidents=("incident_id", "size"),
                  delays=("is_delay", "sum"), delay_min=("delay_min", "sum"))
             .sort_values([STATION_RANK_BY] + [c for c in ("incidents", "delays", "delay_min")
                                               if c != STATION_RANK_BY], ascending=False))
    out = []
    for key, row in (agg.head(n) if n else agg).iterrows():
        info = idx.get(key, {})
        out.append({"key": key, "name": row["station"], "lines": info.get("lines", []),
                    "coords": info.get("coords"), "incidents": int(row["incidents"]),
                    "delays": int(row["delays"]), "delay_min": int(row["delay_min"])})
    return out


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def r(x, n=1):
    return None if pd.isna(x) else round(float(x), n)


def affected_slots(delays: pd.DataFrame) -> pd.DataFrame:
    """One row per (line, hour-slot) during which a delay was in progress."""
    rows = []
    for line, s, e in zip(delays["line"], delays["start"], delays["end"]):
        h = s.floor("h")
        while h < e:
            rows.append((line, h))
            h += pd.Timedelta(hours=1)
    slots = pd.DataFrame(rows, columns=["line", "slot"]).drop_duplicates()
    slots["day"] = slots["slot"].dt.normalize()
    slots["hour"] = slots["slot"].dt.hour
    slots["wd"] = slots["slot"].dt.dayofweek
    return slots[slots["hour"].isin(SERVICE_HOURS)]


def line_stats(g: pd.DataFrame, n_slots_line: int, total_slots: int) -> dict:
    if g.empty:
        return {"incidents": 0, "avg_delay_min": None, "median_delay_min": None,
                "total_delay_min": 0, "pct_hours_delayed": 0.0, "worst": None, "top_cause": None}
    w = g.loc[g["minutes"].idxmax()]
    causes = g["cause_primary"].replace("", "Non précisée").value_counts(normalize=True)
    return {
        "incidents": int(len(g)),
        "avg_delay_min": r(g["minutes"].mean()),
        "median_delay_min": r(g["minutes"].median()),
        "total_delay_min": int(g["minutes"].sum()),
        "pct_hours_delayed": r(100 * n_slots_line / total_slots, 2),
        "worst": {
            "minutes": int(w["minutes"]),
            "date": w["day"].strftime("%Y-%m-%d"),
            "start": w["start"].strftime("%H:%M"),
            "cause": w["cause_primary"] or None,
            "detail": w["cause_secondary"] or None,
            "location": w["location"].title() if w["location"] not in ("", "#") else None,
        },
        "top_cause": {"name": causes.index[0], "share_pct": r(100 * causes.iloc[0])},
    }


def build_outputs(delays: pd.DataFrame, stations: pd.DataFrame, idx: dict,
                  data_through: str | None) -> tuple[dict, dict, dict]:
    last_month = pd.Period(data_through or delays["month"].max(), "M")
    delays = delays[delays["month"] <= str(last_month)]
    stations = stations[stations["month"] <= str(last_month)]
    win_end = last_month.end_time.normalize()
    win_start = (last_month - (WINDOW_MONTHS - 1)).start_time
    win = delays[delays["day"].between(win_start, win_end)]
    st_win = stations[stations["day"].between(win_start, win_end)]

    days = pd.date_range(win_start, win_end, freq="D")
    per_wd = pd.Series(days.dayofweek).value_counts().reindex(range(7), fill_value=0)
    total_slots = len(days) * len(SERVICE_HOURS)

    slots = affected_slots(win)
    slots = slots[slots["day"].between(win_start, win_end)]
    any_slots = slots.drop_duplicates("slot")

    # ---- P(delay) by weekday x hour -----------------------------------------
    def grid(s: pd.DataFrame) -> list[list[float]]:
        c = s.groupby(["wd", "hour"]).size()
        return [[r(c.get((wd, h), 0) / per_wd[wd], 4) if per_wd[wd] else None
                 for h in SERVICE_HOURS] for wd in range(7)]

    likelihood = {
        "hours": SERVICE_HOURS,
        "weekdays": WEEKDAYS,
        "window": {"start": win_start.strftime("%Y-%m-%d"), "end": win_end.strftime("%Y-%m-%d")},
        "definition": f"Share of past {WINDOW_MONTHS} months' days on which a delay of "
                      f"{MIN_DELAY}+ min was in progress during that hour",
        "lines": {k: grid(slots[slots["line"] == k]) for k in LINES},
        "any": grid(any_slots),
    }

    # ---- per-line + global stats over the window ----------------------------
    lines = {}
    for k, (label, fr, color) in LINES.items():
        lines[k] = {"label": label, "name_fr": fr, "color": color,
                    **line_stats(win[win["line"] == k], int((slots["line"] == k).sum()), total_slots)}

    # ---- monthly table over the full history (for the ranking selector) -----
    monthly = []
    st_by_month = dict(tuple(stations.groupby("month")))
    for month, g in delays.groupby("month"):
        per = {}
        for k in LINES:
            gl = g[g["line"] == k]
            per[k] = {"incidents": int(len(gl)), "total_delay_min": int(gl["minutes"].sum()),
                      "avg_delay_min": r(gl["minutes"].mean())}
        top3 = sorted(LINES, key=lambda k: (-per[k]["total_delay_min"], -per[k]["incidents"]))[:3]
        sg = st_by_month.get(month)
        gu = g.drop_duplicates("incident_id")       # a multi-line incident counts once network-wide
        monthly.append({"month": month, "incidents": int(len(gu)),
                        "avg_delay_min": r(gu["minutes"].mean()), "lines": per, "top3": top3,
                        "top3_stations": rank_stations(sg, idx) if sg is not None else []})

    win_u = win.drop_duplicates("incident_id")
    stats = {
        "window": likelihood["window"] | {"months": WINDOW_MONTHS},
        "min_delay_min": MIN_DELAY,
        "latest_month": str(last_month),
        "global": {
            "incidents": int(len(win_u)),
            "avg_delay_min": r(win_u["minutes"].mean()),
            "median_delay_min": r(win_u["minutes"].median()),
            "pct_hours_delayed_any_line": r(100 * len(any_slots) / total_slots, 2),
        },
        "lines": lines,
        # every station over the window (for sizing/colouring station dots on the map)
        "stations": [x for x in rank_stations(st_win, idx, n=None) if x["coords"]],
        "station_rank_by": STATION_RANK_BY,
        "monthly": monthly,
    }
    return stats, likelihood, {"window": stats["window"], "latest_month": str(last_month)}


def write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> None:
    if not LATEST.exists():
        raise SystemExit(f"{LATEST} not found. Run pipeline/fetch.py first.")
    latest, data_through = read_stm_csv(LATEST)
    print(f"Latest file: {len(latest):,} rows, data through {data_through or 'unknown'}")

    history, prev_rows = merge_history(latest)
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    history.to_csv(HISTORY, index=False, compression={"method": "gzip", "mtime": 0})
    print(f"History: {prev_rows:,} -> {len(history):,} rows")

    all_rows, delays, issues = enrich(history)
    idx = load_station_index()
    if not idx:
        print("WARNING: no station geometry; run `python pipeline/fetch.py --geo` for map matching")
    stations, st_diag = station_frame(all_rows, idx)
    issues |= st_diag
    print(f"Delays >= {MIN_DELAY} min: {delays['incident_id'].nunique():,} incidents "
          f"({len(delays):,} incident-line pairs; {issues['multi_line_incidents']} multi-line) | station incidents: {len(stations):,} "
          f"(name match {st_diag['station_match_pct']}%)")
    for k in ("unknown_line_values", "unmatched_station_names", "map_stations_without_incidents",
              "top_non_station_locations"):
        if issues[k]:
            print(f"  {k}: {issues[k]}")
    stats, likelihood, win = build_outputs(delays, stations, idx, data_through)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prev_meta = {}
    if (OUT_DIR / "meta.json").exists():
        prev_meta = json.loads((OUT_DIR / "meta.json").read_text(encoding="utf-8"))
    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": SOURCE_URL,
        "data_through": data_through,
        "previous_data_through": prev_meta.get("data_through"),
        "latest_rows": len(latest),
        "history_rows": len(history),
        "previous_history_rows": prev_rows,
        "history_span": [history["date"].min(), history["date"].max()],
        "delay_incidents_total": int(delays["incident_id"].nunique()),
        "issues": issues,
        "min_delay_min": MIN_DELAY,
        **win,
    }
    write_json(OUT_DIR / "stats.json", stats)
    write_json(OUT_DIR / "likelihood.json", likelihood)
    write_json(OUT_DIR / "meta.json", meta)
    g = stats["global"]
    print(f"Window {stats['window']['start']} → {stats['window']['end']}: "
          f"{g['incidents']:,} delays, avg {g['avg_delay_min']} min, "
          f"{g['pct_hours_delayed_any_line']}% of service hours affected (any line)")
    last = stats["monthly"][-1]
    print(f"{last['month']}: top lines {last['top3']} | top stations "
          f"{[(x['name'], x['incidents']) for x in last['top3_stations']]}")


if __name__ == "__main__":
    main()