#!/usr/bin/env bash
# PLAN §11 path A maintenance window, run from the Mac at the repo root:  internals/window.sh [--dry-run]
# Stops the serving vLLM, reboots the host, runs run_internals.py in the vLLM image, restores serving.
# Any failure after vLLM is stopped triggers restore(): kill internals container, reboot if it ran, up -d, health check.
# A failed final health check therefore retries restore once (second reboot) before giving up.
set -euo pipefail
cd "$(dirname "$0")/.."

# Host-specific values live in .host.env (git-ignored; see .host.env.example)
[[ -z "${GPU_HOST:-}" && -f .host.env ]] && source .host.env  # values already in the environment win (tests)
HOST=${GPU_HOST:?set GPU_HOST in .host.env}
RD='~/identity-study'
VLLM_CONTAINER=${GPU_VLLM_CONTAINER:?set GPU_VLLM_CONTAINER}
MODEL=Qwen/Qwen3.6-35B-A3B
IMAGE=vllm/vllm-openai:cu130-nightly
CNAME=identity-internals
STAGES=${STAGES:-internals}  # "models internals" for the follow-up window
INTERNALS=${INTERNALS:-run_internals.py}  # run_v2.py for the follow-up
INT_OUT=${INT_OUT:-out}
# follow-up: other models, all served at once (name, HF id, port); ~110 GB free with Qwen down, 0.25 each
MODELS=("olmo allenai/OLMo-2-1124-7B-Instruct 8101 4096" "phi microsoft/Phi-4-mini-instruct 8102" "gptoss openai/gpt-oss-20b 8103")
MODELS_ONLY=${MODELS_ONLY:-}  # e.g. "olmo gptoss" to rerun a subset
GEN_ARGS="--split test --modes std --conditions C0 C1 C3 E N1 N3 N10 --runs 0 1 2 --categories identity --concurrency 32"
DRY=0; [[ "${1:-}" == --dry-run ]] && DRY=1
LOCAL_OUT=${LOCAL_OUT:-internals/out}  # tests point this elsewhere
mkdir -p "$LOCAL_OUT"
LOG="$LOCAL_OUT/window_$(date +%Y%m%d_%H%M%S).log"
STOPPED=0 RAN=0 RESTORED=0
POLL=${POLL:-10}  # seconds between polls (tests set 0)

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
run() { log "+ $*"; (( DRY )) || "$@" 2>&1 | tee -a "$LOG"; }
SSHO="-o ConnectTimeout=10 -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=6"  # a dropped link must not hang a step
r() { run ssh $SSHO "$HOST" "$@"; }
q() { ssh $SSHO "$HOST" "$@"; }  # quiet remote query, never in dry-run

boot_id() { q cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo none; }

reboot_host() {
  log "reboot"
  (( DRY )) && { log "+ ssh $HOST 'sudo -n reboot'; wait for a new boot_id (15 min)"; return; }
  local old; old=$(boot_id)
  q 'sudo -n reboot' || true  # ssh drops when the box goes down
  for _ in $(seq 90); do
    sleep "$POLL"
    local now; now=$(boot_id)
    [[ "$now" != none && "$now" != "$old" ]] && { log "ssh back, boot_id $now"; return; }
  done
  log "ERROR: Host did not come back within 15 min"; return 1
}

internals() {  # internals <timeout> <args...>
  local t=$1; shift
  r "timeout $t docker run --rm --name $CNAME --gpus all --ipc=host -e HF_HUB_OFFLINE=1 \
-v ~/.cache/huggingface:/root/.cache/huggingface -v $RD:/work --entrypoint python3 $IMAGE \
/work/internals/$INTERNALS --out /work/internals/$INT_OUT $*"
}

