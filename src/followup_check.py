"""Blind 20-item human check for the follow-up runs (Qwen length sweep + other models), identity questions only.

  python3 src/followup_check.py build   -> human_check/followup/{items.json,index.html}
  (label, Download CSV, save as human_check/followup/labels.csv)
  python3 src/followup_check.py score   -> agreement of the human with each judge on own-origin and persona

The page reuses the main check's layout; "true origin" means the developer named on each item (the model behind it).
"""
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze as A  # noqa: E402
import common as C  # noqa: E402
import followup as F  # noqa: E402
import human_check as H  # noqa: E402
import judge as J  # noqa: E402

SEED = 20261104
D = C.ROOT / "human_check" / "followup"
RUNS = {"sweep_qwen": ("Qwen3.6 (Alibaba)", C.LABS["qwen_alibaba"]),
        **{f"x_{n}": (f"{lab} ({'Ai2' if n == 'olmo' else 'Microsoft' if n == 'phi' else 'OpenAI'})", rx)
           for n, (lab, rx) in F.MODELS.items()}}
PER_RUN = 5  # 4 runs x 5 = 20; 1 of 5 from judge disagreement when there is one


def sample():
    rng, out = random.Random(SEED), []
    for rid, (model, rx) in RUNS.items():
        raw = C.RUNS / rid / "raw.jsonl"
        if not raw.exists():
            continue
        G = {r["key"]: r for r in C.read_jsonl(raw)}
        labs = {j: {r["key"]: r for r in C.read_jsonl(C.JUDGE / f"labels_{j}_{rid}.jsonl")} for j in A.JUDGES}
        both = sorted(k for k in G if all(k in labs[j] for j in A.JUDGES) and G[k].get("category") == "identity")
        if not both:
            continue
        dis = [k for k in both if len({F.own_label(labs[j][k], rx) for j in A.JUDGES}) > 1]
        pick = rng.sample(dis, 1) if dis else []
        rest = [k for k in both if k not in pick]
        pick += rng.sample(rest, min(PER_RUN - len(pick), len(rest)))
        out += [{"key": k, "run": rid, "model": model, "item_id": labs[A.JUDGES[0]][k]["item_id"],
                 "from_disagreement": k in dis, "user": G[k]["probe_text"], "response": G[k]["content"] or "(empty)"}
                for k in pick]
    rng.shuffle(out)
    return out


def build():
    items = sample()
    D.mkdir(parents=True, exist_ok=True)
    (D / "sample.json").write_text(json.dumps(items, ensure_ascii=False, indent=1))  # keys: for scoring, not shown
    shown = [{"item_id": s["item_id"], "group": "identity", "fields": ["persona", "true_origin", "other_lab"],
              "user": f"[model under test: {s['model']}]\n\n{s['user']}", "response": s["response"]} for s in items]
    (D / "items.json").write_text(json.dumps(shown, ensure_ascii=False, indent=1))
    html = (H.HTML.replace("__ITEMS__", json.dumps(shown, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__COLS__", json.dumps(H.CSV_COLS))
            .replace("KEY = 'human_check_v1'", "KEY = 'human_check_followup_v1'")
            .replace("Claims Qwen / Tongyi / Alibaba as its model or developer",
                     "Claims its real developer or model (named at the top of the user message)"))
    assert "human_check_followup_v1" in html and "named at the top" in html, "template changed; update the replacements"
    (D / "index.html").write_text(html)
    print(f"{len(shown)} items ({sum(s['from_disagreement'] for s in items)} from judge disagreement) -> {D / 'index.html'}")


def score():
    S = {s["item_id"]: s for s in json.loads((D / "sample.json").read_text())}
    rows = H.convert(D / "labels.csv")
    for j in A.JUDGES:
        agree = {"true_origin": [0, 0], "persona": [0, 0]}
        for r in rows:
            s = S.get(r["item_id"])
            if not s:
                continue
            lab = {x["key"]: x for x in C.read_jsonl(C.JUDGE / f"labels_{j}_{s['run']}.jsonl")}[s["key"]]
            own, per = F.own_label(lab, RUNS[s["run"]][1])
            for f, v in (("true_origin", own), ("persona", per)):
                if r[f] is not None:
                    agree[f][0] += int(r[f] == v)
                    agree[f][1] += 1
        print(j, {f: f"{a}/{n}" for f, (a, n) in agree.items()})


if __name__ == "__main__":
    {"build": build, "score": score}[sys.argv[1]]()
