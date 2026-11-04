"""Shared contract for every stage: paths, probes, conditions, sampling, seeds, keys.

Data formats (one JSON object per line):
  probes/{test,dev}.yaml   list of probe dicts, see load_probes()
  runs/<run_id>/raw.jsonl  one generation per line, see generate.py (fields: key, run_id, probe_id,
                           intent_id, category, subtype, author, condition, mode, run_idx, seed,
                           content, reasoning, finish_reason, usage, latency_s, ts, error)
  runs/<run_id>/rules.jsonl, judge/labels_<judge>.jsonl, keyed by `key`.
"""
import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PROBES, PROMPTS, RUNS, JUDGE, RESULTS = (ROOT / d for d in ("probes", "prompts", "runs", "judge", "results"))

MODEL = "Qwen/Qwen3.6-35B-A3B"
SNAPSHOT = "995ad96eacd98c81ed38be0c5b274b04031597b0"
ENDPOINT = "http://localhost:8001/v1"  # via: ssh -N -L 8001:localhost:8001 <gpu-host>

CONDITIONS = ("C0", "C1", "C2", "C3")
# Exploratory follow-up (not pre-registered): neutral-prompt length sweep. E = empty system block,
# Nk = C3's header plus its first k rules. Prompt files are in prompts/.
SWEEP_CONDITIONS = ("E", "N1", "N3", "N10")
MODES = ("think", "nothink")
OTHER_MODE = "std"  # single mode for the other models (no thinking switch)
N_SAMPLED_RUNS = 3
GREEDY = "g"  # run_idx value for the greedy run

# Model-card settings (PREFLIGHT §3); every field is sent explicitly so server defaults never apply.
SAMPLING = {
    "think": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
              "presence_penalty": 1.5, "repetition_penalty": 1.0, "max_tokens": 4096},
    "nothink": {"temperature": 0.7, "top_p": 0.80, "top_k": 20, "min_p": 0.0,
                "presence_penalty": 1.5, "repetition_penalty": 1.0, "max_tokens": 1024},
    # other models: one generic setting for all of them, no model-specific penalties
    "std": {"temperature": 0.7, "top_p": 0.9, "max_tokens": 2048},
}

LABS = {  # entity regexes for identity rules; order matters only for reporting
    "qwen_alibaba": r"\b(qwen|tongyi|alibaba)\b",
    "persona": r"\b(mira|quillmere)\b",
    "openai": r"\b(openai|chatgpt|gpt-?\d)",
    "google": r"\b(google|gemini|deepmind|bard|gemma)\b",
    "anthropic": r"\b(anthropic|claude)\b",
    "meta": r"\b(meta ai|llama)\b",
    "other_lab": r"\b(mistral|deepseek|microsoft|copilot|xai|grok|cohere)\b",
}


def load_yaml(path):
    return yaml.safe_load(Path(path).read_text())


def load_probes(split):
    """List of dicts: probe_id, intent_id, split, category, subtype, topic, author, style, text, allowed."""
    return load_yaml(PROBES / f"{split}.yaml")


def system_prompt(condition):
    return None if condition == "C0" else (PROMPTS / f"{condition}.txt").read_text().strip()


def messages(condition, probe_text):
    # C0 omits the system message entirely: system:"" renders an empty system block (PREFLIGHT §2).
    sp = system_prompt(condition)
    msgs = [] if sp is None else [{"role": "system", "content": sp}]
    return msgs + [{"role": "user", "content": probe_text}]


def seed(probe_id, mode, run_idx):
    # Same seed across conditions (common random numbers), different across probes/modes/runs.
    h = hashlib.sha256(f"{probe_id}|{mode}|{run_idx}".encode()).hexdigest()
    return int(h, 16) % (2**31)


def key(run_id, probe_id, condition, mode, run_idx):
    return f"{run_id}|{probe_id}|{condition}|{mode}|{run_idx}"


def request_body(condition, mode, probe_id, probe_text, run_idx, model=MODEL):
    p = dict(SAMPLING[mode])
    if run_idx == GREEDY:
        p["temperature"] = 0.0
    body = {"model": model, "messages": messages(condition, probe_text), **p,
            "seed": seed(probe_id, mode, run_idx)}
    if mode == "nothink":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    if "gpt-oss" in model:
        body["reasoning_effort"] = "low"  # keeps reasoning inside the token budget
    return body


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
