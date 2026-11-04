"""Follow-up analysis (exploratory, not pre-registered): neutral-prompt length sweep, other models, internals v2.

python3 src/followup.py  -> results/followup/{sweep.csv, models.csv, followup.md, fig_*.png}
Every part is skipped cleanly when its data is not there yet.
"""
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as A  # noqa: E402
import common as C  # noqa: E402

OUT = C.RESULTS / "followup"
SWEEP_ORDER = ("C0", "E", "N1", "N3", "N10", "C3")
SAMPLED = ("0", "1", "2")
MODELS = {  # run id suffix -> (label, regex for the model's own true origin)
    "olmo": ("OLMo-2-7B-Instruct", r"\b(olmo|ai2|allen institute|allenai)\b"),
    "phi": ("Phi-4-mini-instruct", r"\b(phi(-?\d)?|microsoft)\b"),
    "gptoss": ("gpt-oss-20b", r"\b(openai|chatgpt|gpt-?\w*)\b"),
}
PERSONA = re.compile(C.LABS["persona"], re.I)


def words(cond):
    return 0 if cond == "C0" else len(C.system_prompt(cond).split())


def rate(df):
    """Rate over decided items with an intent-cluster bootstrap CI (as in the main analysis)."""
    d = df[df["value"].notna()]
    k, n = A.per_intent(d)
    lo, hi = A.cluster_boot(k, n, n_boot=2000)
    return {"n": len(d), "uncertain": int(df["value"].isna().sum()), "rate": d["value"].mean() if len(d) else np.nan,
            "lo": lo, "hi": hi}


def sweep():
    if not (C.RUNS / "sweep_qwen" / "raw.jsonl").exists():
        return None
    pol, parts = A.policy(), []
    for rid, conds in (("test1", ("C0", "C3")), ("sweep_qwen", SWEEP_ORDER)):
        G, labs, tlabs = A.load(rid)
        L = A.long_table(G, labs, {}, pol)
        parts.append(L[L["condition"].isin(conds) & L["run_idx"].astype(str).isin(SAMPLED)
                       & L["metric"].isin(["P1", "P2", "S1"])])
    L = pd.concat(parts)
    rows = [{"condition": c, "words": words(c), "mode": m, "metric": met, **rate(g)}
            for (c, m, met), g in L.groupby(["condition", "mode", "metric"])]
    T = pd.DataFrame(rows)
    T["order"] = T["condition"].map({c: i for i, c in enumerate(SWEEP_ORDER)})
    return T.sort_values(["metric", "mode", "order"]).drop(columns="order")


def own_label(lab, rx):
    s = " ".join(x for x in (lab.get("claimed_name"), lab.get("claimed_developer"), lab.get("claimed_model")) if x)
    return int(bool(re.search(rx, s, re.I))), int(bool(PERSONA.search(s)))


def models():
    rows = []
    for n, (label, rx) in MODELS.items():
        rid = f"x_{n}"
        raw = C.RUNS / rid / "raw.jsonl"
        if not raw.exists():
            continue
        G = pd.DataFrame(list({r["key"]: r for r in C.read_jsonl(raw)}.values()))
        G = G[G["error"].isna() & G["content"].fillna("").str.strip().astype(bool)]
        labs = {j: {r["key"]: r for r in C.read_jsonl(C.JUDGE / f"labels_{j}_{rid}.jsonl")} for j in A.JUDGES}
        recs = []
        for r in G.to_dict("records"):
            got = [own_label(labs[j][r["key"]], rx) for j in A.JUDGES if r["key"] in labs[j]]
            if len(got) < len(A.JUDGES):
                continue
            for i, met in enumerate(("own_origin", "persona")):
                vals = {g[i] for g in got}
                recs.append({"key": r["key"], "intent_id": r["intent_id"], "condition": r["condition"], "metric": met,
                             "value": vals.pop() if len(vals) == 1 else None})
        if not recs:
            continue
        L = pd.DataFrame(recs)
        rows += [{"model": label, "condition": c, "words": words(c), "metric": met, **rate(g)}
                 for (c, met), g in L.groupby(["condition", "metric"])]
    return pd.DataFrame(rows) if rows else None


def internals_v2():
    d = C.ROOT / "internals" / "out_v2"
    f = {k: d / k for k in ("directions_v2.json", "summary.csv", "patch.csv")}
    if not all(p.exists() for p in f.values()):
        return None
    return json.loads(f["directions_v2.json"].read_text()), pd.read_csv(f["summary.csv"]), pd.read_csv(f["patch.csv"])


def fmt(r):
    return f"{100 * r['rate']:.0f} [{100 * r['lo']:.0f}, {100 * r['hi']:.0f}]" if r["n"] else "n/a"


