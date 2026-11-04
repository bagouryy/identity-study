#!/usr/bin/env bash
# Unattended follow-up window that runs ON the GPU host, so nothing depends on the laptop or the network.
# State machine in runs/runner.state; a one-off @reboot crontab entry resumes it after each reboot.
#   start  : stop serving vLLM, reboot
#   models : gate, serve OLMo + gpt-oss side by side, generate, remove them, reboot
#   intern : gate, internals v2 (3 h cap), reboot
#   restore: start serving vLLM + listed containers, health check, remove the crontab entry   (also the failure path)
# usage: internals/host_runner.sh start | resume        DRY=1 prints instead of acting (no docker, no reboot)
set -o pipefail
LEAVING=0  # set when we hand over to a reboot or finish on purpose
RD=${RD:-$HOME/identity-study}
cd "$RD" || exit 1
VLLM_CONTAINER=${VLLM_CONTAINER:?set VLLM_CONTAINER}
RESTART_ALSO=${RESTART_ALSO:-}  # containers the reboots leave stopped
MODEL=Qwen/Qwen3.6-35B-A3B
IMAGE=vllm/vllm-openai:cu130-nightly
STATE=runs/runner.state
MAX_BOOTS=4
DRY=${DRY:-0}
GEN_ARGS="--split test --modes std --conditions C0 C1 C3 E N1 N3 N10 --runs 0 1 2 --categories identity --concurrency 32"
MODELS=("olmo allenai/OLMo-2-1124-7B-Instruct 8101 4096" "gptoss openai/gpt-oss-20b 8103 8192")

log() { echo "[$(date '+%F %T')] $*"; }
do_() { log "+ $*"; (( DRY )) || "$@"; }
st() { cat "$STATE" 2>/dev/null || echo none; }
set_st() { echo "$1" > "$STATE"; log "state -> $1"; }
boots() { cat runs/runner.boots 2>/dev/null || echo 0; }

cron_on() {
  local line="@reboot sleep 45 && VLLM_CONTAINER=$VLLM_CONTAINER RESTART_ALSO='$RESTART_ALSO' $RD/internals/host_runner.sh resume >> $RD/runs/runner.log 2>&1"
  (( DRY )) && { log "+ crontab: $line"; return; }
  ( crontab -l 2>/dev/null | grep -v host_runner.sh; echo "$line" ) | crontab -
}
cron_off() { (( DRY )) && { log "+ crontab: remove entry"; return; }; crontab -l 2>/dev/null | grep -v host_runner.sh | crontab -; }

reboot_to() {  # reboot_to <next state>
  set_st "$1"
  local b; b=$(( $(boots) + 1 )); echo "$b" > runs/runner.boots
  if (( b > MAX_BOOTS )); then log "reboot budget used up ($b): restoring without reboot"; restore; exit; fi
  cron_on
  LEAVING=1
  do_ sudo -n reboot
  (( DRY )) || sleep 600  # the box goes down here
}

fail() { log "FAILED: $*"; reboot_to restore; exit 1; }

gate() {
  (( DRY )) && return 0
  local avail swap
  read -r avail swap < <(free -g | awk '/^Mem:/{a=$7} /^Swap:/{s=$3} END{print a, s}')
  log "gate: available ${avail} GB, swap ${swap} GB"
  (( avail >= 90 && swap <= 1 ))
}

wait_docker() { (( DRY )) && return 0; for _ in $(seq 60); do docker info >/dev/null 2>&1 && return 0; sleep 5; done; return 1; }

ready() {  # ready <port> <model id>, 15 min
  (( DRY )) && return 0
  for _ in $(seq 90); do curl -sf -m 5 "localhost:$1/v1/models" | grep -q "$2" && return 0; sleep 10; done; return 1
}

