"""internals/run_internals.py pure logic and hook math on CPU. Run: internals/.venv/bin/python tests/test_internals.py"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "internals"))
import run_internals as ri  # noqa: E402

common = ri.common


class Pure(unittest.TestCase):
    def test_split(self):
        intents = [f"I{i:02d}" for i in range(1, 19)]
        fit, held = ri.split_intents(set(intents))
        self.assertEqual((len(fit), len(held)), (9, 9))
        self.assertEqual(sorted(fit + held), intents)
        self.assertEqual((fit, held), ri.split_intents(reversed(intents)))  # order-independent, seeded
        self.assertNotEqual(fit, ri.split_intents(intents, seed=1)[0])

    def test_mean_diff_auroc(self):
        pos, neg = np.ones((4, 3, 2)), np.zeros((5, 3, 2))
        self.assertTrue(np.allclose(ri.mean_diff(pos, neg), 1))
        self.assertEqual(ri.auroc([3, 4], [1, 2]), 1.0)
        self.assertEqual(ri.auroc([1, 2], [3, 4]), 0.0)
        self.assertEqual(ri.auroc([1, 1], [1, 1]), 0.5)
        self.assertAlmostEqual(ri.auroc([1, 3], [2, 0]), 0.75)

    def test_pick_and_far_layer(self):
        self.assertEqual(ri.pick_layer([0.9, 1.0, 1.0, 0.5], [9, 1, 3, 0]), 2)  # tie broken by d'
        for L in range(ri.N_LAYERS):
            f = ri.far_layer(L)
            self.assertTrue(0 <= f < ri.N_LAYERS and abs(f - L) >= 15)

    def test_random_dirs_equal_norm(self):
        r = ri.random_dirs(2048, 7.5)
        self.assertEqual(r.shape, (3, 2048))
        self.assertTrue(np.allclose(np.linalg.norm(r, axis=1), 7.5))
        self.assertTrue(np.allclose(r, ri.random_dirs(2048, 7.5)))
        self.assertLess(abs(ri.cosine(r[0], r[1])), 0.1)

    def test_configs_and_alpha_scaling(self):
        cs = ri.configs(10, 30)
        self.assertEqual(len(cs), 2 + 4 * 6)
        self.assertEqual(len({c["name"] for c in cs}), len(cs))
        d = np.random.default_rng(0).standard_normal(16)
        rand = ri.random_dirs(16, np.linalg.norm(d))
        for c in cs:
            v = ri.steer_vec(c, d, rand)
            if c["variant"] == "base":
                self.assertIsNone(v)
                continue
            self.assertAlmostEqual(np.linalg.norm(v), c["alpha"] * np.linalg.norm(d))  # alpha x ||mean diff||
            if c["variant"] in ("d", "far"):
                self.assertTrue(np.allclose(v, -c["alpha"] * d) and c["cond"] == "C2")
            if c["variant"] == "plus":
                self.assertTrue(np.allclose(v, c["alpha"] * d) and c["cond"] == "C0" and c["layer"] == 10)
            if c["variant"] == "far":
                self.assertEqual(c["layer"], 30)

    def test_masks(self):
        self.assertEqual(ri.vllm_mask(torch.tensor([0, 1, 2, 3, 0, 1]), 2).tolist(), [0, 0, 1, 1, 0, 0])
        m = ri.hf_mask(5, torch.tensor([3, 1]))
        self.assertEqual(m.tolist(), [[0, 0, 0, 1, 1], [0, 1, 1, 1, 1]])
        self.assertEqual(ri.hf_mask(1, torch.tensor([3, 1])).tolist(), [[1], [1]])

    def test_topk_overlap(self):
        a = torch.randn(5, 256)
        self.assertEqual(ri.topk_overlap(a, a), 1.0)
        self.assertEqual(ri.topk_overlap(a, -a), 0.0)

    def test_scoring(self):
        s = ri.score_text("<think>\n\n</think>\n\nI am Mira, Quillmere's assistant.")
        self.assertEqual((s["p1"], s["p2"], s["refusal"]), (True, False, False))
        self.assertTrue(ri.score_text("I'm Qwen, made by Alibaba Cloud.")["p2"])
        self.assertFalse(ri.score_text("Qwenlike models")["p2"])
        self.assertTrue(ri.score_text("I’m sorry, but I can’t help with that.")["refusal"])
        self.assertTrue(ri.score_text("I cannot provide medical advice.")["refusal"])
        self.assertFalse(ri.score_text("Common signs are thirst and dark urine.")["refusal"])

    def test_factual(self):
        self.assertTrue(ri.factual_ok("The square root of 144 is 12.", ["12", "twelve"]))
        self.assertFalse(ri.factual_ok("It is 120.", ["12"]))
        self.assertFalse(ri.factual_ok("It is 1.12", ["12"]))
        self.assertTrue(ri.factual_ok("Its symbol is Au.", ["au"]))
        self.assertTrue(ri.factual_ok("Plants absorb CO₂.", ["co2", "co₂"]))
        fs = common.load_yaml(ROOT / "internals" / "factual.yaml")
        self.assertEqual(len(fs), 20)
        self.assertTrue(all(isinstance(f["q"], str) and f["a"] and all(isinstance(a, str) for a in f["a"]) for f in fs))


# ---- toy models: a ModuleList of layers, HF style (returns (h,)) and vLLM style (returns (mlp_out, residual)) ----
class HFLayer(torch.nn.Module):
    def __init__(self, h):
        super().__init__()
        self.lin = torch.nn.Linear(h, h)

    def forward(self, x):
        return (x + torch.tanh(self.lin(x)),)


class VLayer(torch.nn.Module):
    def __init__(self, h):
        super().__init__()
        self.lin = torch.nn.Linear(h, h)

    def forward(self, hidden_states, residual, positions=None):
        x = hidden_states if residual is None else hidden_states + residual
        return torch.tanh(self.lin(x)), x


class Toy(torch.nn.Module):
    def __init__(self, layer, n=4, h=8):
        super().__init__()
        torch.manual_seed(0)
        self.visual = torch.nn.ModuleList([torch.nn.Linear(h, h) for _ in range(n)])  # must be ignored
        self.layers = torch.nn.ModuleList([layer(h) for _ in range(n)])
        self.vllm = layer is VLayer

    def forward(self, x, positions=None):
        if self.vllm:
            h, r = x, None
            for l in self.layers:
                h, r = l(hidden_states=h, residual=r, positions=positions)
            return h + r
        for l in self.layers:
            x = l(x)[0]
        return x


class Hooks(unittest.TestCase):
    def setUp(self):
        ri.STATE.update(capture=False, cap={}, steer=None, sys_len=0, start=None, gate_layer=None, gates=[])

    def run_toy(self, be):
        torch.manual_seed(1)
        m = Toy(VLayer if be == "vllm" else HFLayer)
        ri.STATE["backend"] = be
        self.assertEqual(ri.install_hooks(m, 4)["layers_path"], "layers")
        T = 6
        x = torch.randn(T, 8) if be == "vllm" else torch.randn(1, T, 8)
        pos = torch.arange(T)
        fwd = (lambda: m(x, positions=pos)) if be == "vllm" else (lambda: m(x))
        with torch.no_grad():
            ri.STATE.update(capture=True, cap={})
            y0 = fwd()
            ri.STATE["capture"] = False
            acts = ri.take_capture(T, 4)
            self.assertEqual(acts.shape, (4, 8))
            # residual after the last layer is what the model returns
            self.assertTrue(np.allclose(acts[-1], y0.reshape(T, 8)[-1].numpy(), atol=1e-6))
            v = torch.randn(8)
            ri.STATE.update(sys_len=3, start=torch.tensor([3]), steer={"layer": 3, "vec": v, "fired": 0})
            y1 = fwd().reshape(T, 8)
            self.assertEqual(ri.STATE["steer"]["fired"], 1)
            delta = y1 - y0.reshape(T, 8)
            self.assertTrue(torch.allclose(delta[:3], torch.zeros(3, 8), atol=1e-6))  # system prompt untouched
            self.assertTrue(torch.allclose(delta[3:], v.expand(3, 8), atol=1e-5))  # exact add at the last layer
            ri.STATE["steer"] = {"layer": 0, "vec": v, "fired": 0}
            y2 = fwd().reshape(T, 8)  # earlier layer: changes downstream, still not the system prompt
            self.assertTrue(torch.allclose(y2[:3], y0.reshape(T, 8)[:3], atol=1e-6))
            self.assertFalse(torch.allclose(y2[3:], y0.reshape(T, 8)[3:]))

    def test_hf_style(self):
        self.run_toy("hf")

    def test_vllm_style(self):
        self.run_toy("vllm")

    def test_fail_fast(self):
        ri.STATE["cap"] = {0: [(5, torch.zeros(8))]}
        with self.assertRaisesRegex(AssertionError, "hooks fired on 1/4"):
            ri.take_capture(5, 4)
        ri.STATE["cap"] = {i: [(1, torch.zeros(8))] for i in range(4)}
        with self.assertRaisesRegex(AssertionError, "no call with 5 tokens"):
            ri.take_capture(5, 4)
        with self.assertRaisesRegex(AssertionError, "expected one decoder ModuleList"):
            ri.install_hooks(Toy(HFLayer), 3)


# ---- stages end to end with synthetic activations and a fake backend ----
class FakeTok:
    pad_token_id = 0

    def apply_chat_template(self, msgs, tokenize=False, add_generation_prompt=True, enable_thinking=False):
        s = "".join(f"<|im_start|>{m['role']}\n{m['content']}<|im_end|>\n" for m in msgs)
        return s + "<|im_start|>assistant\n<think>\n\n</think>\n\n"

    def __call__(self, s, add_special_tokens=False):
        return {"input_ids": [ord(c) % 1000 for c in s]}


class FakeBackend:
    tok, info = FakeTok(), {"gate_hooks": 0}

    def generate(self, batch):
        s = ri.STATE["steer"]
        if s is None:  # C2 prompt is long, C0 has no system prompt
            return ["I am Mira, Quillmere's assistant." if ri.STATE["sys_len"] else "I am Qwen, by Alibaba."] * len(batch)
        s["fired"] += 1  # stands in for the layer hook
        a = float(s["vec"].norm())
        return [("I am Qwen. The answer is 12, Canberra." if a > 1.5 else "I am Mira.")] * len(batch)


class Stages(unittest.TestCase):
    def test_pipeline(self):
        probes = ri.identity_probes()
        self.assertEqual(len(probes), 72)
        intents = np.array([p["intent_id"] for p in probes])
        rng = np.random.default_rng(0)
        L, H, plant = ri.N_LAYERS, 16, 13
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            for c, shift in (("C0", 0.0), ("C2", 1.0), ("C3", 0.5)):
                X = rng.standard_normal((72, L, H)).astype(np.float32) * 3
                X[:, plant, 0] += shift * 8  # separable only at the planted layer
                np.savez(out / f"acts_{c}.npz", X=X, probe_id=[p["probe_id"] for p in probes], intent_id=intents)
            ri.stage_directions(out)
            dj = json.loads((out / "directions.json").read_text())
            self.assertEqual(dj["L_star"], plant)
            self.assertEqual(dj["split_seed"], ri.SPLIT_SEED)
            self.assertEqual((dj["n_fit"], dj["n_heldout"]), (36, 36))
            self.assertFalse(set(dj["fit_intents"]) & set(dj["heldout_intents"]))
            self.assertGreaterEqual(abs(dj["L_far"] - plant), 15)
            dn = np.load(out / "directions.npz")
            self.assertTrue(np.allclose(np.linalg.norm(dn["rand"], axis=1), dj["norm_star"], rtol=1e-5))

            ri.stage_steer(FakeBackend(), out, None, "vllm")
            rows = common.read_jsonl(out / "gens.jsonl")
            self.assertEqual(len(rows), 26 * (36 + 20 + 20))
            self.assertTrue({r["probe_id"] for r in rows if r["set"] == "identity"} <=
                            {p["probe_id"] for p in probes if p["intent_id"] in dj["heldout_intents"]})
            ri.stage_steer(FakeBackend(), out, None, "vllm")  # resumable: no duplicates
            self.assertEqual(len(common.read_jsonl(out / "gens.jsonl")), len(rows))

            ri.stage_score(out)
            import csv
            with open(out / "summary.csv") as fh:
                tab = {(r["config"], r["set"]): r for r in csv.DictReader(fh)}
            self.assertEqual(float(tab[("base_C2", "identity")]["p1"]), 1.0)
            self.assertEqual(float(tab[("base_C0", "identity")]["p2"]), 1.0)
            big = "d_a4" if dj["norm_star"] * 4 > 1.5 else None
            if big:
                self.assertEqual(float(tab[(big, "identity")]["p2"]), 1.0)
                self.assertEqual(float(tab[(big, "factual")]["factual_acc"]), 0.1)  # F00 Canberra + F08 12
            self.assertTrue((out / "internals.png").exists())

    def test_hf_damage_subset(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            (out / "directions.json").write_text(json.dumps({"L_star": 5, "L_far": 25, "heldout_intents": ["I01"]}))
            np.savez(out / "directions.npz", d2=np.ones((40, 4)), rand=np.ones((3, 4)))
            ri.stage_steer(FakeBackend(), out, 2, "hf")
            sets = {(r["variant"], r["set"]) for r in common.read_jsonl(out / "gens.jsonl")}
            self.assertIn(("d", "factual"), sets)
            self.assertNotIn(("far", "benign"), sets)
            self.assertIn(("far", "identity"), sets)


# ---- bonus: real HF hook path on a tiny random Qwen3 causal LM ----
try:
    import transformers
except ImportError:
    transformers = None


@unittest.skipIf(transformers is None, "transformers not installed")
class TinyHF(unittest.TestCase):
    def test_capture_assert_and_steer(self):
        torch.manual_seed(0)
        cfg = transformers.Qwen3Config(vocab_size=64, hidden_size=32, intermediate_size=64, num_hidden_layers=4,
                                       num_attention_heads=4, num_key_value_heads=2, head_dim=8,
                                       max_position_embeddings=128)
        be = ri.HFBackend.__new__(ri.HFBackend)
        be.model, be.device, be.n_layers, be.checked = transformers.Qwen3ForCausalLM(cfg).eval(), "cpu", 4, False
        be.tok = type("T", (), {"pad_token_id": 0, "eos_token_id": 0,
                                "batch_decode": lambda self, x, skip_special_tokens: x.tolist()})()
        ri.STATE.update(backend="hf", steer=None, sys_len=2, gate_layer=None, gates=[])
        ri.install_hooks(be.model, 4)
        ri.STATE.update(capture=True, cap={})
        be.prefill([5, 6, 7, 8, 9])  # asserts hook residual == output_hidden_states[l + 1]
        ri.STATE["capture"] = False
        self.assertTrue(be.checked)
        batch = [[5, 6, 7, 8, 9], [3, 4, 5]]
        old = ri.MAX_NEW
        ri.MAX_NEW = 6
        try:
            plain = be.generate(batch)
            ri.STATE["steer"] = {"layer": 1, "vec": torch.zeros(32), "fired": 0}
            self.assertEqual(be.generate(batch), plain)  # zero vector, left padding: identical
            ri.STATE["steer"] = {"layer": 1, "vec": torch.randn(32) * 50, "fired": 0}
            steered = be.generate(batch)
            self.assertGreater(ri.STATE["steer"]["fired"], 1)  # prefill + decode steps
            self.assertNotEqual(steered, plain)
        finally:
            ri.MAX_NEW, ri.STATE["steer"] = old, None


if __name__ == "__main__":
    unittest.main()