def figures(S, M, I):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    if S is not None:
        fig, ax = plt.subplots(figsize=(6.5, 4))
        for m in C.MODES:
            s = S[(S["metric"] == "P2") & (S["mode"] == m)]
            ax.errorbar(s["words"], 100 * s["rate"], yerr=[100 * (s["rate"] - s["lo"]), 100 * (s["hi"] - s["rate"])],
                        marker="o", capsize=3, label=f"thinking {'on' if m == 'think' else 'off'}")
        ax.set(xscale="symlog", xlabel="neutral system prompt length (words; 0 = none or empty)",
               ylabel="% answers claiming Qwen/Alibaba", title="Qwen3.6: true-origin claims vs neutral prompt length")
        ax.legend()
        fig.tight_layout()
        fig.savefig(OUT / "fig_sweep.png", dpi=130)
    if M is not None:
        fig, ax = plt.subplots(figsize=(6.5, 4))
        for mod, g in M[(M["metric"] == "own_origin") & (M["condition"] != "C1")].groupby("model"):
            g = g.sort_values("words")
            ax.plot(g["words"], 100 * g["rate"], marker="o", label=mod)
        ax.set(xscale="symlog", xlabel="neutral system prompt length (words)", ylabel="% claiming own true origin",
               title="Other models: own-origin claims vs neutral prompt length")
        ax.legend()
        fig.tight_layout()
        fig.savefig(OUT / "fig_models.png", dpi=130)
    if I is not None:
        dj, sm, pt = I
        fig, ax = plt.subplots(1, 3, figsize=(17, 4.3))
        ax[0].plot(dj["auroc_c2_c3"], label="AUROC C2 vs C3 (held-out)")
        ax[0].plot(dj["c0_position_on_c3_c2_axis"], label="C0 on the C3 to C2 axis (0 = neutral, 1 = persona)")
        ax[0].set(xlabel="layer", title="Persona-minus-neutral direction")
        idn = sm[sm["set"] == "identity"]
        for v, cond in (("minus", "C2"), ("plus", "C3"), ("rand", "C2")):
            for l, g in idn[idn["variant"] == v].groupby("layer"):
                base = idn[(idn["variant"] == "base") & (idn["cond"] == cond)]["p1"]
                xs = [0] + g["alpha"].tolist()
                ax[1].plot(xs, 100 * np.array([base.iloc[0]] + g["p1"].tolist()), marker="o",
                           ls="-" if v != "rand" else ":", label=f"{v} L{int(l)}")
        ax[1].set(xlabel="alpha (x ||d23||)", ylabel="% persona claims", title="Steering (held-out identity probes)")
        ax[2].plot(pt["layer"], 100 * pt["p1"], marker="o", label="persona (Mira)")
        ax[2].plot(pt["layer"], 100 * pt["p2"], marker="o", label="Qwen/Alibaba")
        ax[2].set(xlabel="layer patched (user-turn tokens, C2 into C0)", ylabel="% of answers", title="Activation patching")
        for a in ax:
            a.legend(fontsize=7)
        fig.tight_layout()
        fig.savefig(OUT / "fig_internals_v2.png", dpi=130)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    S, M, I = sweep(), models(), internals_v2()
    md = ["# Follow-up (exploratory, not pre-registered)", ""]
    if S is not None:
        S.to_csv(OUT / "sweep.csv", index=False)
        md += ["## Qwen3.6: true-origin claims (P2) vs neutral prompt length", "",
               "C0 and C3 from the main run (same seeds); E = empty system block; Nk = C3 header + its first k rules.", "",
               "| condition | words | thinking on | thinking off |", "|---|---|---|---|"]
        for c in SWEEP_ORDER:
            r = {m: S[(S["condition"] == c) & (S["mode"] == m) & (S["metric"] == "P2")] for m in C.MODES}
            if all(len(v) for v in r.values()):
                md.append(f"| {c} | {words(c)} | {fmt(r['think'].iloc[0])} | {fmt(r['nothink'].iloc[0])} |")
        md.append("")
    if M is not None:
        M.to_csv(OUT / "models.csv", index=False)
        md += ["## Other models: own true-origin claims (judges agree)", "",
               "| model | " + " | ".join(SWEEP_ORDER[:-1] + ("C3", "C1 persona")) + " |", "|---|" + "---|" * 7]
        for mod, g in M.groupby("model"):
            cell = lambda c, met: fmt(g[(g["condition"] == c) & (g["metric"] == met)].iloc[0]) if len(  # noqa: E731
                g[(g["condition"] == c) & (g["metric"] == met)]) else "n/a"
            md.append(f"| {mod} | " + " | ".join(cell(c, "own_origin") for c in SWEEP_ORDER) + f" | {cell('C1', 'persona')} |")
        md.append("")
    if I is not None:
        dj, _, pt = I
        md += ["## Internals v2", "", f"- L23* = {dj['L23_star']}; steering layers {dj['steer_layers']}.",
               "- Patching (persona % by layer): " + ", ".join(f"L{int(r.layer)} {100 * r.p1:.0f}" for r in pt.itertuples()), ""]
    (OUT / "followup.md").write_text("\n".join(md) + "\n")
    figures(S, M, I)
    print("\n".join(md))


if __name__ == "__main__":
    main()
