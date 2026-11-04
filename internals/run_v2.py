"""Internals v2 (exploratory follow-up, not pre-registered). Reuses run_internals.py's backend and hooks.

v1 contrasted C2 with C0 and found a "a system prompt is present" direction. v2 asks about identity itself:
  directions  d23 = mean(C2) - mean(C3) per layer on the fit intents (persona prompt minus an equally long neutral one)
  steer       -a*d23 in C2 (remove persona) and +a*d23 in C3 (induce it), at L23* and layers 10/20/30, larger alphas,
              one norm-matched random direction per layer as control
  patch       for each layer l: run C0 (no prompt) but overwrite the residual of the user-turn tokens at layer l with
              their values from the C2 run. If the model then says "Mira", identity has reached those tokens by layer l.
  score       summary.csv + patch.csv in --out

python3 internals/run_v2.py --stage all --out /work/internals/out_v2 [--acts /work/internals/out] [--limit 2]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_internals as R  # noqa: E402

common = R.common
MID_LAYERS = (10, 20, 30)
ALPHAS = (1, 2, 4, 8)  # x ||d23 at that layer||
PATCH_LAYERS = tuple(range(0, R.N_LAYERS, 4)) + (R.N_LAYERS - 1,)
PATCH_MAX_NEW = 60  # the first sentence carries the identity claim


# ---------- pure logic (unit-tested) ----------
def steer_layers(L23, mid=MID_LAYERS):
    return sorted({L23, *mid})


def configs_v2(layers):
    cs = [dict(name=f"base_{c}", variant="base", cond=c, layer=None, alpha=0, sign=0) for c in ("C2", "C3", "C0")]
    for l in layers:
        for a in ALPHAS:
            cs += [dict(name=f"minus_L{l}_a{a}", variant="minus", cond="C2", layer=l, alpha=a, sign=-1),
                   dict(name=f"plus_L{l}_a{a}", variant="plus", cond="C3", layer=l, alpha=a, sign=1),
                   dict(name=f"rand_L{l}_a{a}", variant="rand", cond="C2", layer=l, alpha=a, sign=-1)]
    return cs


def user_suffix_len(ids_c0, ids_c2):
    """Tokens shared at the end of both prompts (the user turn + assistant header). C0 has no system block."""
    k = 0
    while k < min(len(ids_c0), len(ids_c2)) and ids_c0[-1 - k] == ids_c2[-1 - k]:
        k += 1
    return k


# ---------- stages ----------
def stage_directions(acts, out):
    a = {c: np.load(acts / f"acts_{c}.npz") for c in ("C0", "C2", "C3")}
    intents = a["C0"]["intent_id"]
    fit, held = R.split_intents(set(intents.tolist()))
    f, h = np.isin(intents, fit), np.isin(intents, held)
    X = {c: a[c]["X"] for c in a}
    d = R.mean_diff(X["C2"][f], X["C3"][f])
    auc = [R.auroc(X["C2"][h, l] @ d[l], X["C3"][h, l] @ d[l]) for l in range(R.N_LAYERS)]
    dps = [R.dprime(X["C2"][h, l] @ d[l], X["C3"][h, l] @ d[l]) for l in range(R.N_LAYERS)]
    # C0 sits where on the C3 -> C2 axis? 0 = like neutral, 1 = like persona (held-out, per layer)
    pos = [float(np.mean((X["C0"][h, l] - X["C3"][h, l].mean(0)) @ d[l]) / (d[l] @ d[l] + 1e-12)) for l in range(R.N_LAYERS)]
    L = R.pick_layer(auc, dps)
    norms = np.linalg.norm(d, axis=1)
    rng = np.random.default_rng(R.RAND_SEED)
    rand = rng.standard_normal(d.shape)
    rand = rand / np.linalg.norm(rand, axis=1, keepdims=True) * norms[:, None]  # norm-matched per layer
    np.savez(out / "directions_v2.npz", d23=d, rand=rand)
    res = {"fit_intents": fit, "heldout_intents": held, "auroc_c2_c3": auc, "dprime_c2_c3": dps,
           "c0_position_on_c3_c2_axis": pos, "norm_d23": norms.tolist(), "L23_star": L,
           "steer_layers": steer_layers(L)}
    (out / "directions_v2.json").write_text(json.dumps(res, indent=1))
    R.log(f"directions v2: L23*={L} auroc={auc[L]:.3f} d'={dps[L]:.2f} ||d23||={norms[L]:.2f}")


def stage_steer(be, out, limit):
    dj = json.loads((out / "directions_v2.json").read_text())
    dn = np.load(out / "directions_v2.npz")
    probes = [p for p in R.identity_probes() if p["intent_id"] in dj["heldout_intents"]][:limit]
    fact = [(f"F{i:02d}", q["q"], q["a"]) for i, q in enumerate(common.load_yaml(HERE / "factual.yaml"))][:limit]
    gpath = out / "gens.jsonl"
    done = {(r["config"], r["set"]) for r in common.read_jsonl(gpath)}
    for cfg in configs_v2(dj["steer_layers"]):
        sets = {"identity": [(p["probe_id"], p["text"], None) for p in probes]}
        if cfg["variant"] in ("base", "minus") and cfg["alpha"] in (0, max(ALPHAS)):
            sets["factual"] = fact  # damage check at the largest push only
        for sname, items in sets.items():
            if (cfg["name"], sname) in done:
                continue
            t0 = time.time()
            batch, sys_lens = zip(*(R.render(be.tok, cfg["cond"], t) for _, t, _ in items))
            assert len(set(sys_lens)) == 1, "one system prompt per batch"
            v = None
            if cfg["variant"] != "base":
                base = dn["rand"][cfg["layer"]] if cfg["variant"] == "rand" else dn["d23"][cfg["layer"]]
                v = cfg["sign"] * cfg["alpha"] * base
            R.STATE["sys_len"] = sys_lens[0]
            R.STATE["steer"] = None if v is None else {"layer": cfg["layer"], "vec": torch.tensor(v), "fired": 0}
            try:
                texts = be.generate(list(batch))
                if v is not None:
                    assert R.STATE["steer"]["fired"] > 0, f"steering hook never fired for {cfg['name']}"
            finally:
                R.STATE["steer"] = None
            for (pid, _, ans), t in zip(items, texts):
                common.append_jsonl(gpath, {**cfg, "config": cfg["name"], "set": sname, "probe_id": pid,
                                            "answers": ans, "text": t})
            R.log(f"steer {cfg['name']:>16} {sname:<8} n={len(items)} {time.time() - t0:.0f}s")


def stage_patch(be, out, limit):
    dj = json.loads((out / "directions_v2.json").read_text())
    probes = [p for p in R.identity_probes() if p["intent_id"] in dj["heldout_intents"]][:limit]
    path = out / "patch.jsonl"
    done = {(r["probe_id"], r["layer"]) for r in common.read_jsonl(path)}
    sp = be.SP(max_tokens=PATCH_MAX_NEW, temperature=0)
    for p in probes:
        ids0, _ = R.render(be.tok, "C0", p["text"])
        ids2, _ = R.render(be.tok, "C2", p["text"])
        K = user_suffix_len(ids0, ids2)
        assert K >= 5, f"{p['probe_id']}: only {K} shared suffix tokens"
        for l in PATCH_LAYERS:
            if (p["probe_id"], l) in done:
                continue
            R.STATE.update(capfull=l, full={})
            try:
                be.prefill(ids2)
            finally:
                R.STATE["capfull"] = None
            rows = [x for x in R.STATE["full"].get(l, []) if x.shape[0] == len(ids2)]
            assert rows, f"layer {l}: no full capture of the C2 prefill"
            R.STATE["patch"] = {"layer": l, "T": len(ids0), "target": rows[0][-K:], "fired": 0}
            try:
                o = be.llm.generate([{"prompt_token_ids": ids0}], sp, use_tqdm=False)[0].outputs[0].text
                assert R.STATE["patch"]["fired"] > 0, f"patch hook never fired at layer {l}"
            finally:
                R.STATE["patch"] = None
            common.append_jsonl(path, {"probe_id": p["probe_id"], "layer": l, "K": K, "text": o, **R.score_text(o)})
        R.log(f"patch {p['probe_id']}: {len(PATCH_LAYERS)} layers, K={K}")


def stage_score(out):
    rows = common.read_jsonl(out / "gens.jsonl")
    agg = {}
    for r in rows:
        s = R.score_text(r["text"])
        if r["answers"]:
            s["factual"] = R.factual_ok(r["text"], r["answers"])
        agg.setdefault((r["config"], r["set"]), (r, []))[1].append(s)
    lines = ["config,variant,cond,layer,alpha,set,n,p1,p2,refusal,factual_acc"]
    for (cfg, sname), (r, ss) in sorted(agg.items()):
        m = lambda k: f"{np.mean([s[k] for s in ss]):.4f}" if k in ss[0] else ""  # noqa: E731
        lines.append(",".join(map(str, [cfg, r["variant"], r["cond"], r["layer"], r["alpha"], sname, len(ss),
                                         m("p1"), m("p2"), m("refusal"), m("factual")])))
    (out / "summary.csv").write_text("\n".join(lines) + "\n")
    P = common.read_jsonl(out / "patch.jsonl")
    pl = ["layer,n,p1,p2"] + [f"{l},{len(g)},{np.mean([x['p1'] for x in g]):.4f},{np.mean([x['p2'] for x in g]):.4f}"
                              for l in sorted({x["layer"] for x in P}) for g in [[x for x in P if x["layer"] == l]]]
    (out / "patch.csv").write_text("\n".join(pl) + "\n")
    R.log(f"score v2: {len(rows)} steering generations, {len(P)} patch generations")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=("capture", "directions", "steer", "patch", "score", "all"), default="all")
    ap.add_argument("--out", default=str(HERE / "out_v2"))
    ap.add_argument("--acts", default=None, help="folder with acts_C*.npz (default: --out, captured here)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--backend", choices=("vllm",), default="vllm")  # window.sh passes it; v2 is vLLM only
    a = ap.parse_args(argv)
    out = Path(a.out) / "smoke" if a.limit else Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    acts = Path(a.acts) if a.acts else out
    stages = ["capture", "directions", "steer", "patch", "score"] if a.stage == "all" else [a.stage]
    be = R.VLLMBackend() if {"capture", "steer", "patch"} & set(stages) else None
    for st in stages:
        t0 = time.time()
        R.log(f"== v2 {st}")
        {"capture": lambda: R.stage_capture(be, acts, a.limit), "directions": lambda: stage_directions(acts, out),
         "steer": lambda: stage_steer(be, out, a.limit), "patch": lambda: stage_patch(be, out, a.limit),
         "score": lambda: stage_score(out)}[st]()
        common.append_jsonl(out / "timing.jsonl", {"stage": st, "s": round(time.time() - t0, 1), "ts": time.time()})


if __name__ == "__main__":
    main()
