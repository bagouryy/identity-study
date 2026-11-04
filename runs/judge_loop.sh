#!/bin/bash
# Follow-up runs: pull from the host, rules, judge (both judges) until every listed run is DONE and fully judged.
# usage: runs/sweep_loop.sh run_id[:all] ...   (":all" = judge every identity answer, for non-Qwen models)
cd "$(dirname "$0")/.."
source .host.env
log() { echo "$(date -u +%H:%M) $*" | tee -a runs/sweep_loop.log; }
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10"
while true; do
  left=0
  for spec in "$@"; do
    rid=${spec%%:*}; flag=; [[ $spec == *:all ]] && flag=--judge-all-identity
    rsync -az -e "$SSH" "$GPU_HOST:identity-study/runs/$rid/" "runs/$rid/" 2>/dev/null
    rsync -az -e "$SSH" "$GPU_HOST:identity-study/runs/$rid.log" "runs/$rid.log" 2>/dev/null
    [[ -f runs/$rid/raw.jsonl ]] || { left=1; continue; }
    python3 src/rules.py --run-id "$rid" $flag >/dev/null || { left=1; continue; }
    out=$( { python3 src/judge.py --run-id "$rid" --judge haiku --concurrency 6 & python3 src/judge.py --run-id "$rid" --judge sonnet --concurrency 6; wait; } 2>&1 | tee -a runs/sweep_loop.judge.log | grep "items to judge")
    log "$rid: $(echo $out | tr '\n' ' ')"
    grep -q DONE "runs/$rid.log" 2>/dev/null && ! grep -v ": 0 items" <<<"$out" | grep -q . || left=1
  done
  (( left )) || { log "all runs DONE and judged"; break; }
  sleep 240
done
