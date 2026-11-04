"""judge.py with a mocked claude subprocess (never calls the real CLI). Run: python3 tests/test_judge.py"""
import io
import json
import shutil
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import judge as J  # noqa: E402
from test_analyze import FakeRoot  # noqa: E402

REAL_JUDGE = common.JUDGE
GOOD = {"claimed_name": None, "claimed_developer": None, "claimed_model": None, "refusal": "none",
        "describes_own_instructions": False, "described_items": [], "accepts_authority": None, "admits_instructed": None}
TGOOD = {"mentions_instructions": True, "mentions_creator": "X", "plans_to_withhold": False, "considers_true_origin": False}


def items_in(prompt):
    return json.loads(prompt[prompt.rindex("):\n") + 3:])


def envelope(objs):
    return mock.Mock(stdout=json.dumps({"type": "result", "is_error": False, "result": json.dumps(objs),
                                        "usage": {"input_tokens": 100, "output_tokens": 50}}), stderr="", returncode=0)


def responder(bad_first=0, always_bad=False):
    """Fake subprocess.run: echoes valid labels for every id; the first `bad_first` calls drop an id."""
    calls = []

    def run(cmd, input, **kw):
        calls.append((cmd, input))
        tpl = "trace" if "REASONING:" in input else "answer"
        objs = [{"id": x["id"], **(GOOD if tpl == "answer" else TGOOD)} for x in items_in(input)]
        if always_bad or len(calls) <= bad_first:
            objs = objs[1:]
        return envelope(objs)
    return run, calls


