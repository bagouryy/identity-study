"""PLAN §11 internals: capture -> directions -> steer -> score. Each stage is resumable; outputs go to --out.

Runs inside vllm/vllm-openai:cu130-nightly with the study dir mounted at /work (see window.sh):
  python3 /work/internals/run_internals.py --backend vllm|hf --stage capture|directions|steer|score|all --out /work/internals/out
  --limit N: first N probes per set, writes to <out>/smoke (hook smoke test).

vLLM backend: the V1 engine is kept IN-PROCESS (VLLM_ENABLE_V1_MULTIPROCESSING=0, set before importing vllm), so
forward hooks registered through LLM.apply_model run in this process and read/write STATE directly. vLLM decoder
layers return (mlp_out, residual) and defer the add to the next layer's fused norm, so the residual stream after
layer l is out[0] + out[1]; steering adds to out[1]. Positions come from the layer's integer-tensor input.
HF backend: residual = layer output hidden_states[0], asserted equal to output_hidden_states on the first prompt.

Outputs: acts_<cond>.npz, directions.json/.npz, gens.jsonl, router.json, summary.csv, internals.png, timing.jsonl.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import torch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
import common  # noqa: E402

N_LAYERS = 40
CAPTURE_CONDS = ("C0", "C2", "C3")
SPLIT_SEED, RAND_SEED = 20261103, 7
N_RAND = 3
ALPHAS = (0, 0.5, 1, 2, 4)  # x ||mean diff||; since d is the mean diff itself, the added vector is alpha * d
MAX_NEW = 200
FAR_GAP = 20  # |far layer - L*|, PLAN needs >= 15; 20 always fits in 40 layers
N_BENIGN = 20
TOPK = 8
P1_RE = re.compile(common.LABS["persona"], re.I)
P2_RE = re.compile(common.LABS["qwen_alibaba"], re.I)
A = r"['’]"
REFUSAL = re.compile(  # compact version of src/rules.py HARD_REFUSAL, first 300 chars
    rf"\b(i (can{A}?t|cannot|won{A}t) (help|assist|provide|do that|share|comply)|i{A}m (not able|unable) to"
    rf"|i am (not able|unable) to|sorry, (but )?i (can{A}?t|cannot))\b", re.I)
THINK = re.compile(r"<think>.*?</think>\s*", re.S)

# Hook state. Hooks are installed once and read this dict on every forward.
STATE = {"backend": None, "capture": False, "cap": {}, "steer": None, "sys_len": 0, "start": None,
         "gate_layer": None, "gates": [],
         "capfull": None, "full": {}, "patch": None}  # v2 (run_v2.py): full-sequence capture at one layer, positional patch


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ---------- pure logic (unit-tested) ----------
def split_intents(intents, seed=SPLIT_SEED):
    perm = np.random.default_rng(seed).permutation(sorted(intents))
    h = len(perm) // 2
    return sorted(perm[:h].tolist()), sorted(perm[h:].tolist())


def mean_diff(pos, neg):
    """[n, L, H] x2 -> [L, H]."""
    return pos.mean(0) - neg.mean(0)


def auroc(pos, neg):
    pos, neg = np.asarray(pos)[:, None], np.asarray(neg)[None, :]
    return float(((pos > neg) + 0.5 * (pos == neg)).mean())


def dprime(pos, neg):
    return float((np.mean(pos) - np.mean(neg)) / (np.sqrt((np.var(pos) + np.var(neg)) / 2) + 1e-12))


def pick_layer(aucs, dps):
    # Max AUROC; ties (likely: a system prompt is easy to detect) broken by held-out d'.
    best = max(aucs)
    return max((i for i, a in enumerate(aucs) if a == best), key=lambda i: dps[i])


def far_layer(L, n=N_LAYERS):
    return L - FAR_GAP if L >= FAR_GAP else L + FAR_GAP


def cosine(a, b):
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))


def random_dirs(h, norm, n=N_RAND, seed=RAND_SEED):
    r = np.random.default_rng(seed).standard_normal((n, h))
    return r / np.linalg.norm(r, axis=1, keepdims=True) * norm


def configs(L, Lfar):
    cs = [dict(name=f"base_{c}", variant="base", cond=c, layer=None, alpha=0, sign=0) for c in ("C2", "C0")]
    for a in ALPHAS[1:]:
        cs += [dict(name=f"d_a{a}", variant="d", cond="C2", layer=L, alpha=a, sign=-1)]
        cs += [dict(name=f"rand{k}_a{a}", variant=f"rand{k}", cond="C2", layer=L, alpha=a, sign=-1) for k in range(N_RAND)]
        cs += [dict(name=f"far_a{a}", variant="far", cond="C2", layer=Lfar, alpha=a, sign=-1),
               dict(name=f"plus_a{a}", variant="plus", cond="C0", layer=L, alpha=a, sign=1)]
    return cs


def steer_vec(cfg, d, rand):
    if cfg["variant"] == "base":
        return None
    base = rand[int(cfg["variant"][4:])] if cfg["variant"].startswith("rand") else d
    return cfg["sign"] * cfg["alpha"] * base


def vllm_mask(pos, sys_len):
    """vLLM flattens the batch to [T, H]; steer every token at or after the end of the system prompt."""
    return pos >= sys_len


def hf_mask(T, start):
    """HF [B, T, H] with left padding: prefill steers columns >= pad + sys_len; decode (T == 1) steers all rows."""
    if T == 1:
        return torch.ones(len(start), 1, dtype=torch.bool, device=start.device)
    return torch.arange(T, device=start.device)[None, :] >= start[:, None]


def residual(out, backend):
    if backend == "vllm":
        assert isinstance(out, tuple) and len(out) == 2, f"vLLM layer output must be (hidden, residual), got {type(out)}"
        return out[0] + out[1]
    return out[0] if isinstance(out, tuple) else out


def add_to_output(out, delta, backend):
    if backend == "vllm":
        return out[0], out[1] + delta
    return (out[0] + delta,) + tuple(out[1:]) if isinstance(out, tuple) else out + delta


def int_tensor(args, kwargs):
    return next((a for a in [*kwargs.values(), *args] if torch.is_tensor(a) and not a.is_floating_point()), None)


def topk_overlap(a, b, k=TOPK):
    """Mean fraction of shared top-k experts per row, [T, E] x2."""
    ta, tb = a.topk(k, -1).indices, b.topk(k, -1).indices
    return float((ta[:, :, None] == tb[:, None, :]).any(-1).float().mean())


def clean(text):
    return THINK.sub("", text or "")


def score_text(text):
    t = clean(text)
    return {"p1": bool(P1_RE.search(t)), "p2": bool(P2_RE.search(t)), "refusal": bool(REFUSAL.search(t[:300]))}


def factual_ok(text, answers):
    return any(re.search(rf"(?<![\w.]){re.escape(a)}(?!\w)", clean(text), re.I) for a in answers)


# ---------- hooks ----------
def make_layer_hook(i):
    def hook(mod, args, kwargs, out):
        be = STATE["backend"]
        if STATE["capture"]:
            r = residual(out, be)
            STATE["cap"].setdefault(i, []).append((r.shape[-2], r.reshape(-1, r.shape[-1])[-1].float().cpu()))
        if STATE["capfull"] == i:
            r = residual(out, be)
            STATE["full"].setdefault(i, []).append(r.reshape(-1, r.shape[-1]).float().cpu())
        pt = STATE["patch"]
        if pt is not None and pt["layer"] == i:
            r = residual(out, be)
            if r.reshape(-1, r.shape[-1]).shape[0] == pt["T"]:  # single-prompt prefill only, decode steps untouched
                flat = r.reshape(-1, r.shape[-1])
                delta = torch.zeros_like(flat)
                K = pt["target"].shape[0]
                delta[-K:] = pt["target"].to(flat.device, flat.dtype) - flat[-K:]  # set the last K rows to the target
                pt["fired"] += 1
                return add_to_output(out, delta.reshape(r.shape), be)
        s = STATE["steer"]
        if s is None or s["layer"] != i:
            return None
        h = out[0] if isinstance(out, tuple) else out
        if be == "vllm":
            pos = int_tensor(args, kwargs)
            assert pos is not None, "vLLM layer got no positions tensor; cannot mask the system prompt"
            pos = pos[0] if pos.dim() == 2 else pos  # M-RoPE [3, T]: text rows are identical
            assert pos.shape[0] == h.shape[0], f"positions {tuple(pos.shape)} vs hidden {tuple(h.shape)}"
            m = vllm_mask(pos, STATE["sys_len"])
        else:
            m = hf_mask(h.shape[1], STATE["start"].to(h.device))
        s["fired"] += 1
        return add_to_output(out, m.unsqueeze(-1).to(h.dtype) * s["vec"].to(h.device, h.dtype), be)
    return hook


def make_gate_hook(i):
    def hook(mod, args, out):
        if STATE["gate_layer"] == i:
            o = out[0] if isinstance(out, tuple) else out
            STATE["gates"].append(o.reshape(-1, o.shape[-1]).float().cpu())
    return hook


def install_hooks(model, n_layers=N_LAYERS):
    """Find the one decoder-layer ModuleList (length n_layers, not in the vision tower) and hook every layer."""
    cands = [(n, m) for n, m in model.named_modules()
             if isinstance(m, torch.nn.ModuleList) and len(m) == n_layers and not re.search("visual|vision", n)]
    assert len(cands) == 1, f"expected one decoder ModuleList of length {n_layers}, found {[n for n, _ in cands]}"
    name, layers = cands[0]
    gates = 0
    for i, layer in enumerate(layers):
        layer.register_forward_hook(make_layer_hook(i), with_kwargs=True)
        g = getattr(getattr(layer, "mlp", None), "gate", None)
        if isinstance(g, torch.nn.Module):
            g.register_forward_hook(make_gate_hook(i))
            gates += 1
    return {"layers_path": name, "n_layers": len(layers), "gate_hooks": gates}


def take_capture(n_tokens, n_layers=N_LAYERS):
    """Pick the prefill call (n_tokens rows) for every layer -> [L, H]. Fails fast if hooks did not fire."""
    cap = STATE["cap"]
    assert len(cap) == n_layers, f"hooks fired on {len(cap)}/{n_layers} layers"
    rows = []
    for i in range(n_layers):
        hit = [r for n, r in cap[i] if n == n_tokens]
        assert hit, f"layer {i}: no call with {n_tokens} tokens, saw {[n for n, _ in cap[i]]}"
        rows.append(hit[0])
    return torch.stack(rows).numpy()


# ---------- backends ----------
class VLLMBackend:
    def __init__(self):
        os.environ["VLLM_ENABLE_V1_MULTIPROCESSING"] = "0"  # keep the worker (and our hooks) in this process
        os.environ.setdefault("VLLM_ALLOW_INSECURE_SERIALIZATION", "1")  # harmless in-process; needed if it is not
        from vllm import LLM, SamplingParams
        self.SP = SamplingParams
        kw = dict(model=common.MODEL, revision=common.SNAPSHOT, dtype="bfloat16", enforce_eager=True,
                  enable_prefix_caching=False, max_model_len=4096, gpu_memory_utilization=0.75, seed=0)  # unified memory: ~98 GiB free after reboot
        try:
            self.llm = LLM(**kw, limit_mm_per_prompt={"image": 0, "video": 0})
        except (TypeError, ValueError) as e:
            log(f"limit_mm_per_prompt rejected ({e}); retrying without")
            self.llm = LLM(**kw)
        self.tok = self.llm.get_tokenizer()
        STATE["backend"] = "vllm"
        self.info = self.on_model(install_hooks)
        log("hooks:", self.info)

    def on_model(self, fn):
        if hasattr(self.llm, "apply_model"):
            return self.llm.apply_model(fn)[0]
        return self.llm.collective_rpc(lambda w: fn(w.get_model()))[0]

    def prefill(self, ids):
        self.llm.generate([{"prompt_token_ids": ids}], self.SP(max_tokens=1, temperature=0), use_tqdm=False)

    def generate(self, batch):
        outs = self.llm.generate([{"prompt_token_ids": ids} for ids in batch],
                                 self.SP(max_tokens=MAX_NEW, temperature=0), use_tqdm=False)
        return [o.outputs[0].text for o in outs]


class HFBackend:
    def __init__(self, model_id=common.MODEL, revision=common.SNAPSHOT, device="cuda", n_layers=N_LAYERS):
        import transformers as tf
        self.tok = tf.AutoTokenizer.from_pretrained(model_id, revision=revision)
        kw = dict(revision=revision, dtype=torch.bfloat16 if device == "cuda" else torch.float32, device_map=device)
        try:
            self.model = tf.AutoModelForCausalLM.from_pretrained(model_id, **kw)
        except (ValueError, KeyError) as e:
            log(f"AutoModelForCausalLM failed ({e}); using AutoModelForImageTextToText")
            self.model = tf.AutoModelForImageTextToText.from_pretrained(model_id, **kw)
        self.model.eval()
        self.device, self.n_layers, self.checked = device, n_layers, False
        STATE["backend"] = "hf"
        self.info = install_hooks(self.model, n_layers)
        log("hooks:", self.info)

    @torch.no_grad()
    def prefill(self, ids):
        check = STATE["capture"] and not self.checked
        STATE["start"] = torch.tensor([STATE["sys_len"]])  # batch of one, no padding
        out = self.model(input_ids=torch.tensor([ids], device=self.device), use_cache=False, output_hidden_states=check)
        if check:  # residual hook == hidden_states[l + 1]; the last entry is post-final-norm in HF, so skip it
            hs = out.hidden_states
            assert hs is not None and len(hs) == self.n_layers + 1, f"hidden_states len {None if hs is None else len(hs)}"
            got = take_capture(len(ids), self.n_layers)
            for l in range(self.n_layers - 1):
                assert np.allclose(got[l], hs[l + 1][0, -1].float().cpu().numpy(), atol=1e-3), f"layer {l} != hidden_states"
            self.checked = True

    @torch.no_grad()
    def generate(self, batch):
        T = max(map(len, batch))
        pad = self.tok.pad_token_id if self.tok.pad_token_id is not None else self.tok.eos_token_id
        ids = torch.tensor([[pad] * (T - len(b)) + b for b in batch], device=self.device)
        att = torch.tensor([[0] * (T - len(b)) + [1] * len(b) for b in batch], device=self.device)
        STATE["start"] = torch.tensor([T - len(b) + STATE["sys_len"] for b in batch])
        out = self.model.generate(input_ids=ids, attention_mask=att, max_new_tokens=MAX_NEW, do_sample=False,
                                  pad_token_id=pad)
        return self.tok.batch_decode(out[:, T:], skip_special_tokens=True)


def render(tok, cond, text):
    """Thinking-off prompt ids and the token count of everything before the user turn (the system prompt)."""
    s = tok.apply_chat_template(common.messages(cond, text), tokenize=False, add_generation_prompt=True,
                                enable_thinking=False)
    assert "</think>" in s[-20:], f"prompt must end with the empty think block, got ...{s[-40:]!r}"
    ids = tok(s, add_special_tokens=False)["input_ids"]
    cut = s.index("<|im_start|>user")
    pre = tok(s[:cut], add_special_tokens=False)["input_ids"] if cut else []
    assert ids[:len(pre)] == pre, "system prefix does not tokenize as a prefix of the prompt"
    return ids, len(pre)


# ---------- stages ----------
def identity_probes():
    return [p for p in common.load_probes("test") if p["category"] == "identity"]


def stage_capture(be, out, limit):
    probes = identity_probes()[:limit]
    for c in CAPTURE_CONDS:
        path = out / f"acts_{c}.npz"
        if path.exists():
            log(f"capture {c}: exists, skip")
            continue
        X = []
        for p in probes:
            ids, _ = render(be.tok, c, p["text"])
            STATE.update(capture=True, cap={})
            try:
                be.prefill(ids)
                X.append(take_capture(len(ids)))
            finally:
                STATE["capture"] = False
        X = np.stack(X).astype(np.float32)
        assert X.shape[:2] == (len(probes), N_LAYERS) and np.isfinite(X).all(), f"bad capture {X.shape}"
        np.savez(path, X=X, probe_id=[p["probe_id"] for p in probes], intent_id=[p["intent_id"] for p in probes])
        log(f"capture {c}: {X.shape}, mean ||resid|| at L20 = {np.linalg.norm(X[:, 20], axis=1).mean():.1f}")


def stage_directions(out):
    a = {c: np.load(out / f"acts_{c}.npz") for c in CAPTURE_CONDS}
    intents = a["C0"]["intent_id"]
    fit, held = split_intents(set(intents.tolist()))
    f, h = np.isin(intents, fit), np.isin(intents, held)
    X = {c: a[c]["X"] for c in CAPTURE_CONDS}
    d2, d3 = mean_diff(X["C2"][f], X["C0"][f]), mean_diff(X["C3"][f], X["C0"][f])
    proj = lambda c, l: X[c][h, l] @ d2[l]  # noqa: E731
    auc = [auroc(proj("C2", l), proj("C0", l)) for l in range(N_LAYERS)]
    dps = [dprime(proj("C2", l), proj("C0", l)) for l in range(N_LAYERS)]
    auc3 = [auroc(X["C3"][h, l] @ d3[l], X["C0"][h, l] @ d3[l]) for l in range(N_LAYERS)]
    L = pick_layer(auc, dps)
    norm = float(np.linalg.norm(d2[L]))
    res = {"split_seed": SPLIT_SEED, "rand_seed": RAND_SEED, "fit_intents": fit, "heldout_intents": held,
           "n_fit": int(f.sum()), "n_heldout": int(h.sum()), "auroc_c2_c0": auc, "dprime_c2_c0": dps,
           "auroc_c3_c0": auc3, "cos_d2_d3": [cosine(d2[l], d3[l]) for l in range(N_LAYERS)],
           "norm_d2": np.linalg.norm(d2, axis=1).tolist(), "L_star": L, "L_far": far_layer(L), "norm_star": norm}
    np.savez(out / "directions.npz", d2=d2, d3=d3, rand=random_dirs(d2.shape[1], norm))
    (out / "directions.json").write_text(json.dumps(res, indent=1))
    log(f"directions: L*={L} auroc={auc[L]:.3f} d'={dps[L]:.2f} ||d||={norm:.2f} far={res['L_far']}")


def steer_sets(out, limit):
    probes = common.load_probes("test")
    held = json.loads((out / "directions.json").read_text())["heldout_intents"]
    ident = [(p["probe_id"], p["text"], None) for p in probes if p["category"] == "identity" and p["intent_id"] in held]
    benign = [(p["probe_id"], p["text"], None) for p in probes if p["category"] == "benign" and p["probe_id"].endswith("-0")]
    fact = [(f"F{i:02d}", q["q"], q["a"]) for i, q in enumerate(common.load_yaml(HERE / "factual.yaml"))]
    return {"identity": ident[:limit], "benign": benign[:N_BENIGN][:limit], "factual": fact[:limit]}


def stage_steer(be, out, limit, backend):
    dj = json.loads((out / "directions.json").read_text())
    dn = np.load(out / "directions.npz")
    L, Lfar = dj["L_star"], dj["L_far"]
    d, rand = dn["d2"][L], dn["rand"]
    sets = steer_sets(out, limit)
    gpath = out / "gens.jsonl"
    done = {(r["config"], r["set"]) for r in common.read_jsonl(gpath)}
    for cfg in configs(L, Lfar):
        # shortcut: the slow HF path runs damage checks only on base + d configs (PLAN minimum), vLLM on all
        for sname in ("identity", "benign", "factual") if backend == "vllm" or cfg["variant"] in ("base", "d") else ("identity",):
            if (cfg["name"], sname) in done:
                continue
            t0, items = time.time(), sets[sname]
            batch, sys_lens = zip(*(render(be.tok, cfg["cond"], t) for _, t, _ in items))
            assert len(set(sys_lens)) == 1, "one system prompt per batch"
            v = steer_vec(cfg, d, rand)
            STATE["sys_len"] = sys_lens[0]
            STATE["steer"] = None if v is None else {"layer": cfg["layer"], "vec": torch.tensor(v), "fired": 0}
            try:
                texts = be.generate(list(batch))
                if v is not None:
                    assert STATE["steer"]["fired"] > 0, f"steering hook never fired for {cfg['name']}"
            finally:
                STATE["steer"] = None
            for (pid, _, ans), t in zip(items, texts):
                common.append_jsonl(gpath, {**cfg, "config": cfg["name"], "set": sname, "probe_id": pid,
                                            "answers": ans, "text": t})
            log(f"steer {cfg['name']:>12} {sname:<8} n={len(items)} {time.time() - t0:.0f}s")
    router_diag(be, out, sets["identity"], L, d)


def router_diag(be, out, items, L, d):
    """Optional: top-8 expert overlap at layer L*+1 (first router downstream of the edit) with vs without steering."""
    path = out / "router.json"
    if path.exists() or be.info.get("gate_hooks", 0) < N_LAYERS or L + 1 >= N_LAYERS:
        return log("router diag: skipped (done, no gate hooks, or L* is last)")
    res = {"layer": L + 1, "alphas": list(ALPHAS[1:]), "overlap": {}}
    base = []
    try:
        for a in ALPHAS:
            ov = []
            for j, (_, text, _) in enumerate(items):
                ids, sl = render(be.tok, "C2", text)
                STATE.update(sys_len=sl, gate_layer=L + 1, gates=[],
                             steer=None if a == 0 else {"layer": L, "vec": torch.tensor(-a * d), "fired": 0})
                be.prefill(ids)
                g = [x for x in STATE["gates"] if x.shape[0] == len(ids)]
                assert g, f"router hook saw {[x.shape for x in STATE['gates']]}"
                if a == 0:
                    base.append(g[0][sl:])
                else:
                    ov.append(topk_overlap(base[j], g[0][sl:]))
            if a:
                res["overlap"][str(a)] = float(np.mean(ov))
    except (AssertionError, RuntimeError) as e:
        return log(f"router diag: failed ({e}); skipped")
    finally:
        STATE.update(steer=None, gate_layer=None, gates=[])
    path.write_text(json.dumps(res, indent=1))
    log("router diag:", res["overlap"])


def stage_score(out):
    rows = common.read_jsonl(out / "gens.jsonl")
    agg = {}
    for r in rows:
        s = score_text(r["text"])
        if r["answers"]:
            s["factual"] = factual_ok(r["text"], r["answers"])
        agg.setdefault((r["config"], r["set"]), (r, []))[1].append(s)
    fields = ["config", "variant", "cond", "layer", "alpha", "set", "n", "p1", "p2", "refusal", "factual_acc"]
    table = []
    for (cfg, sname), (r, ss) in sorted(agg.items()):
        m = lambda k: round(float(np.mean([s[k] for s in ss])), 4) if k in ss[0] else ""  # noqa: E731
        table.append({**{k: r[k] for k in ("config", "variant", "cond", "layer", "alpha")}, "set": sname, "n": len(ss),
                      "p1": m("p1"), "p2": m("p2"), "refusal": m("refusal"), "factual_acc": m("factual")})
    with open(out / "summary.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fields)
        w.writeheader()
        w.writerows(table)
    log(f"score: {len(rows)} generations -> summary.csv ({len(table)} rows)")
    plot(out, table)


def curve(table, variant, sname, key):
    """(alphas, values) for a variant; alpha 0 is the matching baseline, rand* is averaged over seeds."""
    cond = "C0" if variant == "plus" else "C2"
    pts = {0: [r[key] for r in table if r["config"] == f"base_{cond}" and r["set"] == sname]}
    for r in table:
        if r["set"] == sname and (r["variant"] == variant or (variant == "rand" and r["variant"].startswith("rand"))):
            pts.setdefault(r["alpha"], []).append(r[key])
    xs = sorted(a for a in pts if pts[a] and pts[a][0] != "")
    return xs, [float(np.mean(pts[a])) for a in xs]


def plot(out, table):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return log("matplotlib missing; rerun --stage score on the Mac for the figure")
    dj = json.loads((out / "directions.json").read_text())
    fig, ax = plt.subplots(1, 4, figsize=(20, 4.5))
    ax[0].plot(dj["auroc_c2_c0"], label="AUROC C2 vs C0 (held-out)")
    ax[0].plot(dj["auroc_c3_c0"], label="AUROC C3 vs C0 (held-out)")
    ax[0].plot(dj["cos_d2_d3"], label="cos(d_C2, d_C3)")
    ax[0].axvline(dj["L_star"], ls=":", c="k")
    ax[0].set(xlabel="layer", title=f"Layer sweep (L*={dj['L_star']})", ylim=(-0.05, 1.05))
    labels = {"d": "-a*d at L* (C2)", "rand": "random dirs, mean of 3 (C2)", "far": f"-a*d at layer {dj['L_far']} (C2)",
              "plus": "+a*d at L* (C0)"}
    for i, (sname, key, title) in enumerate([("identity", "p1", "P1 persona"), ("identity", "p2", "P2 true origin")], 1):
        for v, lab in labels.items():
            ax[i].plot(*curve(table, v, sname, key), marker="o", label=lab)
        ax[i].set(xlabel="alpha (x ||mean diff||)", title=f"{title}, held-out identity probes", ylim=(-0.05, 1.05))
    for sname, key, lab in [("benign", "refusal", "benign refusal"), ("factual", "factual_acc", "factual accuracy")]:
        for v in ("d", "rand", "far", "plus"):
            xs, ys = curve(table, v, sname, key)
            if len(xs) > 1:
                ax[3].plot(xs, ys, marker="o", ls="-" if v == "d" else ":", label=f"{lab}, {v}")
    ax[3].set(xlabel="alpha (x ||mean diff||)", title="Damage check", ylim=(-0.05, 1.05))
    for a in ax:
        a.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(out / "internals.png", dpi=130)
    log("figure: internals.png")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=("vllm", "hf"), default="vllm")
    ap.add_argument("--stage", choices=("capture", "directions", "steer", "score", "all"), default="all")
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args(argv)
    out = Path(a.out) / "smoke" if a.limit else Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stages = ["capture", "directions", "steer", "score"] if a.stage == "all" else [a.stage]
    be = None
    if {"capture", "steer"} & set(stages):
        t0 = time.time()
        be = VLLMBackend() if a.backend == "vllm" else HFBackend()
        common.append_jsonl(out / "timing.jsonl", {"stage": "load", "backend": a.backend, "s": round(time.time() - t0, 1)})
    for st in stages:
        t0 = time.time()
        log(f"== {st} ({a.backend})")
        {"capture": lambda: stage_capture(be, out, a.limit), "directions": lambda: stage_directions(out),
         "steer": lambda: stage_steer(be, out, a.limit, a.backend), "score": lambda: stage_score(out)}[st]()
        dt = round(time.time() - t0, 1)
        common.append_jsonl(out / "timing.jsonl", {"stage": st, "backend": a.backend, "s": dt, "ts": time.time()})
        log(f"== {st} done in {dt}s")


if __name__ == "__main__":
    main()
