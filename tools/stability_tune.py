#!/usr/bin/env python3
"""Correlate bench request fate with the assigned node-quality vector, and
suggest ADR-0009 weight movement from measured separation.

Reads the results bundle produced by tools/prod_bench.py (results.json plus
the stability.correlated rows it embeds), joins each request to the latest
node sample of its tunnel, then buckets success on tier-a share and mean
score. The recommendation is deliberately conservative: with a pool that is
mostly low-probe nodes, no signal is a real outcome and the correct tune is
"keep defaults".

Run from the repo root after a bench, e.g.:
    python3 tools/stability_tune.py --outdir bench_out --live nodes.json
    python3 tools/stability_tune.py --outdir bench_out

"--live" may point at a GET /nodes snapshot to describe the pool the bench ran
against (tier distribution, mean probe_total, mean throughput).
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

BANDS = [
    (0.0, 0.0, "tier_a_share == 0"),
    (0.0, 0.5, "0 < tier_a_share <= 0.5"),
    (0.5, 1.0, "0.5 < tier_a_share <= 1"),
]


def _rate(pairs: list[tuple[float, bool]]) -> tuple[int, int, float]:
    n = len(pairs)
    ok = sum(1 for _, s in pairs if s)
    return n, ok, 100.0 * ok / n if n else 0.0


def _band(
    pairs: list[tuple[float, bool]], lo: float, hi: float
) -> tuple[int, int, float]:
    sel = (
        [p for p in pairs if p[0] == 0.0]
        if hi == 0.0
        else [p for p in pairs if lo < p[0] <= hi]
    )
    return _rate(sel)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def analyze(results: dict) -> dict:
    correlated = results.get("stability", {}).get("correlated", [])
    if not correlated:
        return {"error": "no correlated rows (bench did not capture probe_history)"}

    a: dict = {
        "requests": len(correlated),
        "ok": sum(1 for c in correlated if c["success"]),
        "tier_a_share": [],
        "mean_score_quartiles": [],
        "pearson_mean_score": None,
        "recommendation": [],
    }

    tiers = [(c["tier_a_share"], c["success"]) for c in correlated]
    for lo, hi, label in BANDS:
        n, ok, pct = _band(tiers, lo, hi)
        a["tier_a_share"].append({"band": label, "ok": ok, "total": n, "rate_pct": pct})

    scored = [
        (c["mean_score"], c["success"])
        for c in correlated
        if c.get("mean_score") is not None
    ]
    if scored:
        a["pearson_mean_score"] = _pearson(
            [s for s, _ in scored], [1.0 if ok else 0.0 for _, ok in scored]
        )
        ordered = sorted(scored, key=lambda t: t[0])
        block = max(len(ordered) // 4, 1)
        for i in range(4):
            band = ordered[i * block : (i + 1) * block if i < 3 else None]
            n, ok, pct = _rate(band)
            a["mean_score_quartiles"].append(
                {"q": i, "ok": ok, "total": n, "rate_pct": pct}
            )

    recommend(a)
    return a


def recommend(a: dict) -> None:
    if a["ok"] == 0:
        a["recommendation"] = [
            "zero successes - pool quality dominates; keep defaults",
            "re-run once the live engine has accumulated probe history",
        ]
        return
    rates = [row["rate_pct"] for row in a["tier_a_share"]]
    separated = any(
        rates[i + 1] >= 15.0 and rates[i] < 5.0 for i in range(len(rates) - 1)
    )  # noqa: PLR2004
    pearson = a["pearson_mean_score"]
    if separated and pearson is not None and pearson >= 0.2:
        a["recommendation"] = [
            "tier/score separates request fate; sharpen weight_avail "
            "and/or floor min_avail",
        ]
    elif separated:
        a["recommendation"] = [
            "tier-a share separates ok-rate but score does not; "
            "move the floor, not the weights",
        ]
    else:
        a["recommendation"] = [
            "no measured separation at these request volumes; keep defaults",
        ]


def render(a: dict) -> str:
    if "error" in a:
        return f"# stability tuning analysis\n- {a['error']}\n"
    lines = [
        "# stability tuning analysis",
        f"evaluable requests: {a['requests']} (ok {a['ok']}, "
        f"{100.0 * a['ok'] / a['requests'] if a['requests'] else 0:.1f}%)",
    ]
    pool = a.get("pool")
    if pool:
        lines.append(
            f"pool: {pool['nodes']} nodes, tier a/b/cold = "
            f"{pool['tier_a']}/{pool['tier_b']}/{pool['tier_cold']}, "
            f"mean probe_total {pool['mean_probe_total']}, "
            f"mean throughput {pool['mean_throughput_kb_s']} KiB/s"
        )
    lines.append("")
    lines.append("ok-rate by tier-a share:")
    for row in a["tier_a_share"]:
        lines.append(
            f"  {row['band']:<23}: {row['ok']}/{row['total']} ({row['rate_pct']:.1f}%)"
        )
    lines.append("")
    lines.append("ok-rate by mean-score quartile:")
    for row in a["mean_score_quartiles"]:
        lines.append(
            f"  q{row['q']}: {row['ok']}/{row['total']} ({row['rate_pct']:.1f}%)"
        )
    pearson = a["pearson_mean_score"]
    lines.append(
        "pearson(mean_score, ok): " + ("n/a" if pearson is None else f"{pearson:.2f}")
    )
    lines.append("")
    lines.append("### recommendation")
    lines.extend(f"- {r}" for r in a["recommendation"])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", default="bench_out")
    parser.add_argument(
        "--live",
        default=None,
        help="optional GET /nodes snapshot JSON describing the pool under test",
    )
    args = parser.parse_args()

    results_path = Path(args.outdir) / "results.json"
    if not results_path.exists():
        raise SystemExit(f"no {results_path}; run tools/prod_bench.py first")
    results = json.loads(results_path.read_text())
    analysis = analyze(results)
    analysis["results_path"] = str(results_path)

    if args.live:
        live = json.loads(Path(args.live).read_text())
        nodes = live.get("nodes", live) if isinstance(live, dict) else live
        if isinstance(nodes, list) and nodes:
            tiers = [n.get("tier") for n in nodes]
            totals = [float(n.get("probe_total") or 0) for n in nodes]
            speeds = [
                float(n["throughput_kb_s"]) for n in nodes if n.get("throughput_kb_s")
            ]
            analysis["pool"] = {
                "nodes": len(nodes),
                "tier_a": tiers.count("A"),
                "tier_b": tiers.count("B"),
                "tier_cold": tiers.count("cold"),
                "mean_probe_total": round(statistics.fmean(totals), 1),
                "mean_throughput_kb_s": (
                    round(statistics.fmean(speeds), 1) if speeds else None
                ),
            }

    tune_path = Path(args.outdir) / "tune.md"
    tune_path.write_text(render(analysis))
    print(render(analysis))


if __name__ == "__main__":
    main()
