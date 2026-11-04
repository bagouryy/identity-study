"""Write 4 paraphrases per intent: one each by Sonnet, Haiku, Qwen3.6 (no system prompt) and a rule-based rewrite.

python3 src/author.py plan                 # assign authors + style cells -> probes/raw/slots.json
python3 src/author.py run sonnet|haiku|qwen|rule
python3 src/author.py assemble             # -> probes/test.yaml, probes/dev.yaml
Every prompt and raw output is saved under probes/raw/ so the probe set can be audited.
"""
import json
import random
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import yaml

from common import ENDPOINT, MODEL, PROBES, load_yaml

RAW = PROBES / "raw"
AUTHORS = ["sonnet", "haiku", "qwen", "rule"]
CELLS = [(r, l, f) for r in ("formal", "casual") for l in ("short", "long") for f in ("fluent", "non-native")]
STYLE_TEXT = {
    "formal": "formal register: polite, complete sentences",
    "casual": "casual register: relaxed chat tone",
    "short": "short: a single sentence",
    "long": "long: 2 to 4 sentences; you may add neutral context about why the user asks, but no extra requests",
    "fluent": "fluent: no errors",
    "non-native": "non-native English: a few natural grammar slips and one or two typos, still clearly understandable",
}
RULES = """Rules:
- Keep exactly the same request and intent. Do not answer it.
- Do not name any company, product, AI model or person unless the original message names it.
- Do not mention any assistant name.
- Do not presuppose an answer to the question. If the original contains a premise (even a false one), keep it.
- Any added context must be impersonal: never describe the user's own health, symptoms, contract, money or a specific person.
- Output only the rewritten user message, nothing else."""


def intents():
    d = load_yaml(PROBES / "intents.yaml")
    return d["intents"], d["labs"]


def plan():
    rng = random.Random(20261103)
    its, labs = intents()
    cells = []
    slots = []
    for it in its:
        authors = AUTHORS[:]
        rng.shuffle(authors)
        if len(cells) < 4:  # refill a shuffled deck so cells stay balanced across intents
            deck = CELLS[:]
            rng.shuffle(deck)
            cells += deck
        for k in range(4):
            cell, cells = cells[0], cells[1:]
            text = it["text"].replace("{LAB}", labs[k]) if it.get("subtype") == "lab_rotation" else it["text"]
            slots.append({"probe_id": f"{it['id']}-{k}", "intent_id": it["id"], "slot": k, "author": authors[k],
                          "style": dict(zip(("register", "length", "fluency"), cell)), "source": text})
    (RAW / "slots.json").write_text(json.dumps(slots, indent=1))
    print(len(slots), "slots")


def style_line(s):
    return "; ".join(STYLE_TEXT[s[k]] for k in ("register", "length", "fluency"))


def one_prompt(s):
    return f"Rewrite this user message to an AI assistant.\nStyle: {style_line(s['style'])}.\n{RULES}\n\nOriginal message:\n{s['source']}"


def run_claude(model, slots):
    items = "\n\n".join(f"[{s['probe_id']}]\nStyle: {style_line(s['style'])}.\nOriginal message: {s['source']}" for s in slots)
    prompt = ("You are helping build a research probe set. For each item below, rewrite the user message "
              f"in the requested style.\n{RULES}\n\nReturn ONLY a JSON object mapping each item id to its "
              f"rewritten message.\n\n{items}")
    (RAW / f"prompt_{model}.txt").write_text(prompt)
    out = subprocess.run(["claude", "-p", "--model", model, "--output-format", "json", "--tools", "",
                          "--system-prompt", "You rewrite text exactly as instructed and output strict JSON."],
                         input=prompt, capture_output=True, text=True, check=True).stdout
    (RAW / f"envelope_{model}.json").write_text(out)
    res = json.loads(out)["result"]
    return json.loads(res[res.index("{"):res.rindex("}") + 1])


def run_qwen(slots):
    def call(s):
        body = {"model": MODEL, "messages": [{"role": "user", "content": one_prompt(s)}], "max_tokens": 300,
                "temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5, "seed": 7,
                "chat_template_kwargs": {"enable_thinking": False}}
        r = urllib.request.Request(f"{ENDPOINT}/chat/completions", json.dumps(body).encode(),
                                   {"Content-Type": "application/json"})
        return s["probe_id"], json.load(urllib.request.urlopen(r, timeout=300))["choices"][0]["message"]["content"].strip()
    with ThreadPoolExecutor(16) as ex:
        return dict(ex.map(call, slots))


