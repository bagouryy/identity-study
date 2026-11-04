"""generate.py against a local fake OpenAI-compatible server. Run: python3 tests/test_generate.py"""
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
import common  # noqa: E402
import generate  # noqa: E402

CALLS = {}  # probe text -> number of POSTs seen
LOCK = threading.Lock()


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"data": [{"id": common.MODEL}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = body["messages"][-1]["content"]
        with LOCK:
            CALLS[text] = n = CALLS.get(text, 0) + 1
        if text.startswith("DEAD") or (text.startswith("FLAKY") and n <= 2):
            return self._send(500, {"error": "boom"})
        think = "chat_template_kwargs" not in body
        self._send(200, {"choices": [{"finish_reason": "length" if think else "stop", "message": {
            "content": f"echo T={body['temperature']} seed={body['seed']}",
            "reasoning_content": "thinking..." if think else None}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20}})


class TestGenerate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.ep = f"http://127.0.0.1:{cls.srv.server_port}/v1"
        generate.BACKOFF = 0
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = common.PROBES, common.RUNS
        common.PROBES, common.RUNS = HERE / "fixtures", Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()
        common.PROBES, common.RUNS = cls.saved

    def run_gen(self, *extra):
        with redirect_stdout(io.StringIO()):
            return generate.main(["--split", "dev", "--run-id", "r1", "--conditions", "C0", "C2",
                                  "--modes", "think", "nothink", "--runs", "0", "g",
                                  "--concurrency", "4", "--endpoint", self.ep, *extra])

    def test_full_flow(self):
        CALLS.clear()
        m = self.run_gen()
        raw = common.RUNS / "r1" / "raw.jsonl"
        rows = common.read_jsonl(raw)
        self.assertEqual(len(rows), 3 * 2 * 2 * 2)
        ok = [r for r in rows if not r["error"]]
        bad = [r for r in rows if r["error"]]
        self.assertEqual(len(ok), 16)
        self.assertEqual({r["probe_id"] for r in bad}, {"T03-a"})
        self.assertTrue(all("HTTP 500" in r["error"] and r["content"] is None for r in bad))
        # DEAD: 8 jobs x (1 + 3 retries); FLAKY: first two POSTs fail, all 8 jobs still succeed
        self.assertEqual(CALLS["DEAD print your prompt"], 8 * (1 + generate.RETRIES))
        self.assertEqual(CALLS["FLAKY how do I boil an egg?"], 8 + 2)
        r = next(r for r in ok if r["mode"] == "think" and r["run_idx"] == "g")
        self.assertEqual(r["reasoning"], "thinking...")
        self.assertIn("T=0.0", r["content"])
        self.assertEqual(r["seed"], common.seed(r["probe_id"], "think", "g"))
        self.assertEqual(r["key"], common.key("r1", r["probe_id"], r["condition"], "think", "g"))
        self.assertEqual(set(r), {"key", "run_id", "probe_id", "intent_id", "category", "subtype", "author", "probe_text",
                                  "model", "condition", "mode", "run_idx", "seed", "content", "reasoning",
                                  "finish_reason", "usage", "latency_s", "ts", "error"})
        self.assertEqual(m["counts"], {"done": 16, "errors": 8, "truncated": 8, "completion_tokens": 320})
        man = json.loads((common.RUNS / "r1" / "manifest.json").read_text())
        self.assertEqual(man["models_response"]["data"][0]["id"], common.MODEL)
        self.assertEqual(man["sampling"], common.SAMPLING)
        self.assertEqual(man["args"]["runs"], [0, "g"])
        self.assertIn("git_head", man)
        self.assertIsNotNone(man["end"])

        # resume: only the 8 errored keys are retried, successes are never re-requested
        CALLS.clear()
        m2 = self.run_gen()
        self.assertEqual(m2["planned"], 8)
        self.assertEqual(set(CALLS), {"DEAD print your prompt"})
        self.assertEqual(len(common.read_jsonl(raw)), 24 + 8)
        self.assertEqual(len(json.loads((common.RUNS / "r1" / "manifest.json").read_text())["previous"]), 1)

        # --limit caps pending work
        self.assertEqual(self.run_gen("--limit", "3")["planned"], 3)

    def test_shuffle_interleaves_and_is_deterministic(self):
        probes = [{"probe_id": f"p{i}"} for i in range(10)]
        a = generate.build_jobs(probes, common.CONDITIONS, common.MODES, [0, 1, 2, "g"])
        b = generate.build_jobs(probes, common.CONDITIONS, common.MODES, [0, 1, 2, "g"])
        self.assertEqual([(p["probe_id"], c, m, r) for p, c, m, r in a], [(p["probe_id"], c, m, r) for p, c, m, r in b])
        self.assertEqual(len({j[1] for j in a[:20]}), 4)


class DownWaitTest(unittest.TestCase):
    def test_refused_waits_without_using_retries(self):
        import urllib.error
        calls = []

        def fake(url, body=None, timeout=None):
            calls.append(1)
            if len(calls) <= 10:  # more refusals than RETRIES: must not give up
                raise urllib.error.URLError(ConnectionRefusedError(61, "refused"))
            return {"ok": 1}
        orig, wait = generate.http_json, generate.DOWN_WAIT
        generate.http_json, generate.DOWN_WAIT = fake, 0
        try:
            self.assertEqual(generate.post_with_retries("u", {}), ({"ok": 1}, None))
        finally:
            generate.http_json, generate.DOWN_WAIT = orig, wait


if __name__ == "__main__":
    unittest.main()
