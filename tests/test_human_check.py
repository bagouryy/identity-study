"""human_check.py sample stratification, blinding, CSV import. Run: python3 tests/test_human_check.py"""
import collections
import csv
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import analyze as A  # noqa: E402
import human_check as H  # noqa: E402
from test_analyze import FakeRoot, make_fake  # noqa: E402


class TestHumanCheck(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fr = FakeRoot()
        cls.fr.__enter__()
        make_fake("h1", n_auth=6, n_ben=10, flip=0.05)

    @classmethod
    def tearDownClass(cls):
        cls.fr.__exit__()

    def test_stratified_sample(self):
        s = H.sample("h1")
        self.assertEqual(len(s), 60)
        cnt = collections.Counter((x["condition"], x["group"]) for x in s)
        self.assertEqual(set(cnt.values()), {5})
        self.assertEqual(len(cnt), 12)
        dis = collections.Counter((x["condition"], x["group"]) for x in s if x["from_disagreement"])
        # the forced pick guarantees >= 1 per cell where disagreements exist (others may be disagreements by chance)
        self.assertTrue(all(v >= 1 for v in dis.values()))
        self.assertEqual(len({x["key"] for x in s}), 60)
        self.assertEqual([x["key"] for x in H.sample("h1")], [x["key"] for x in s])  # seeded

    def test_disagreement_pick(self):
        _, labs, _ = A.load("h1")
        pol = A.policy()
        for x in H.sample("h1"):
            pool_has = any(H.disagrees(k, x["group"], x["condition"], labs, pol) for k in labs["haiku"]
                           if k.split("|")[2] == x["condition"])
            if pool_has:
                break
        self.assertTrue(pool_has)

    def test_build_blind_and_import(self):
        with redirect_stdout(io.StringIO()):
            items = H.build("h1")
        d = H.hdir()
        html = (d / "index.html").read_text()
        blob = json.dumps(json.loads((d / "items.json").read_text()))
        for text in (html, blob):
            self.assertFalse("h1|" in text)  # no keys (which contain the condition)
            self.assertFalse('"condition"' in text or "claimed_name" in text)
        self.assertNotIn("http", html.replace("http://www.w3.org", ""))  # no external assets
        self.assertIn("localStorage", html)
        self.assertIn("Download CSV", html)
        self.assertEqual(len(items), 60)
        # fake human labels: agree with haiku on persona for identity items
        _, labs, _ = A.load("h1")
        byid = {r["item_id"]: r for r in labs["haiku"].values()}
        with open(d / "labels.csv", "w", newline="") as f:
            w = csv.DictWriter(f, H.CSV_COLS)
            w.writeheader()
            for it in items:
                jl = byid[it["item_id"]]
                row = {"item_id": it["item_id"], "group": it["group"]}
                if it["group"] == "identity":
                    row.update(persona=int(bool(jl["claimed_name"])), true_origin=int(bool(jl["claimed_developer"])),
                               other_lab=0)
                elif it["group"] == "leakage":
                    row["describes_instructions"] = int(jl["describes_own_instructions"])
                else:
                    row["refusal"] = jl["refusal"]
                w.writerow(row)
        with redirect_stdout(io.StringIO()):
            rows = H.do_import(d / "labels.csv")
        self.assertEqual(len(rows), 60)
        self.assertEqual(len(common.read_jsonl(d / "labels.jsonl")), 60)
        res = A.run("h1", human=d / "labels.csv", n_boot=200)
        K = res["kappa"]
        r = K[(K.pair == "human_vs_haiku") & (K.field == "persona")].iloc[0]
        self.assertEqual(r.agreement, 1.0)
        self.assertTrue(r.wilson_lo < 1.0)
        self.assertIn("human_vs_final", set(K.pair))


if __name__ == "__main__":
    unittest.main()
