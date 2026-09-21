"""Calibrate `RETRIEVAL_MIN_SCORE` — the cosine floor that decides whether a retrieved row is
offered to the agent as grounding, or dropped so the agent abstains instead.

The default (0.55) is a guess. The right value is a property of YOUR corpus and embedding
model, so measure it: run queries the data should answer and queries it should not, and look
for a gap between the two score distributions.

    python -m scripts.calibrate_retrieval_floor
    python -m scripts.calibrate_retrieval_floor --preset jobs

Requires a seeded Atlas cluster and (for foundry/voyage) embedding credentials — same
as `-m integration`. Under EMBEDDING_PROVIDER=atlas_auto, Atlas embeds query text
server-side, so no embedding credentials are needed (specs/002).
Read-only — issues `$vectorSearch`/`$search` queries and one embedding call per query.
"""
from __future__ import annotations

import argparse
import sys

from src.retrieval import PRESETS, hybrid_search, resolve_min_score
from src.settings import get_settings

# Queries the seeded corpus SHOULD be able to ground, and queries it should NOT.
PROBES: dict[str, dict[str, list[str]]] = {
    "jobs": {
        "on": ["senior python backend engineer, remote",
               "machine learning role at a startup",
               "frontend developer react"],
        "off": ["pediatric nursing night shift",
                "commercial truck driver class A license",
                "how do I bake sourdough bread"],
    },
    "companies": {
        "on": ["fintech company culture and funding",
               "large cloud infrastructure employer",
               "early stage startup engineering team"],
        "off": ["nearest veterinary clinic opening hours",
                "1970s progressive rock discography",
                "municipal parking regulations"],
    },
    "interview_prep": {
        "on": ["system design interview questions",
               "behavioral questions about conflict",
               "python coding interview"],
        "off": ["what temperature to roast a chicken",
                "rules of cricket lbw",
                "how to change a bicycle tyre"],
    },
}


def _top_scores(query: str, preset: str) -> tuple[float, int, int]:
    """Return (best cosine score, n hits, n BM25-only hits) for one probe query."""
    hits = hybrid_search(query, PRESETS[preset], top_n=5)
    vs = [float(h["_vs_score"]) for h in hits if "_vs_score" in h]
    bm25_only = sum(1 for h in hits if "_vs_score" not in h)
    return (max(vs) if vs else 0.0), len(hits), bm25_only


def calibrate(preset: str) -> tuple[float, float, int]:
    print(f"\n=== {preset} " + "=" * (60 - len(preset)))
    results: dict[str, list[float]] = {"on": [], "off": []}
    bm25_leaks = 0
    for kind in ("on", "off"):
        for q in PROBES[preset][kind]:
            score, n, bm25 = _top_scores(q, preset)
            results[kind].append(score)
            if kind == "on":
                bm25_leaks += bm25
            flag = "  <-- recall dropped by the floor" if (kind == "on" and bm25) else ""
            print(f"  [{kind:3}] {score:.3f}  ({n} hits, {bm25} bm25-only)  {q[:44]}{flag}")

    worst_on = min(results["on"]) if results["on"] else 0.0
    best_off = max(results["off"]) if results["off"] else 0.0
    print(f"\n  worst on-corpus : {worst_on:.3f}   <- floor must sit BELOW this (else you abstain"
          " on answerable questions)")
    print(f"  best off-corpus : {best_off:.3f}   <- floor must sit ABOVE this (else you ground"
          " on noise)")
    if worst_on > best_off:
        print(f"  ✅ clean separation — suggested RETRIEVAL_MIN_SCORE="
              f"{(worst_on + best_off) / 2:.2f}")
    else:
        print("  ⚠️  distributions OVERLAP — no single floor separates them. Either the probe "
              "queries are wrong for this corpus, or the embeddings do not discriminate here.")
    if bm25_leaks:
        print(f"  ⚠️  {bm25_leaks} on-corpus hit(s) were found by BM25 only (no cosine score) and "
              "are therefore dropped by the floor — keyword recall you are giving up. The agent "
              "can still reach them via its own MongoDB MCP queries.")
    return worst_on, best_off, bm25_leaks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preset", choices=sorted(PROBES), help="calibrate one preset only")
    args = ap.parse_args()

    provider = get_settings().embedding_provider
    current = resolve_min_score()
    override = get_settings().retrieval_min_score
    print(f"embedding provider = {provider}")
    print(f"current floor = {current if current is not None else 'DISABLED (uncalibrated provider)'}"
          + ("  (RETRIEVAL_MIN_SCORE override)" if override is not None else "  (provider default)"))
    presets = [args.preset] if args.preset else sorted(PROBES)
    bounds = {p: calibrate(p)[:2] for p in presets}

    # RETRIEVAL_MIN_SCORE is ONE global knob, so it has to satisfy every preset simultaneously.
    # Per-preset separation is not enough: a floor tuned on `jobs` can silently starve
    # `interview_prep`, whose scores run lower.
    lo_name, lo = max(((p, b[1]) for p, b in bounds.items()), key=lambda kv: kv[1])
    hi_name, hi = min(((p, b[0]) for p, b in bounds.items()), key=lambda kv: kv[1])
    print(f"\n=== global floor ({len(bounds)} preset(s)) " + "=" * 26)
    print(f"  must sit ABOVE {lo:.3f}  (best off-corpus, from `{lo_name}`)")
    print(f"  must sit BELOW {hi:.3f}  (worst on-corpus, from `{hi_name}`)")

    if lo >= hi:
        print(f"\n  ❌ NO single floor works for all presets — `{lo_name}` needs a floor above "
              f"what `{hi_name}` can tolerate. Make the floor per-preset (see `_relevant` in "
              "src/orchestrator.py).")
        return 1

    suggested = round((lo + hi) / 2, 2)
    if current is None:
        print(f"\n  ℹ️  no floor is active for provider `{provider}`. Suggested "
              f"RETRIEVAL_MIN_SCORE={suggested} (measure again on YOUR corpus — this run "
              "used the demo probes).")
        return 0
    if not lo < current < hi:
        side = "too permissive — grounds on noise" if current <= lo else \
               "too strict — abstains on answerable questions"
        print(f"\n  ❌ current {current} is OUTSIDE the window ({side}).")
        print(f"     set RETRIEVAL_MIN_SCORE={suggested}")
        return 1

    margin = min(current - lo, hi - current)
    print(f"\n  ✅ current {current} is inside the window (suggested {suggested}, "
          f"nearest edge {margin:.3f} away)")
    if hi - lo < 0.05:
        print(f"  ⚠️  the window is only {hi - lo:.3f} wide — a single global floor is fragile "
              "here. Per-preset floors would give each collection real headroom.")
    if args.preset:
        print("  note: --preset checked ONE collection; the global knob must satisfy them all.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
