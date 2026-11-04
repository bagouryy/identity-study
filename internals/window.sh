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
DRY=0; [[ "${1:-}" == --dry-run ]] && DRY=1
LOCAL_OUT=${LOCAL_OUT:-internals/out}  # tests point this elsewhere
mkdir -p "$LOCAL_OUT"
LOG="$LOCAL_OUT/window_$(date +%Y%m%d_%H%M%S).log"
STOPPED=0 RAN=0 RESTORED=0
POLL=${POLL:-10}  # seconds between polls (tests set 0)

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
run() { log "+ $*"; (( DRY )) || "$@" 2>&1 | tee -a "$LOG"; }
r() { run ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "$@"; }
q() { ssh -o ConnectTimeout=10 -o BatchMode=yes "$HOST" "$@"; }  # quiet remote query, never in dry-run

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
/work/internals/run_internals.py --out /work/internals/out $*"
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
    (( DRY )) || q "docker rm -f $CNAME" >/dev/null 2>&1 || true
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
run rsync -az src/common.py "$HOST:$RD/src/"
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
run rsync -az "$HOST:$RD/internals/out/" "$LOCAL_OUT/"
if [[ -x internals/.venv/bin/python ]]; then
  run internals/.venv/bin/python internals/run_internals.py --stage score --out "$LOCAL_OUT" || log "local score failed (serving restore continues)"
fi

log "== 8. reboot, restart vLLM, health check"
restore
log "REMINDER: remove the temporary reboot rule now: ssh -t $HOST 'sudo rm /etc/sudoers.d/identity-reboot'"
