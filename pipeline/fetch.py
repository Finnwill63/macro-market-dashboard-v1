#!/usr/bin/env python3
"""
macro-market-dashboard data pipeline.

Reads series-manifest.yaml, fetches each series, writes one JSON file per
series into data/. The page reads those files; nothing is fetched at render
time.

Providers:
  fred        keyless CSV        rates, FX, oil, US indices, VIX
  ecb_sdw     keyless SDMX/CSV   policy rate, EUR crosses
  twelvedata  keyed, 8 req/min   European + Nordic equity ETFs, metals

Derived series are computed after all raw series are fetched — currently
fx_adjust, which converts a USD-denominated series into EUR terms using
the fetched EUR/USD rate. Raw and derived are both stored.

Failure policy: a series that fails keeps its last good value and is marked
stale. The job exits non-zero so GitHub emails you; the page stays up.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import datetime as dt
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "series-manifest.yaml"
EXPLAIN = ROOT / "explanations.yaml"
DATA = ROOT / "data"
UA = {"User-Agent": "macro-market-dashboard/1.0 (+github.com/Finnwill63)"}
TIMEOUT = 30
HISTORY_DAYS = 10 * 365
TODAY = dt.date.today()


def get(url: str) -> str:
    with urlopen(Request(url, headers=UA), timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_date(s: str) -> "dt.date | None":
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m"):
        try:
            return dt.datetime.strptime(s.strip()[:10], fmt).date()
        except ValueError:
            continue
    return None


def clean(points):
    cutoff = TODAY - dt.timedelta(days=HISTORY_DAYS)
    out = [(d, v) for d, v in points if d and v is not None and d >= cutoff]
    out.sort(key=lambda p: p[0])
    return [{"d": d.isoformat(), "v": round(v, 6)} for d, v in out]


# ----------------------------------------------------------------- providers

def fetch_fred(spec):
    sid = spec["series_id"]
    # FRED returns each series' DEFAULT GRAPH WINDOW unless given a start
    # date — for some series that is only 3 years. Always ask explicitly.
    start = (TODAY - dt.timedelta(days=HISTORY_DAYS + 400)).isoformat()
    rows = csv.DictReader(io.StringIO(get(
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}")))
    pts = []
    for row in rows:
        k = list(row.keys())
        raw = (row[k[1]] or "").strip()
        if raw in (".", "", "NA"):
            continue                      # FRED marks non-trading days with .
        pts.append((parse_date(row[k[0]]), float(raw)))
    if not pts:
        raise RuntimeError(f"fred returned no usable rows for {sid}")
    return clean(pts)


def fetch_ecb(spec):
    url = (f"https://data-api.ecb.europa.eu/service/data/{spec['key']}"
           f"?format=csvdata&detail=dataonly")
    rows = csv.DictReader(io.StringIO(get(url)))
    pts = [(parse_date(r["TIME_PERIOD"]), float(r["OBS_VALUE"]))
           for r in rows if r.get("OBS_VALUE") not in (None, "", "NaN")]
    if not pts:
        raise RuntimeError(f"ecb returned no rows for {spec['key']}")
    return clean(pts)


class RateLimiter:
    """Twelve Data free tier allows 8 credits per minute. Stay under it."""

    def __init__(self, calls: int = 7, window: int = 60):
        self.calls, self.window, self.hits = calls, window, []

    def wait(self):
        now = time.monotonic()
        self.hits = [t for t in self.hits if now - t < self.window]
        if len(self.hits) >= self.calls:
            nap = self.window - (now - self.hits[0]) + 1
            print(f"    (rate limit: pausing {nap:.0f}s)", file=sys.stderr)
            time.sleep(nap)
            self.hits = []
        self.hits.append(time.monotonic())


TD_LIMIT = RateLimiter()


def fetch_twelvedata(spec):
    key = os.environ.get("TD_KEY", "")
    if not key:
        raise RuntimeError("TD_KEY not set")
    TD_LIMIT.wait()
    url = (f"https://api.twelvedata.com/time_series?symbol={spec['symbol']}"
           f"&interval=1day&outputsize=5000&apikey={key}")
    j = json.loads(get(url))
    if j.get("status") != "ok":
        raise RuntimeError(f"twelvedata: {str(j.get('message'))[:70]}")
    pts = [(parse_date(v["datetime"]), float(v["close"]))
           for v in j.get("values", []) if v.get("close")]
    if not pts:
        raise RuntimeError(f"twelvedata returned no values for {spec['symbol']}")
    return clean(pts)


PROVIDERS = {"fred": fetch_fred, "ecb_sdw": fetch_ecb, "twelvedata": fetch_twelvedata}


# ------------------------------------------------------------------ derived

def fx_adjust(base_points, fx_points, invert=False):
    """
    Convert a USD-priced series into EUR terms.

    fx is EUR/USD as USD-per-EUR (FRED DEXUSEU), so EUR value = USD / rate.
    Each base date uses the last FX observation on or before it, since FX
    and equity calendars differ on holidays.
    """
    if not base_points or not fx_points:
        return []
    fx = [(dt.date.fromisoformat(p["d"]), p["v"]) for p in fx_points]
    out, j = [], 0
    for p in base_points:
        d, v = dt.date.fromisoformat(p["d"]), p["v"]
        while j + 1 < len(fx) and fx[j + 1][0] <= d:
            j += 1
        if fx[j][0] > d or fx[j][1] == 0:
            continue
        rate = fx[j][1]
        out.append({"d": p["d"], "v": round(v * rate if invert else v / rate, 6)})
    return out


# ---------------------------------------------------------------------- main

def load_explanations():
    """Optional file. Missing or malformed means no explanations, not a failure."""
    try:
        return yaml.safe_load(EXPLAIN.read_text()) or {}
    except Exception as e:
        print(f"  (no explanations: {e})", file=sys.stderr)
        return {}


EXPLANATIONS = {}


def load_previous(sid):
    f = DATA / f"{sid}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except json.JSONDecodeError:
            return None
    return None


def pct_change(points, days):
    if len(points) < 2:
        return None
    last = points[-1]
    target = dt.date.fromisoformat(last["d"]) - dt.timedelta(days=days)
    prior = [p for p in points if dt.date.fromisoformat(p["d"]) <= target]
    if not prior or prior[-1]["v"] == 0:
        return None
    return round((last["v"] / prior[-1]["v"] - 1) * 100, 2)


def percentile(points, years=5):
    if len(points) < 30:
        return None
    cutoff = TODAY - dt.timedelta(days=365 * years)
    vals = [p["v"] for p in points if dt.date.fromisoformat(p["d"]) >= cutoff]
    if len(vals) < 30:
        return None
    return round(100 * sum(1 for v in vals if v <= points[-1]["v"]) / len(vals), 1)


def enrich(meta, points):
    last = points[-1] if points else None
    return {
        "id": meta["id"],
        "label": meta["label"],
        "category": meta.get("category"),
        "units": meta.get("units", "index"),
        "cadence": meta.get("cadence", "weekday"),
        "note": meta.get("note"),
        "explain": (EXPLANATIONS.get("series") or {}).get(meta["id"]),
        "last": last["v"] if last else None,
        "last_date": last["d"] if last else None,
        "chg": {"1d": pct_change(points, 1), "1w": pct_change(points, 7),
                "1m": pct_change(points, 30), "3m": pct_change(points, 90),
                "12m": pct_change(points, 365)},
        "pctile_5y": percentile(points),
        "points": points,
    }


def resolve_sources(s):
    out = []
    for block in ("primary", "fallback"):
        spec = s.get(block) or {}
        if spec.get("provider") in PROVIDERS:
            out.append(spec)
    if not out:
        print(f"  ! {s['id']}: no usable provider in manifest", file=sys.stderr)
    return out


def main():
    manifest = yaml.safe_load(MANIFEST.read_text())
    global EXPLANATIONS
    EXPLANATIONS = load_explanations()
    DATA.mkdir(exist_ok=True)
    status, failures, store = [], [], {}

    for s in manifest["series"]:
        sid = s["id"]
        points, used, err = None, None, None

        for spec in resolve_sources(s):
            try:
                points = PROVIDERS[spec["provider"]](spec)
                used = spec["provider"]
                break
            except (URLError, HTTPError, RuntimeError, ValueError, KeyError) as e:
                err = f"{spec['provider']}: {e}"
                print(f"  ! {sid} via {spec['provider']} — {e}", file=sys.stderr)

        if points:
            rec = enrich(s, points)
            rec.update(source=used, stale=False, updated=TODAY.isoformat())
            status.append({"id": sid, "state": "ok", "source": used})
            print(f"  ✓ {sid:26s} {used:11s} {rec['last']}")
        else:
            prev = load_previous(sid)
            if not prev:
                failures.append(sid)
                status.append({"id": sid, "state": "missing", "error": err})
                print(f"  ✗ {sid:26s} no data and no cache", file=sys.stderr)
                continue
            rec = prev
            since = (TODAY - dt.date.fromisoformat(
                prev.get("updated", TODAY.isoformat()))).days
            rec.update(stale=True, stale_days=since, error=err)
            failures.append(sid)
            status.append({"id": sid, "state": "stale", "days": since})
            print(f"  ~ {sid:26s} stale {since}d — kept last good value",
                  file=sys.stderr)

        store[sid] = rec
        (DATA / f"{sid}.json").write_text(json.dumps(rec, separators=(",", ":")))

    # ---- derived series, computed from what we just fetched ----
    for d in manifest.get("derived", []):
        did, base, using = d["id"], store.get(d["from"]), store.get(d["using"])
        if not base or not using or not base.get("points"):
            print(f"  ✗ {did:26s} needs {d['from']} + {d['using']}",
                  file=sys.stderr)
            status.append({"id": did, "state": "missing"})
            failures.append(did)
            continue
        pts = fx_adjust(base["points"], using["points"], d.get("invert", False))
        rec = enrich(d, pts)
        rec.update(source=f"derived from {d['from']}", stale=base.get("stale", False),
                   updated=TODAY.isoformat())
        store[did] = rec
        (DATA / f"{did}.json").write_text(json.dumps(rec, separators=(",", ":")))
        status.append({"id": did, "state": "ok", "source": "derived"})
        print(f"  ✓ {did:26s} {'derived':11s} {rec['last']}")

    ok = sum(1 for r in status if r["state"] == "ok")
    (DATA / "status.json").write_text(json.dumps({
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "ok": ok, "total": len(status), "series": status}, indent=2))

    print(f"\n{ok}/{len(status)} series fresh")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
