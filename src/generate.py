"""Generation client: probe x condition x mode x run_idx -> runs/<run_id>/raw.jsonl (idempotent resume).

python3 src/generate.py --split dev --run-id dev1 [--conditions C0 C1 C2 C3] [--modes think nothink]
                        [--runs 0 1 2 g] [--concurrency 32] [--limit N] [--endpoint URL]
"""
import argparse
import json
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

TIMEOUT = 900
RETRIES = 3
BACKOFF = 5.0  # seconds, doubled per retry; tests set 0
DOWN_WAIT = 30.0  # seconds between checks while the endpoint refuses connections
SHUFFLE_SEED = 20261103
PROGRESS_EVERY = 50


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_idx_arg(s):
    return s if s == common.GREEDY else int(s)


def http_json(url, body=None, timeout=TIMEOUT):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def post_with_retries(url, body):
    """Returns (response_json, error_str). Retries network errors and 5xx, not 4xx."""
    err = None
    attempt = 0
    while attempt <= RETRIES:
        try:
            return http_json(url, body), None
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}: {e.read()[:500].decode(errors='replace')}"
            if e.code < 500:
                break
        except urllib.error.URLError as e:
            if isinstance(e.reason, ConnectionRefusedError):
                # endpoint/tunnel down (not the model failing): wait without using up a retry
                time.sleep(DOWN_WAIT)
                continue
            err = f"{type(e).__name__}: {e}"
        except (TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as e:
            err = f"{type(e).__name__}: {e}"
        if attempt < RETRIES:
            time.sleep(BACKOFF * 2**attempt)
        attempt += 1
    return None, err


def build_jobs(probes, conditions, modes, runs):
    jobs = [(p, c, m, r) for p in probes for c in conditions for m in modes for r in runs]
    random.Random(SHUFFLE_SEED).shuffle(jobs)  # interleave conditions over time so load drift hits all equally
    return jobs


def run_one(job, run_id, url, model=common.MODEL):
    p, c, m, r = job
    body = common.request_body(c, m, p["probe_id"], p["text"], r, model)
    t0 = time.time()
    resp, err = post_with_retries(url, body)
    latency = time.time() - t0
    msg, choice = {}, {}
    if resp is not None:
        try:
            choice = resp["choices"][0]
            msg = choice.get("message") or {}
        except (KeyError, IndexError, TypeError):
            err = f"bad response: {json.dumps(resp)[:500]}"
    return {
        "key": common.key(run_id, p["probe_id"], c, m, r), "run_id": run_id,
        "probe_id": p["probe_id"], "intent_id": p.get("intent_id"), "category": p.get("category"),
        "subtype": p.get("subtype"), "author": p.get("author"), "probe_text": p["text"],
        "model": model, "condition": c, "mode": m, "run_idx": r, "seed": body["seed"],
        "content": msg.get("content"),
        "reasoning": msg.get("reasoning_content") or msg.get("reasoning"),
        "finish_reason": choice.get("finish_reason"), "usage": (resp or {}).get("usage"),
        "latency_s": round(latency, 3), "ts": now(), "error": err,
    }


def git(*args):
    try:
        out = subprocess.run(["git", *args], cwd=common.ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", required=True, choices=["dev", "test"])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--conditions", nargs="+", default=list(common.CONDITIONS),
                    choices=common.CONDITIONS + common.SWEEP_CONDITIONS)
    ap.add_argument("--modes", nargs="+", default=list(common.MODES), choices=common.MODES + (common.OTHER_MODE,))
    ap.add_argument("--categories", nargs="+", help="only probes in these categories")
    ap.add_argument("--model", default=common.MODEL)
    ap.add_argument("--runs", nargs="+", type=run_idx_arg,
                    default=[*range(common.N_SAMPLED_RUNS), common.GREEDY])
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--endpoint", default=common.ENDPOINT)
    a = ap.parse_args(argv)
    endpoint = a.endpoint.rstrip("/")

    out_dir = common.RUNS / a.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "raw.jsonl"
    done_keys = {r["key"] for r in common.read_jsonl(raw) if not r.get("error")}
    probes = [p for p in common.load_probes(a.split) if not a.categories or p["category"] in a.categories]
    jobs = [j for j in build_jobs(probes, a.conditions, a.modes, a.runs)
            if common.key(a.run_id, j[0]["probe_id"], j[1], j[2], j[3]) not in done_keys]
    if a.limit is not None:
        jobs = jobs[:a.limit]

    try:
        models = http_json(endpoint + "/models", timeout=30)
    except Exception as e:  # manifest records the failure; generation errors surface per line
        models = {"error": f"{type(e).__name__}: {e}"}
    manifest = {"args": vars(a), "sampling": common.SAMPLING, "model": a.model,
                "snapshot": common.SNAPSHOT, "endpoint": endpoint, "models_response": models,
                "git_head": git("rev-parse", "HEAD"),
                "git_tags": (git("tag", "--points-at", "HEAD") or "").split() or [],
                "already_done": len(done_keys), "planned": len(jobs),
                "start": now(), "end": None, "counts": None}
    mpath = out_dir / "manifest.json"
    if mpath.exists():  # resume: keep earlier invocations' provenance
        old = json.loads(mpath.read_text())
        manifest["previous"] = old.pop("previous", []) + [old]
    mpath.write_text(json.dumps(manifest, indent=2))

    lock = threading.Lock()
    t_start, n, errors, trunc, toks, stream_rates = time.time(), 0, 0, 0, 0, []
    url = endpoint + "/chat/completions"
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for fut in as_completed([ex.submit(run_one, j, a.run_id, url, a.model) for j in jobs]):
            row = fut.result()
            ct = (row["usage"] or {}).get("completion_tokens") or 0
            with lock:
                common.append_jsonl(raw, row)
                n += 1
                errors += bool(row["error"])
                trunc += row["finish_reason"] == "length"
                toks += ct
                if ct and row["latency_s"]:
                    stream_rates.append(ct / row["latency_s"])
                if n % PROGRESS_EVERY == 0 or n == len(jobs):
                    el = time.time() - t_start
                    eta = el / n * (len(jobs) - n)
                    med = statistics.median(stream_rates) if stream_rates else 0.0
                    print(f"{n}/{len(jobs)} agg {toks / el:.0f} tok/s, median stream {med:.1f} tok/s, "
                          f"errors {errors}, truncated {trunc}, ETA {eta / 60:.1f} min", flush=True)

    manifest.update(end=now(), counts={"done": n - errors, "errors": errors, "truncated": trunc,
                                       "completion_tokens": toks})
    mpath.write_text(json.dumps(manifest, indent=2))
    return manifest


if __name__ == "__main__":
    main()
