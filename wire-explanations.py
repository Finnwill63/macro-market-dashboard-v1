#!/usr/bin/env python3
"""Wire explanations.yaml into the pipeline. Safe to run twice."""
from pathlib import Path

# ---- fetch.py: load the file and attach to each series record ----
p = Path("pipeline/fetch.py"); s = p.read_text()

if "EXPLAIN" not in s:
    s = s.replace(
        'MANIFEST = ROOT / "series-manifest.yaml"',
        'MANIFEST = ROOT / "series-manifest.yaml"\nEXPLAIN = ROOT / "explanations.yaml"')
    s = s.replace(
        'def load_previous(sid):',
        'def load_explanations():\n'
        '    """Optional file. Missing or malformed means no explanations, not a failure."""\n'
        '    try:\n'
        '        return yaml.safe_load(EXPLAIN.read_text()) or {}\n'
        '    except Exception as e:\n'
        '        print(f"  (no explanations: {e})", file=sys.stderr)\n'
        '        return {}\n\n\n'
        'EXPLANATIONS = {}\n\n\n'
        'def load_previous(sid):')
    s = s.replace(
        '        "note": meta.get("note"),',
        '        "note": meta.get("note"),\n'
        '        "explain": (EXPLANATIONS.get("series") or {}).get(meta["id"]),')
    s = s.replace(
        '    manifest = yaml.safe_load(MANIFEST.read_text())\n    DATA.mkdir(exist_ok=True)',
        '    manifest = yaml.safe_load(MANIFEST.read_text())\n'
        '    global EXPLANATIONS\n    EXPLANATIONS = load_explanations()\n'
        '    DATA.mkdir(exist_ok=True)')
    p.write_text(s); print("fetch.py wired")
else:
    print("fetch.py already wired")

# ---- score_scenarios.py: attach to each driver + the method note ----
p = Path("pipeline/score_scenarios.py"); s = p.read_text()

if "EXPLAIN" not in s:
    s = s.replace('SPEC = ROOT / "scenario-spec.yaml"',
                  'SPEC = ROOT / "scenario-spec.yaml"\nEXPLAIN = ROOT / "explanations.yaml"')
    s = s.replace('def score(name: str, cfg: dict, banding: dict, override: dict | None) -> dict:\n'
                  '    series = build_measure(cfg["measure"])',
                  'def score(name: str, cfg: dict, banding: dict, override: dict | None,\n'
                  '          explain: dict | None = None) -> dict:\n'
                  '    series = build_measure(cfg["measure"])')
    s = s.replace('"weak": cfg.get("weak", False), "bears_on": cfg.get("bears_on", [])}',
                  '"weak": cfg.get("weak", False), "bears_on": cfg.get("bears_on", []),\n'
                  '              "explain": (explain or {}).get(name)}')
    s = s.replace('    scored = [score(n, cfg, banding, overrides.get(n))\n'
                  '              for n, cfg in spec["drivers"].items()]',
                  '    try:\n'
                  '        ex = yaml.safe_load(EXPLAIN.read_text()) or {}\n'
                  '    except Exception:\n'
                  '        ex = {}\n'
                  '    scored = [score(n, cfg, banding, overrides.get(n), ex.get("drivers"))\n'
                  '              for n, cfg in spec["drivers"].items()]')
    s = s.replace('        "caveat": (', '        "how_to_read": ex.get("method"),\n        "caveat": (')
    p.write_text(s); print("score_scenarios.py wired")
else:
    print("score_scenarios.py already wired")