health() {
  log "health check: waiting for /v1/models to list $MODEL (15 min)"
  (( DRY )) && { log "+ ssh $HOST 'curl -sf localhost:8001/v1/models' (poll), one chat request, docker ps"; return; }
  local ok=0
  for _ in $(seq 90); do [[ "$(q "curl -sf -m 5 localhost:8001/v1/models" 2>/dev/null || true)" == *"$MODEL"* ]] && { ok=1; break; }; sleep "$POLL"; done
  (( ok )) || { log "FAIL: model not listed after 15 min"; return 1; }
  r "curl -sf -m 120 localhost:8001/v1/chat/completions -H 'Content-Type: application/json' \
-d '{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with OK.\"}],\"max_tokens\":16,\"chat_template_kwargs\":{\"enable_thinking\":false}}'" \
    || { log "FAIL: synthetic chat request"; return 1; }
  # other services on the host are not ours to fail on: restart what the reboot left down, then only report
  if [[ -n "${BEFORE:-}" ]]; then
    local down; down=$(comm -23 <(tr ' ' '\n' <<< "$BEFORE" | sort) <(q "docker ps --format '{{.Names}}'" | sort))
    [[ -n "$down" ]] && { log "restarting containers that ran before the window: $down"; r "docker start $(echo $down)" || true; sleep $((POLL * 9)); }
  fi
  r "docker ps --format '{{.Names}}\t{{.Status}}'"
  [[ "$(q "docker ps --format '{{.Status}}'")" == *unhealthy* ]] && log "WARNING: a container reports unhealthy (vLLM itself is OK)"
  log "health OK"
}