models() {
  local n id port mlen pids=() up=()
  for m in "${MODELS[@]}"; do
    read -r n id port mlen <<< "$m"
    do_ docker rm -f "identity-$n" >/dev/null 2>&1
    do_ docker run -d --name "identity-$n" --gpus all --ipc=host -p "127.0.0.1:$port:8000" -v "$RD/hf:/hf" \
      -v "$HOME/.cache/huggingface/hub/models--openai--gpt-oss-20b:/hf/hub/models--openai--gpt-oss-20b:ro" \
      -v "$RD/tiktoken:/tiktoken:ro" -e TIKTOKEN_ENCODINGS_BASE=/tiktoken -e HF_HOME=/hf -e HF_HUB_OFFLINE=1 \
      "$IMAGE" "$id" --gpu-memory-utilization 0.25 --max-model-len "$mlen"
    if ready "$port" "$id"; then up+=("$m"); log "$n ready"
    else log "$n not ready, skipping"; (( DRY )) || docker logs --tail 30 "identity-$n" 2>&1; do_ docker rm -f "identity-$n"; fi
  done
  for m in "${up[@]}"; do
    read -r n id port _ <<< "$m"
    log "generate $n"
    if (( DRY )); then log "+ python3 src/generate.py --run-id x_$n --model $id --endpoint http://localhost:$port/v1 $GEN_ARGS"; continue; fi
    ( for i in 1 2; do timeout 90m python3 src/generate.py --run-id "x_$n" --model "$id" \
        --endpoint "http://localhost:$port/v1" $GEN_ARGS >> "runs/x_$n.log" 2>&1; done; echo DONE >> "runs/x_$n.log" ) &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p"; done
  log "model generation finished"
  for m in "${MODELS[@]}"; do read -r n _ <<< "$m"; do_ docker rm -f "identity-$n" >/dev/null 2>&1; done
}

internals() {
  do_ timeout 3h docker run --rm --name identity-internals --gpus all --ipc=host -e HF_HUB_OFFLINE=1 \
    -v "$HOME/.cache/huggingface:/root/.cache/huggingface" -v "$RD:/work" --entrypoint python3 "$IMAGE" \
    /work/internals/run_v2.py --out /work/internals/out_v2 --stage all
}

restore() {
  wait_docker || log "docker not answering"
  do_ docker rm -f identity-olmo identity-gptoss identity-internals >/dev/null 2>&1
  do_ docker start "$VLLM_CONTAINER"
  [[ -n "$RESTART_ALSO" ]] && do_ docker start $RESTART_ALSO
  if ready 8001 "$MODEL"; then
    (( DRY )) || curl -sf -m 120 localhost:8001/v1/chat/completions -H 'Content-Type: application/json' \
      -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with OK.\"}],\"max_tokens\":8,\"chat_template_kwargs\":{\"enable_thinking\":false}}" \
      && log "health OK" || log "WARNING: model listed but chat request failed"
  else
    log "RESTORE FAILED: $MODEL not listed after 15 min. By hand: docker start $VLLM_CONTAINER (reboot first if memory is full)"
  fi
  cron_off
  set_st done
  LEAVING=1
  (( DRY )) || docker ps --format '{{.Names}}\t{{.Status}}'
}

on_exit() {  # any unexpected exit mid-window still brings serving back
  local s; s=$(st)
  (( LEAVING )) || [[ $s != models && $s != intern ]] || { log "unexpected exit in state $s"; reboot_to restore; }
}
trap on_exit EXIT

case "${1:-}" in
  start)
    [[ "$(st)" == none || "$(st)" == done ]] || { log "runner already in state $(st)"; exit 1; }
    sudo -n -l reboot >/dev/null 2>&1 || (( DRY )) || { log "no passwordless reboot; nothing changed"; exit 1; }
    echo 0 > runs/runner.boots
    log "== start: stop $VLLM_CONTAINER"
    do_ docker stop -t 60 "$VLLM_CONTAINER"
    reboot_to models ;;
  resume)
    wait_docker || fail "docker not up after boot"
    log "== resume in state $(st)"
    case "$(st)" in
      models) gate || fail "memory gate before models"; models; reboot_to intern ;;
      intern) gate || fail "memory gate before internals"; internals || log "internals exit $?"; reboot_to restore ;;
      restore) restore ;;
      *) log "nothing to do in state $(st)"; cron_off ;;
    esac ;;
  *) echo "usage: $0 start|resume"; exit 2 ;;
esac
