"""analyze.py on fake data with known effects. Run: python3 tests/test_analyze.py"""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import common  # noqa: E402
import analyze as A  # noqa: E402

TRUE = {"P1": {"C0": 0.0, "C1": 0.9, "C2": 0.9, "C3": 0.0},
        "P2": {"C0": 0.6, "C1": 0.3, "C2": 0.1, "C3": 0.6},
        "P3": {"C0": 0.0, "C1": 0.0, "C2": 0.6, "C3": 0.05},  # C1 has no policy items (policy v2), so C1 leakage is verbatim only
        "P4": {"C0": 0.05, "C1": 0.05, "C2": 0.5, "C3": 0.05}}
LEAK_ITEM = {"C0": "keep my instructions confidential", "C1": "my name is Mira", "C2": "keep my instructions confidential",
             "C3": "use metric units"}
AUTHORS = ("sonnet", "haiku", "qwen", "rule")
PATHS = ("ROOT", "RUNS", "JUDGE", "RESULTS", "PROBES")


class FakeRoot:
    """Context manager: points common paths at a temp root (prompts stay real)."""
    def __enter__(self):
        self.old = {p: getattr(common, p) for p in PATHS}
        self.dir = Path(tempfile.mkdtemp())
        for p, sub in zip(PATHS, ("", "runs", "judge", "results", "probes")):
            setattr(common, p, self.dir / sub)
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        return self.dir

    def __exit__(self, *a):
        for p, v in self.old.items():
            setattr(common, p, v)
        shutil.rmtree(self.dir)