models() {  # serve the other models side by side, generate the identity sweep against each, remove them
  local ready=() m n id port
  for m in "${MODELS[@]}"; do
    read -r n id port mlen <<< "$m"
    [[ -z "$MODELS_ONLY" || " $MODELS_ONLY " == *" $n "* ]] || continue
    r "docker run -d --name identity-$n --gpus all --ipc=host -p 127.0.0.1:$port:8000 -v $RD/hf:/hf \
-v ~/.cache/huggingface/hub/models--openai--gpt-oss-20b:/hf/hub/models--openai--gpt-oss-20b:ro \
-v $RD/tiktoken:/tiktoken:ro -e TIKTOKEN_ENCODINGS_BASE=/tiktoken -e HF_HOME=/hf -e HF_HUB_OFFLINE=1 $IMAGE $id --gpu-memory-utilization 0.25 --max-model-len ${mlen:-8192}" || { log "start $n failed, skipping"; continue; }
    (( DRY )) && { ready+=("$m"); continue; }
    local ok=0  # one at a time: vLLM sizes its pool from free memory at start
    for _ in $(seq 90); do [[ "$(q "curl -sf -m 5 localhost:$port/v1/models" 2>/dev/null || true)" == *"$id"* ]] && { ok=1; break; }; sleep "$POLL"; done
    if (( ok )); then ready+=("$m"); log "$n ready on :$port"
    else log "$n not ready after 15 min, skipping"; q "docker logs --tail 40 identity-$n" >> "$LOG" 2>&1 || true; q "docker rm -f identity-$n" >/dev/null 2>&1 || true; fi
  done
  (( ${#ready[@]} )) || { log "no other model came up"; return 0; }
  local cmd="cd $RD;"
  for m in "${ready[@]}"; do
    read -r n id port _ <<< "$m"
    cmd+=" (for i in 1 2; do python3 src/generate.py --run-id x_$n --model $id --endpoint http://localhost:$port/v1 $GEN_ARGS >> runs/x_$n.log 2>&1; done; echo DONE >> runs/x_$n.log) &"
  done
  # detached on the host so an ssh drop cannot kill generation; poll for every DONE marker (3 h cap)
  r "setsid nohup bash -c '$cmd wait' >/dev/null 2>&1 < /dev/null &"
  if (( !DRY )); then
    local logs=""; for m in "${ready[@]}"; do read -r n _ <<< "$m"; logs+=" $RD/runs/x_$n.log"; done
    for _ in $(seq $((3 * 360))); do
      local nd; nd=$(q "cat $logs 2>/dev/null | grep -c DONE" 2>/dev/null); [[ "$nd" =~ ^[0-9]+$ ]] && (( nd >= ${#ready[@]} )) && break
      sleep "$POLL"
    done
    log "model generation finished or timed out: $(q "tail -qn1 $logs" 2>/dev/null | tr '\n' ' ')"
  fi
  r "docker rm -f $(for m in "${MODELS[@]}"; do read -r n _ <<< "$m"; printf 'identity-%s ' "$n"; done)" || true
}

restore() {
  (( RAN )) && reboot_host  # internals may leave memory pinned; reboot first so vLLM gets the full pool
  r "docker start $VLLM_CONTAINER"  # reuses the container's own config; compose would need the unreadable .env
  health
  RESTORED=1
}

on_exit() {
  local rc=$?
  trap - EXIT
  if (( STOPPED && !RESTORED )); then
    log "!! exit $rc after vLLM was stopped: restoring serving"
    (( DRY )) || q "docker rm -f $CNAME identity-olmo identity-phi identity-gptoss" >/dev/null 2>&1 || true
    restore || log "!! RESTORE FAILED. By hand: ssh $HOST \"docker start $VLLM_CONTAINER\" (reboot first if memory looks full)"
  fi
  (( rc == 0 && !DRY )) && log "SUCCESS: window finished, results in $LOCAL_OUT/" || true
  (( rc != 0 )) && log "FAILURE: exit $rc, see $LOG"
  exit $rc
}
trap on_exit EXIT
trap 'exit 130' INT TERM

log "== 0. preflight"
if (( !DRY )); then
  for _ in 1 2 3 4 5 6; do q true 2>/dev/null && break; log "ssh not answering, retrying"; sleep "$POLL"; done
  q true 2>/dev/null || { log "Host unreachable over ssh. Nothing was changed."; exit 1; }
  q 'sudo -n -l reboot' >/dev/null 2>&1 || {  # the sudoers rule allows reboot only, so do not test with 'true'
    log "No passwordless sudo on the host, so this script cannot reboot it. Nothing was changed."
    log "Either add a sudoers rule (<user> ALL=(root) NOPASSWD: /sbin/reboot, /usr/sbin/reboot) or run the window by hand:"
    log "  stop+rm vllm, sudo reboot, then internals/window.sh steps from 'gate' on."
    exit 1; }
fi

log "== 1. sync code to the host"
r "mkdir -p $RD/src $RD/probes $RD/internals/out"
run rsync -az --exclude .venv --exclude out --exclude __pycache__ internals/ "$HOST:$RD/internals/"
run rsync -az --exclude __pycache__ src/ "$HOST:$RD/src/"
run rsync -az prompts/ "$HOST:$RD/prompts/"
run rsync -az probes/test.yaml "$HOST:$RD/probes/"

BEFORE=$( (( DRY )) || q "docker ps --format '{{.Names}}'" | tr '\n' ' ')  # restored after the window
log "== 2. stop and remove serving vLLM (restart policy unless-stopped: a removed container stays gone on boot)"
STOPPED=1
r "docker stop -t 60 $VLLM_CONTAINER"  # stopped by hand, so restart policy unless-stopped keeps it down across the reboot
(( DRY )) || [[ -z "$(q "docker ps -q --filter name=^$VLLM_CONTAINER\$")" ]] || { log "vllm container still running"; exit 1; }

log "== 3. reboot to clear leaked memory"
reboot_host

gate() {
  log "== gate: available memory >= 90 GB, swap ~0"
  r "free -g"
  (( DRY )) && return
  read -r avail swap < <(q "free -g | awk '/^Mem:/{a=\$7} /^Swap:/{s=\$3} END{print a, s}'")
  (( avail >= 90 && swap <= 1 )) || { log "gate FAILED: available ${avail} GB, swap used ${swap} GB"; exit 1; }
  log "gate OK: available ${avail} GB, swap ${swap} GB"
}
log "== 4. memory gate"
gate

if [[ $STAGES == *models* ]]; then
  log "== 4m. other models, served together"
  RAN=1  # their containers may also leave memory pinned
  models
  log "== 4m. reboot and re-gate before internals"
  reboot_host
  gate
fi
[[ $STAGES == *internals* ]] || { log "== no internals stage requested"; restore; exit 0; }

log "== 5. hook smoke test (vllm, 20 min), fallback hf"
RAN=1
BACKEND=vllm
if ! internals 20m --backend vllm --stage capture --limit 2; then
  log "vllm hook smoke test failed; trying --backend hf"
  BACKEND=hf
  (( DRY )) || q "docker rm -f $CNAME" >/dev/null 2>&1 || true
  internals 20m --backend hf --stage capture --limit 2
fi

log "== 5b. reboot again: an exited internals container leaves GPU memory pinned (12 GiB free seen)"
reboot_host
gate
log "== 6. full run, backend $BACKEND (3 h timeout)"
internals 3h --backend "$BACKEND" --stage all

log "== 7. fetch results"
run rsync -az "$HOST:$RD/internals/$INT_OUT/" "$LOCAL_OUT/"
if [[ -x internals/.venv/bin/python ]]; then
  run internals/.venv/bin/python internals/run_internals.py --stage score --out "$LOCAL_OUT" || log "local score failed (serving restore continues)"
fi

log "== 8. reboot, restart vLLM, health check"
restore
log "REMINDER: remove the temporary reboot rule now: ssh -t $HOST 'sudo rm /etc/sudoers.d/identity-reboot'"
