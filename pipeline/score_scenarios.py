#!/usr/bin/env python3
"""
Scores each scenario driver into a percentile band and writes
data/scenario_state.json.

Percentiles are computed over the driver's own measure — a level, a rolling
change, or a spread between two series — across the configured window.
Thresholds are converted back into native units so the output can say
"32bp from bear" rather than "at the 74th percentile".

Run after fetch.py, before build_context.py.
"""

from __future__ import annotations

import json
import datetime as dt
from pathlib import Path
from statistics import quantiles

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SPEC = ROOT / "scenario-spec.yaml"
EXPLAIN = ROOT / "explanations.yaml"


# ------------------------------------------------------------ measure series

def load(sid: str) -> list[tuple[dt.date, float]]:
    f = DATA / f"{sid}.json"
    if not f.exists():
        return []
    rec = json.loads(f.read_text())
    return [(dt.date.fromisoformat(p["d"]), p["v"]) for p in rec.get("points", [])]


def as_level(sid: str) -> list[tuple[dt.date, float]]:
    return load(sid)


def as_change(sid: str, horizon_days: int) -> list[tuple[dt.date, float]]:
    """Rolling percent change over the horizon, computed at every date."""
    pts = load(sid)
    if len(pts) < 2:
        return []
    out, j = [], 0
    for i, (d, v) in enumerate(pts):
        target = d - dt.timedelta(days=horizon_days)
        while j + 1 < i and pts[j + 1][0] <= target:
            j += 1
        base_d, base_v = pts[j]
        if base_d > target or base_v == 0:
            continue
        out.append((d, (v / base_v - 1) * 100))
    return out


def as_spread(a: str, b: str, scale: float) -> list[tuple[dt.date, float]]:
    """A minus B, aligned on B's last known value at or before A's date."""
    A, B = load(a), load(b)
    if not A or not B:
        return []
    out, j = [], 0
    for d, v in A:
        while j + 1 < len(B) and B[j + 1][0] <= d:
            j += 1
        if B[j][0] <= d:
            out.append((d, (v - B[j][1]) * scale))
    return out


HORIZONS = {"1m": 30, "3m": 90, "6m": 182, "12m": 365}


def build_measure(m: dict) -> list[tuple[dt.date, float]]:
    t = m.get("type")
    if t == "level":
        return as_level(m["series"])
    if t == "change":
        return as_change(m["series"], HORIZONS[m.get("horizon", "12m")])
    if t == "spread":
        return as_spread(m["series"], m["minus"], m.get("scale", 1))
    raise ValueError(f"unknown measure type: {t}")


# ------------------------------------------------------------------ scoring

def score(name: str, cfg: dict, banding: dict, override: dict | None,
          explain: dict | None = None) -> dict:
    series = build_measure(cfg["measure"])
    result = {"driver": name, "label": cfg["label"], "units": cfg.get("units"),
              "weak": cfg.get("weak", False), "bears_on": cfg.get("bears_on", []),
              "explain": (explain or {}).get(name)}

    if not series:
        return {**result, "band": "no_data",
                "note": "underlying series missing — has it been added to the manifest?"}

    cutoff = dt.date.today() - dt.timedelta(days=365 * banding["window_years"])
    windowed = [(d, v) for d, v in series if d >= cutoff]
    window = [v for _, v in windowed]
    years = ((windowed[-1][0] - windowed[0][0]).days / 365.25) if len(windowed) > 1 else 0

    if years < banding["min_window_years"] or len(window) < 24:
        return {**result, "band": "insufficient_history",
                "value": round(series[-1][1], 3),
                "years_available": round(years, 1)}

    lo_p, hi_p = banding["thresholds"]["low"], banding["thresholds"]["high"]
    cuts = quantiles(window, n=100, method="inclusive")
    lo_v, hi_v = cuts[lo_p - 1], cuts[hi_p - 1]

    value = series[-1][1]
    pct = round(100 * sum(1 for v in window if v <= value) / len(window), 1)

    # which tail is bullish
    if cfg["higher_is"] == "bull":
        band = "bull" if value >= hi_v else "bear" if value <= lo_v else "base"
        bear_edge, bull_edge = lo_v, hi_v
    else:
        band = "bear" if value >= hi_v else "bull" if value <= lo_v else "base"
        bear_edge, bull_edge = hi_v, lo_v

    # an absolute override always wins
    ovr, ovr_hit = (override or {}), False
    if "bear_above" in ovr and value >= ovr["bear_above"]:
        band, ovr_hit = "bear", True
    if "bear_below" in ovr and value <= ovr["bear_below"]:
        band, ovr_hit = "bear", True

    return {**result,
            "value": round(value, 3),
            "percentile": pct,
            "band": band,
            "override_applied": ovr_hit,
            "window_years": round(years, 1),
            "edges": {"bear_at": round(bear_edge, 3), "bull_at": round(bull_edge, 3)},
            "distance_to_bear": round(abs(value - bear_edge), 3),
            "distance_to_bull": round(abs(value - bull_edge), 3),
            "as_of": series[-1][0].isoformat()}


def main() -> None:
    spec = yaml.safe_load(SPEC.read_text())
    banding = spec["banding"]
    overrides = spec.get("absolute_override") or {}

    try:
        ex = yaml.safe_load(EXPLAIN.read_text()) or {}
    except Exception:
        ex = {}
    scored = [score(n, cfg, banding, overrides.get(n), ex.get("drivers"))
              for n, cfg in spec["drivers"].items()]

    live = [s for s in scored if s["band"] in ("bull", "base", "bear")]
    mix = {b: sum(1 for s in live if s["band"] == b) for b in ("bull", "base", "bear")}

    # closest to flipping into bear, weak drivers deprioritised
    watch = sorted((s for s in live if s["band"] != "bear"),
                   key=lambda s: (s["weak"], s["distance_to_bear"]))[:2]

    state = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "method": f"percentile bands, {banding['window_years']}y window, "
                  f"{banding['thresholds']['low']}/{banding['thresholds']['high']} cuts",
        "mix": mix,
        "headline": f"{mix['base']} base, {mix['bear']} bear, {mix['bull']} bull",
        "flip_watch": [
            {"driver": s["label"],
             "band": s["band"],
             "value": s["value"],
             "units": s["units"],
             "to_bear": s["distance_to_bear"]}
            for s in watch
        ],
        "drivers": scored,
        "how_to_read": ex.get("method"),
        "caveat": ("Bands are relative to each driver's own history over the "
                   "window. They indicate unusual versus recent, not good "
                   "versus bad. Drivers with an absolute override applied are "
                   "flagged."),
    }

    (DATA / "scenario_state.json").write_text(json.dumps(state, indent=2))
    print(state["headline"])
    for s in scored:
        if s["band"] in ("no_data", "insufficient_history"):
            print(f"  ! {s['label']}: {s['band']}")
        else:
            print(f"  {s['label']:28s} {s['band']:5s} "
                  f"p{s['percentile']:<5} {s['value']}")


if __name__ == "__main__":
    main()