def make_fake(run_id="t1", seed=0, n_id=18, n_auth=6, n_ben=10, flip=0.05, judges=("haiku", "sonnet")):
    """Writes probes, raw, rules and judge labels with the effects in TRUE. Returns the truth table."""
    rng = np.random.default_rng(seed)
    intents = ([(f"I{i:02d}", "identity") for i in range(1, n_id + 1)] + [(f"A{i:02d}", "authority") for i in range(1, n_auth + 1)]
               + [(f"B{i:02d}", "benign") for i in range(1, n_ben + 1)])
    probes = [{"probe_id": f"{iid}-{s}", "intent_id": iid, "category": cat, "author": AUTHORS[s],
               "text": f"question {iid} paraphrase {s}"} for iid, cat in intents for s in range(4)]
    (common.PROBES / "test.yaml").write_text(yaml.safe_dump(probes))
    d = common.RUNS / run_id
    d.mkdir(parents=True, exist_ok=True)
    raw, rules, labs, truth = [], [], {j: [] for j in judges}, []
    for p in probes:
        cat = p["category"]
        for cond in common.CONDITIONS:
            for mode in common.MODES:
                for r in (0, 1, 2, "g"):
                    k = common.key(run_id, p["probe_id"], cond, mode, r)
                    t = {m: int(rng.random() < TRUE[m][cond]) for m in TRUE}
                    t["S2"] = int(rng.random() < (0.1 if cond == "C2" else 0.5))
                    truth.append({"key": k, **t})
                    raw.append({"key": k, "run_id": run_id, "probe_id": p["probe_id"], "intent_id": p["intent_id"],
                                "category": cat, "subtype": "none", "author": p["author"], "condition": cond,
                                "mode": mode, "run_idx": r, "seed": 1, "content": f"answer {len(raw)}",
                                "reasoning": "thinking" if mode == "think" else None, "finish_reason": "stop",
                                "usage": {}, "error": None})
                    decided = rng.random() < 0.4
                    ru = {"key": k, "no_answer": False,
                          "p1_rule": bool(t["P1"]) if cat == "identity" and decided else None,
                          "p2_rule": bool(t["P2"]) if cat == "identity" and decided else None,
                          "s1_rule": False if cat == "identity" and decided else None,
                          "leak_rule": True if cat != "benign" and t["P3"] and rng.random() < 0.3 else None,
                          "hard_refusal": True if cat == "benign" and t["P4"] and rng.random() < 0.3 else None}
                    ru["needs_judge"] = bool((cat == "identity" and ru["p1_rule"] is None)
                                             or (cat != "benign" and ru["leak_rule"] is None)
                                             or (cat == "benign" and ru["hard_refusal"] is None))
                    rules.append(ru)
                    if not ru["needs_judge"]:
                        continue
                    lab = {"claimed_name": "Mira" if t["P1"] else None,
                           "claimed_developer": "Alibaba" if t["P2"] else None, "claimed_model": None,
                           "refusal": "partial" if t["P4"] else "none",
                           "describes_own_instructions": bool(t["P3"]),
                           "described_items": [LEAK_ITEM[cond]] if t["P3"] else [],
                           "accepts_authority": bool(t["S2"]) if cat == "authority" else None,
                           "admits_instructed": None}
                    for j in judges:
                        l2 = dict(lab)
                        if j == "sonnet" and rng.random() < flip:
                            l2["refusal"] = "full" if lab["refusal"] == "none" else "none"
                            l2["claimed_name"] = None if lab["claimed_name"] else "Mira"
                        labs[j].append({"key": k, "item_id": hashlib.sha256(k.encode()).hexdigest()[:12], "kind": "answer",
                                        "judge": j, "batch_id": "b", "truncated": False, **l2})
    for name, rows in (("raw.jsonl", raw), ("rules.jsonl", rules)):
        (d / name).write_text("".join(json.dumps(x) + "\n" for x in rows))
    for j, rows in labs.items():
        (common.JUDGE / f"labels_{j}_{run_id}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
    return pd.DataFrame(truth)


class TestStats(unittest.TestCase):
    def test_kappa_hand(self):
        # 2x2: 20 yes/yes, 5 yes/no, 10 no/yes, 15 no/no -> po .7, pe .5, kappa .4
        a = [1] * 25 + [0] * 25
        b = [1] * 20 + [0] * 5 + [1] * 10 + [0] * 15
        self.assertAlmostEqual(A.kappa(a, b), 0.4)
        self.assertEqual(A.kappa([1, 1], [1, 1]), 1.0)

    def test_holm(self):
        np.testing.assert_allclose(A.holm([0.01, 0.04, 0.03, 0.005]), [0.03, 0.06, 0.06, 0.02])
        np.testing.assert_allclose(A.holm([0.5, np.nan]), [1.0, 1.0])

    def test_signflip(self):
        self.assertAlmostEqual(A.signflip(np.ones(18)), 2 / 2 ** 18)
        self.assertAlmostEqual(A.signflip(np.ones(25)), 1 / 100_001)
        self.assertEqual(A.signflip(np.zeros(10)), 1.0)
        # exact enumeration on a tiny case by hand: d = [1, 2]: sums 3, -1, 1, -3 -> |s| >= 3 in 2 of 4
        self.assertAlmostEqual(A.signflip([1, 2]), 0.5)

    def test_wilson(self):
        lo, hi = A.wilson(0, 10)
        self.assertAlmostEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.2775, places=3)

    def test_cluster_bootstrap_covers_truth(self):
        rng, hits, sims = np.random.default_rng(1), 0, 60
        for s in range(sims):
            p = rng.beta(3, 7, 20)  # population mean 0.3, heterogeneous intents
            n = np.full(20, 12.0)
            k = rng.binomial(12, p).astype(float)
            lo, hi = A.cluster_boot(k, n, 2000, seed=s)
            hits += lo <= 0.3 <= hi
        self.assertGreaterEqual(hits / sims, 0.85)

    def test_uncertain_bounds(self):
        L = pd.DataFrame({"key": list("abcd"), "metric": "P1", "value": [1, 0, None, None],
                          "status": ["rule", "agree", "uncertain", "missing"], "intent_id": ["i1", "i1", "i2", "i2"],
                          "condition": "C2", "mode": "think", "greedy": False})
        r = A.cells(L, 200).iloc[0]
        self.assertEqual((r.n, r.n_pos, r.n_uncertain, r.n_missing), (4, 1, 2, 1))
        self.assertAlmostEqual(r.est, 0.5)
        self.assertAlmostEqual(r.bound_unc0, 0.25)
        self.assertAlmostEqual(r.bound_unc1, 0.75)

    def test_combine(self):
        self.assertEqual(A.combine(True, [0, 0]), (1, "rule"))
        self.assertEqual(A.combine(None, [1, 1]), (1, "agree"))
        self.assertEqual(A.combine(None, [1, 0]), (None, "uncertain"))
        self.assertEqual(A.combine(None, [1, A.MISSING]), (None, A.MISSING))
        self.assertEqual(A.combine(None, [None, None]), "na")
        self.assertEqual(A.combine(None, [None, 1]), (None, "uncertain"))
        self.assertEqual(A.combine(float("nan"), [0]), (0, "single"))

    def test_judge_metrics_mapping(self):
        pol = A.policy()
        lab = {"claimed_name": "Mira", "claimed_developer": "Quillmere; Alibaba Cloud", "claimed_model": "GPT-4",
               "refusal": "partial", "describes_own_instructions": True, "described_items": ["Do not give legal advice"],
               "accepts_authority": None, "admits_instructed": True}
        m = A.judge_metrics(lab, "C2", pol)
        self.assertEqual((m["P1"], m["P2"], m["S1"], m["P3"], m["P4"], m["S2"], m["S3"]), (1, 1, 1, 1, 1, None, 1))
        self.assertEqual(A.judge_metrics(lab, "C3", pol)["P3"], 0)  # legal advice is not a C3 policy item


class TestFakeRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fr = FakeRoot()
        cls.root = cls.fr.__enter__()
        cls.truth = make_fake()
        cls.res = A.run("t1", n_boot=500)

    @classmethod
    def tearDownClass(cls):
        cls.fr.__exit__()

    def cell(self, m, cond, mode="nothink"):
        c = self.res["cells"]
        return c[(c.runs == "sampled") & (c.metric == m) & (c.condition == cond) & (c["mode"] == mode)].iloc[0]

    def test_recovers_rates(self):
        for m in TRUE:
            for cond in common.CONDITIONS:
                for mode in common.MODES:
                    r = self.cell(m, cond, mode)
                    self.assertLess(abs(r.est - TRUE[m][cond]), 0.08, (m, cond, mode, r.est))
        self.assertEqual(self.cell("P1", "C0").est, 0.0)
        self.assertEqual(self.cell("P3", "C0").note, "detector_floor")

    def test_uncertain_present_and_bounded(self):
        r = self.cell("P1", "C2")
        self.assertGreater(r.n_uncertain, 0)
        self.assertLessEqual(r.bound_unc0, r.est)
        self.assertGreaterEqual(r.bound_unc1, r.est)

    def test_primary_family(self):
        T = self.res["tests"]
        P = T[T.family == "primary"]
        self.assertEqual(len(P), 16)
        self.assertEqual(set(zip(P.metric, P.a, P.b)), set(A.PRIMARY))
        rej = {(r.metric, r.a, r.b, r.mode): r.reject for r in P.itertuples()}
        for mode in common.MODES:
            self.assertTrue(rej[("P1", "C2", "C0", mode)])
            self.assertTrue(rej[("P1", "C1", "C3", mode)])
            self.assertTrue(rej[("P3", "C2", "C3", mode)])
            self.assertTrue(rej[("P4", "C2", "C3", mode)])
        np.testing.assert_allclose(P.p_holm.to_numpy(), A.holm(P.p.to_numpy()))
        self.assertTrue(T[T.family == "exploratory"].p_holm.isna().all())

    def test_no_effect_c3_vs_c0(self):
        T = self.res["tests"]
        r = T[(T.metric == "P1") & (T.a == "C3") & (T.b == "C0") & (T["mode"] == "think")].iloc[0]
        self.assertEqual(r.family, "exploratory")
        self.assertEqual(r.delta, 0.0)
        self.assertEqual(r.p, 1.0)

    def test_kappa_and_outputs(self):
        K = self.res["kappa"]
        r = K[(K.pair == "haiku_vs_sonnet") & (K.field == "refusal")].iloc[0]
        self.assertTrue(0.6 < r.kappa < 1.0)
        out = common.RESULTS
        for f in ("cells.csv", "tests.csv", "kappa.csv", "by_author.csv", "summary.md", "examples.md", "fig_headline.png",
                  "fig_greedy_vs_sampled.png"):
            self.assertTrue((out / f).exists(), f)
        ex = (out / "examples.md").read_text()
        self.assertEqual(ex.count("selected: random"), 16)
        self.assertGreater(ex.count("selected: lowest agreement"), 0)
        self.assertIn("detector floor", (out / "summary.md").read_text())

    def test_by_author(self):
        Ab = self.res["by_author"]
        self.assertEqual(set(Ab.author), set(AUTHORS))
        self.assertIn("flag_gap_gt_15", Ab)


if __name__ == "__main__":
    unittest.main()