CONTEXT = ["I'm putting together some notes for my team.", "I have a quick question before I start work.",
           "I've been wondering about this for a while.", "Something came up in a conversation today."]
# only contractions that are grammatical in any position ("who you are" must not become "who you're")
CONTRACT = [("do not", "don't"), ("What is", "What's"), ("is not", "isn't"), ("cannot", "can't")]


def rule_rewrite(s, rng):
    t, st = s["source"], s["style"]
    if st["length"] == "long":
        t = rng.choice(CONTEXT) + " " + t
    if st["register"] == "casual":
        for a, b in CONTRACT:
            t = t.replace(a, b)
        t = (t[0].lower() + t[1:]).rstrip(".")
    else:
        t = "Hello. " + t
    if st["fluency"] == "non-native":
        t = re.sub(r"\b(the|a) ", "", t, count=1)
        words = [w for w in re.findall(r"[A-Za-z]{6,}", t)]
        if words:
            w = rng.choice(words)
            i = rng.randrange(1, len(w) - 2)
            t = t.replace(w, w[:i] + w[i + 1] + w[i] + w[i + 2:], 1)  # adjacent-letter swap typo
    return t


def run(author):
    slots = [s for s in json.loads((RAW / "slots.json").read_text()) if s["author"] == author]
    if author == "rule":
        rng = random.Random(4)
        out = {s["probe_id"]: rule_rewrite(s, rng) for s in slots}
    elif author == "qwen":
        out = run_qwen(slots)
    else:
        out = run_claude(author, slots)
    missing = {s["probe_id"] for s in slots} - set(out)
    assert not missing, f"{author} missing {sorted(missing)}"
    (RAW / f"{author}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print(author, len(out))


def assemble():
    its = {i["id"]: i for i in intents()[0]}
    texts = {}
    for a in AUTHORS:
        texts.update(json.loads((RAW / f"{a}.json").read_text()))
    split = {"test": [], "dev": []}
    for s in json.loads((RAW / "slots.json").read_text()):
        it = its[s["intent_id"]]
        split[it["split"]].append({"probe_id": s["probe_id"], "intent_id": s["intent_id"], "split": it["split"],
                                   "category": it["category"], "subtype": it.get("subtype", "none"),
                                   "topic": it.get("topic"), "author": s["author"], "style": s["style"],
                                   "text": texts[s["probe_id"]].strip().strip('"'), "allowed": it.get("allowed")})
    for k, v in split.items():
        (PROBES / f"{k}.yaml").write_text(yaml.safe_dump(v, sort_keys=False, allow_unicode=True, width=200))
        print(k, len(v))


def fix(path):
    """Rewrite listed probes with their own author model. File lines: '<probe_id>\t<reason>'."""
    slots = {s["probe_id"]: s for s in json.loads((RAW / "slots.json").read_text())}
    texts = {}
    for a in AUTHORS:
        texts.update(json.loads((RAW / f"{a}.json").read_text()))
    todo = [l.split("\t") for l in open(path).read().splitlines() if l.strip()]
    for author in ("sonnet", "haiku", "qwen"):
        batch = []
        for pid, reason in todo:
            s = dict(slots[pid])
            if s["author"] != author:
                continue
            others = [texts[q] for q in slots if slots[q]["intent_id"] == s["intent_id"] and q != pid]
            s["source"] = (s["source"] + "\n(Fix needed: " + reason + " Use clearly different wording from these "
                           "other versions: " + " | ".join(others) + ")")
            batch.append(s)
        if not batch:
            continue
        out = run_qwen(batch) if author == "qwen" else run_claude(author, batch)
        d = json.loads((RAW / f"{author}.json").read_text())
        d.update({k: out[k] for k in (s["probe_id"] for s in batch)})
        (RAW / f"{author}.json").write_text(json.dumps(d, indent=1, ensure_ascii=False))
        print(author, "fixed", len(batch))


if __name__ == "__main__":
    cmd = sys.argv[1]
    {"plan": plan, "assemble": assemble, "fix": lambda: fix(sys.argv[2])}.get(cmd, lambda: run(sys.argv[2]))()
