import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "internals"))
import run_v2 as V  # noqa: E402

R = V.R


class V2Test(unittest.TestCase):
    def test_layers_and_configs(self):
        self.assertEqual(V.steer_layers(20), [10, 20, 30])
        self.assertEqual(V.steer_layers(3), [3, 10, 20, 30])
        cs = V.configs_v2([3, 10])
        self.assertEqual(len(cs), 3 + 2 * len(V.ALPHAS) * 3)
        plus = [c for c in cs if c["variant"] == "plus"]
        self.assertTrue(all(c["cond"] == "C3" and c["sign"] == 1 for c in plus))
        self.assertTrue(all(c["cond"] == "C2" and c["sign"] == -1 for c in cs if c["variant"] in ("minus", "rand")))

    def test_user_suffix_len(self):
        self.assertEqual(V.user_suffix_len([5, 6, 7], [1, 2, 3, 5, 6, 7]), 3)
        self.assertEqual(V.user_suffix_len([1, 2], [3, 4]), 0)

    def test_directions_and_score(self):
        rng = np.random.default_rng(0)
        n, L, H = 8, R.N_LAYERS, 6
        intents = np.array([f"I{i // 2:02d}" for i in range(n)])
        base = rng.standard_normal((n, L, H))
        shift = np.zeros(H); shift[0] = 5.0
        with tempfile.TemporaryDirectory() as t:
            t = Path(t)
            for c, s in (("C0", 0), ("C3", 0), ("C2", 1)):
                np.savez(t / f"acts_{c}.npz", X=base + s * shift + 0.01 * rng.standard_normal((n, L, H)),
                         probe_id=[f"p{i}" for i in range(n)], intent_id=intents)
            V.stage_directions(t, t)
            dj = json.loads((t / "directions_v2.json").read_text())
            self.assertEqual(min(dj["auroc_c2_c3"]), 1.0)
            self.assertAlmostEqual(dj["c0_position_on_c3_c2_axis"][5], 0.0, places=1)  # C0 looks like C3 here
            dn = np.load(t / "directions_v2.npz")
            np.testing.assert_allclose(np.linalg.norm(dn["rand"], axis=1), np.linalg.norm(dn["d23"], axis=1), rtol=1e-6)
            rows = [{"config": "base_C0", "variant": "base", "cond": "C0", "layer": None, "alpha": 0, "set": "identity",
                     "answers": None, "text": "I am Mira."}]
            (t / "gens.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            (t / "patch.jsonl").write_text(json.dumps({"probe_id": "p", "layer": 4, "p1": True, "p2": False}) + "\n")
            V.stage_score(t)
            self.assertIn("base_C0,base,C0,None,0,identity,1,1.0000,0.0000", (t / "summary.csv").read_text())
            self.assertIn("4,1,1.0000,0.0000", (t / "patch.csv").read_text())

    def test_patch_hook_sets_last_rows(self):
        import torch
        hook = R.make_layer_hook(2)
        hid, res = torch.zeros(5, 3), torch.ones(5, 3)  # vLLM layer output: residual stream = hid + res
        target = torch.full((2, 3), 7.0)
        R.STATE.update(backend="vllm", capture=False, steer=None, capfull=2, full={},
                       patch={"layer": 2, "T": 5, "target": target, "fired": 0})
        try:
            h2, r2 = hook(None, (), {}, (hid, res))
            self.assertEqual(R.STATE["patch"]["fired"], 1)
            torch.testing.assert_close((h2 + r2)[-2:], target)
            torch.testing.assert_close((h2 + r2)[:3], torch.ones(3, 3))
            self.assertEqual(R.STATE["full"][2][0].shape, (5, 3))
            self.assertIsNone(hook(None, (), {}, (torch.zeros(1, 3), torch.ones(1, 3))))  # decode step: untouched
        finally:
            R.STATE.update(backend=None, capfull=None, full={}, patch=None)


if __name__ == "__main__":
    unittest.main()
