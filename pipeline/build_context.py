#!/usr/bin/env python3
"""
Builds data/ai_context.json — the input to the foresight panel.

The current dashboard hands the model ~21 lines of "name: price, day%=x".
This assembles the things a weak signal is actually visible in:

  levels + multi-horizon change   what is moving, and over what timescale
  5y percentile                   is this level unusual, or just noisy
  cross-asset divergences         computed, not guessed at by the model
  regime markers                  curve shape, credit stress, real rates
  data quality notes              which series are stale, so the model
                                  does not read a frozen number as a signal

Run after fetch.py.
"""

from __future__ import annotations

import json
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_all() -> dict[str, dict]:
    out = {}
    for f in DATA.glob("*.json"):
        rec = json.loads(f.read_text())
        if not (isinstance(rec, dict) and "label" in rec and "points" in rec):
            continue
        out[f.stem] = rec
    return out


def divergences(S: dict) -> list[dict]:
    """Pairs that normally move together. When they don't, that is the signal."""
    pairs = [
        ("eq_us_sp500", "eq_eu_stoxx600_eur", "US vs European equities"),
        ("eq_nordic_finland_eur", "eq_eu_stoxx600_eur", "Finland vs broad Europe"),
        ("rate_us_10y", "eq_us_sp500", "Long rates vs equities"),
        ("rate_us_hy_spread", "eq_us_sp500", "Credit spreads vs equities"),
        ("cmd_copper", "cmd_gold", "Copper vs gold (growth vs fear)"),
        ("cmd_crude_brent", "eq_eu_stoxx600_eur", "Energy vs European equities"),
    ]
    out = []
    for a, b, label in pairs:
        A, B = S.get(a), S.get(b)
        if not A or not B:
            continue
        ca, cb = A["chg"].get("1m"), B["chg"].get("1m")
        if ca is None or cb is None:
            continue
        gap = round(ca - cb, 2)
        if abs(gap) >= 3:                     # 3pp over a month is worth a look
            out.append({
                "pair": label,
                "gap_1m_pp": gap,
                "detail": f"{A['label']} {ca:+.1f}% vs {B['label']} {cb:+.1f}%",
            })
    return sorted(out, key=lambda d: -abs(d["gap_1m_pp"]))


def regime(S: dict) -> dict:
    def last(k):
        return (S.get(k) or {}).get("last")

    y2, y10, hy = last("rate_us_2y"), last("rate_us_10y"), last("rate_us_hy_spread")
    return {
        "curve_10y_2y": round(y10 - y2, 2) if y2 and y10 else None,
        "curve_state": (None if not (y2 and y10)
                        else "inverted" if y10 < y2 else "positive"),
        "hy_oas": hy,
        "credit_state": (None if hy is None
                         else "stressed" if hy > 5.0
                         else "tight" if hy < 3.5 else "normal"),
    }


def extremes(S: dict) -> list[dict]:
    """Anything sitting in the tails of its own 5y range."""
    out = []
    for s in S.values():
        p = s.get("pctile_5y")
        if p is None:
            continue
        if p >= 95 or p <= 5:
            out.append({"label": s["label"], "pctile_5y": p, "last": s["last"]})
    return sorted(out, key=lambda d: abs(d["pctile_5y"] - 50), reverse=True)


def main() -> None:
    S = load_all()
    stale = [s["label"] for s in S.values() if s.get("stale")]

    ctx = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "series": [
            {
                "label": s["label"],
                "category": s.get("category"),
                "last": s["last"],
                "units": s.get("units"),
                "chg": s["chg"],
                "pctile_5y": s.get("pctile_5y"),
                "stale": s.get("stale", False),
            }
            for s in sorted(S.values(), key=lambda x: (x.get("category") or "", x["label"]))
        ],
        "regime": regime(S),
        "divergences": divergences(S),
        "extremes_5y": extremes(S),
        "data_quality": {
            "stale_series": stale,
            "note": ("Series marked stale are last-known values, not current. "
                     "Do not treat a flat stale series as a signal."),
        },
    }

    (DATA / "ai_context.json").write_text(json.dumps(ctx, indent=2))
    print(f"ai_context.json — {len(S)} series, "
          f"{len(ctx['divergences'])} divergences, "
          f"{len(ctx['extremes_5y'])} at 5y extremes, "
          f"{len(stale)} stale")


if __name__ == "__main__":
    main()