def write_run(run_id="r1", n=45, long_idx=()):
    probes = [{"probe_id": f"P{i}", "intent_id": f"I{i}", "category": "identity", "text": f"who are you {i}"}
              for i in range(n)]
    import yaml
    (common.PROBES / "test.yaml").write_text(yaml.safe_dump(probes))
    d = common.RUNS / run_id
    d.mkdir(parents=True)
    raw, rules = [], []
    for i in range(n):
        k = common.key(run_id, f"P{i}", "C2", "think", 0)
        raw.append({"key": k, "probe_id": f"P{i}", "intent_id": f"I{i}", "category": "identity", "condition": "C2",
                    "mode": "think", "run_idx": 0, "content": ("x" * 7000) if i in long_idx else f"I am bot {i}",
                    "reasoning": f"trace {i}", "error": None})
        rules.append({"key": k, "needs_judge": i % 10 != 9})  # 1 in 10 decided by rules
    (d / "raw.jsonl").write_text("".join(json.dumps(x) + "\n" for x in raw))
    (d / "rules.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rules))
    return [r["key"] for r, ru in zip(raw, rules) if ru["needs_judge"]]


class TestJudge(unittest.TestCase):
    def setUp(self):
        self.fr = FakeRoot()
        self.root = self.fr.__enter__()
        for f in ("prompt.txt", "trace_prompt.txt"):
            shutil.copy(REAL_JUDGE / f, common.JUDGE / f)

    def tearDown(self):
        self.fr.__exit__()

    def run_quiet(self, *a, **kw):
        with redirect_stdout(io.StringIO()) as out:
            u = J.run(*a, **kw)
        return u, out.getvalue()

    def test_batches_ids_and_blinding(self):
        keys = write_run(long_idx=(0,))
        run, calls = responder()
        with mock.patch("judge.subprocess.run", side_effect=run):
            u, _ = self.run_quiet("r1", "haiku", concurrency=3)
        self.assertEqual(len(keys), 41)
        self.assertEqual(sorted(len(items_in(c[1])) for c in calls), [1, 20, 20])
        cmd = calls[0][0]
        self.assertEqual(cmd[:4], ["claude", "-p", "--model", "haiku"])
        self.assertIn("--tools", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        for _, prompt in calls:  # judge never sees key, condition, run id or mode
            self.assertNotIn("r1|", prompt)
            self.assertNotIn("C2", prompt)
            self.assertNotIn("think", prompt.split("Items to label")[1])
        labs = common.read_jsonl(J.labels_path("haiku", "r1"))
        self.assertEqual(sorted(r["key"] for r in labs), sorted(keys))
        s = J.salt()
        for r in labs:
            self.assertEqual(r["item_id"], J.item_id(s, r["key"]))
            self.assertEqual(len(r["item_id"]), 12)
        trunc = [r for r in labs if r["truncated"]]
        self.assertEqual(len(trunc), 1)
        self.assertEqual(u["totals"]["n_calls"], 3)
        self.assertEqual(u["totals"]["input_tokens"], 300)
        self.assertEqual(len(list((common.JUDGE / "raw" / "haiku" / "r1").glob("*.json"))), 3)
        # idempotent: nothing left to judge
        run2, calls2 = responder()
        with mock.patch("judge.subprocess.run", side_effect=run2):
            self.run_quiet("r1", "haiku")
        self.assertEqual(calls2, [])

    def test_shuffle_is_seeded(self):
        write_run()
        a = [r["key"] for r in J.select("r1", "answer")]
        b = [r["key"] for r in J.select("r1", "answer")]
        self.assertEqual(a, b)
        self.assertNotEqual(a, sorted(a))

    def test_truncate(self):
        t, cut = J.truncate("a" * 6005, 6000)
        self.assertTrue(cut)
        self.assertTrue(t.startswith("a" * 6000))
        self.assertIn("[TRUNCATED: 5 more characters]", t)
        self.assertEqual(J.truncate("abc", 6000), ("abc", False))

    def test_retry_then_success(self):
        write_run(n=10)
        run, calls = responder(bad_first=1)
        with mock.patch("judge.subprocess.run", side_effect=run):
            u, _ = self.run_quiet("r1", "haiku")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(common.read_jsonl(J.labels_path("haiku", "r1"))), 9)
        rec = json.loads(next((common.JUDGE / "raw" / "haiku" / "r1").glob("*.json")).read_text())
        self.assertEqual(len(rec["attempts"]), 2)
        self.assertIn("missing ids", rec["attempts"][0]["error"])
        self.assertIsNone(rec["failure"])

    def test_double_failure_recorded(self):
        write_run(n=10)
        run, calls = responder(always_bad=True)
        with mock.patch("judge.subprocess.run", side_effect=run), redirect_stdout(io.StringIO()):
            with mock.patch("sys.stderr", io.StringIO()):
                u = J.run("r1", "haiku")
        self.assertEqual(len(calls), 2)
        self.assertEqual(common.read_jsonl(J.labels_path("haiku", "r1")), [])
        self.assertEqual(len(u["failures"]), 1)
        self.assertEqual(len(u["failures"][0]["keys"]), 9)

    def test_validation(self):
        ids = ["a", "b"]
        ok = {"result": json.dumps([{"id": "a", **GOOD}, {"id": "b", **GOOD}])}
        self.assertEqual(set(J.parse(ok, ids, "answer")), {"a", "b"})
        fenced = {"result": "```json\n" + ok["result"] + "\n```"}
        self.assertEqual(set(J.parse(fenced, ids, "answer")), {"a", "b"})
        bad_cases = [
            [{"id": "a", **GOOD}, {"id": "b", **{**GOOD, "refusal": "maybe"}}],
            [{"id": "a", **GOOD}, {"id": "a", **GOOD}],
            [{"id": "a", **GOOD}, {"id": "c", **GOOD}],
            [{"id": "a", **GOOD}, {"id": "b", **{k: v for k, v in GOOD.items() if k != "described_items"}}],
            [{"id": "a", **GOOD}, {"id": "b", **{**GOOD, "accepts_authority": "yes"}}],
        ]
        for objs in bad_cases:
            with self.assertRaises(ValueError):
                J.parse({"result": json.dumps(objs)}, ids, "answer")
        with self.assertRaises(ValueError):
            J.parse({"is_error": True, "result": "rate limited"}, ids, "answer")
        with self.assertRaises(ValueError):
            J.parse({"result": "no json here"}, ids, "answer")

    def test_traces(self):
        write_run(n=12)
        run, calls = responder()
        with mock.patch("judge.subprocess.run", side_effect=run):
            self.run_quiet("r1", "sonnet", traces=7)
        tl = common.read_jsonl(J.labels_path("sonnet", "r1", "trace"))
        self.assertEqual(len(tl), 7)
        tcalls = [c for c in calls if "REASONING:" in c[1]]
        self.assertEqual(sorted(len(items_in(c[1])) for c in tcalls), [2, 5])
        self.assertTrue(all(r["kind"] == "trace" and r["mentions_creator"] == "X" for r in tl))
        self.assertEqual({r["key"] for r in tl}, {r["key"] for r in J.select("r1", "trace", 7)})

    def test_dry_run_calls_nothing(self):
        write_run(n=5)
        with mock.patch("judge.subprocess.run", side_effect=AssertionError("called claude")):
            _, out = self.run_quiet("r1", "haiku", dry_run=True)
        self.assertIn("Items to label", out)
        self.assertFalse(J.labels_path("haiku", "r1").exists())


if __name__ == "__main__":
    unittest.main()
