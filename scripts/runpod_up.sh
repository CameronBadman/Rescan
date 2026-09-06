#!/usr/bin/env bash
# Bring up Rescan's inference on RunPod with runpodctl — no Terraform state.
#
# One vLLM pod, weights on a network volume that is reused across runs.
# Default: Qwen3.8-27B on one H100 serving every pass. On-demand pods
# (runpodctl has no spot flag; infra/runpod does).
#
#   export RUNPOD_API_KEY=...            # or RUNPOD_API_KEY= in .env
#   ./scripts/runpod_up.sh               # Qwen3.8-27B, everything on it
#   SHAPE=two ./scripts/runpod_up.sh     # optional: also a 235B pod for plan compilation
#   SHAPE=big ./scripts/runpod_up.sh     # optional: everything on the 235B
#   WAIT=1 ./scripts/runpod_up.sh        # also wait until vLLM has loaded the weights
#
# Writes the RESCAN_LLM_* / RESCAN_COMPILE_* lines into .env, records the pod
# ids in .runpod/pods.json for runpod_down.sh, and writes
# infra/aws/llm.auto.tfvars so ./scripts/deploy_aws.sh pushes the same
# settings to the API instance.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${RUNPOD_API_KEY:-}" ] && [ -f .env ]; then
  RUNPOD_API_KEY="$(sed -n 's/^RUNPOD_API_KEY=//p' .env | tail -1)"
  export RUNPOD_API_KEY
fi
[ -n "${RUNPOD_API_KEY:-}" ] || { echo "RUNPOD_API_KEY is not set (shell or .env)" >&2; exit 2; }

SHAPE="${SHAPE:-small}"                   # small (27B, default) | two | big
DC="${DC:-US-GA-2}"                       # must support network volumes and stock the GPUs
IMAGE="${IMAGE:-vllm/vllm-openai:latest}"
BULK_GPU="${BULK_GPU:-NVIDIA H100 80GB HBM3}"
BIG_GPU="${BIG_GPU:-NVIDIA H100 80GB HBM3}"
BIG_GPU_COUNT="${BIG_GPU_COUNT:-4}"
MAX_LEN="${MAX_LEN:-32768}"
VLLM_API_KEY="${VLLM_API_KEY:-$(sed -n 's/^RESCAN_LLM_API_KEY=//p' .env 2>/dev/null | tail -1)}"
[ -n "$VLLM_API_KEY" ] && [ "$VLLM_API_KEY" != "not-needed" ] || VLLM_API_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(30))')"

BULK_MODEL="Qwen/Qwen3.8-27B"
BIG_MODEL="Qwen/Qwen3-235B-A22B-Instruct-2507-FP8"
mkdir -p .runpod

# Parse runpodctl's JSON; an {"error": ...} body or empty output is fatal.
json() {
  python3 -c "
import json, sys
raw = sys.stdin.read().strip()
if not raw:
    sys.exit('runpodctl returned no output')
d = json.loads(raw)
if isinstance(d, dict) and d.get('error'):
    sys.exit('runpodctl: ' + str(d['error']))
print($1)"
}

# volume <name> <size_gb> -> prints the volume id, creating it if needed
volume() {
  local name="$1" size="$2" id
  id="$(runpodctl network-volume list -o json 2>/dev/null | json "next((v['id'] for v in (d if isinstance(d, list) else d.get('networkVolumes', d.get('data', []))) if v.get('name')=='$name'), '')")"
  if [ -z "$id" ]; then
    echo "creating network volume $name (${size} GB, $DC)" >&2
    id="$(runpodctl network-volume create --name "$name" --size "$size" --data-center-id "$DC" -o json | json "d.get('id') or d.get('networkVolume',{}).get('id')")" || exit 1
  fi
  [ -n "$id" ] || { echo "no network volume id for $name; not creating a pod without one" >&2; exit 1; }
  echo "$id"
}

# pod <key> <model> <gpu> <count> <volume_gb> <extra vllm args...>
pod() {
  local key="$1" model="$2" gpu="$3" count="$4" vol_gb="$5"; shift 5
  local vol_id args env id
  vol_id="$(volume "rescan-$key-weights" "$vol_gb")" || exit 1
  args="--model $model --served-model-name $model --host 0.0.0.0 --port 8000 --tensor-parallel-size $count --max-model-len $MAX_LEN --gpu-memory-utilization 0.92 --limit-mm-per-prompt image=0,video=0 $*"
  env="$(python3 -c "import json; print(json.dumps({'HF_HOME': '/workspace/huggingface', 'HF_HUB_ENABLE_HF_TRANSFER': '1', 'VLLM_API_KEY': '$VLLM_API_KEY', 'VLLM_USE_V1': '1'}))")"
  echo "creating pod rescan-$key: $count x $gpu, $model" >&2
  id="$(runpodctl pod create --name "rescan-$key" --image "$IMAGE" --gpu-id "$gpu" --gpu-count "$count" \
        --cloud-type SECURE --data-center-ids "$DC" --container-disk-in-gb 40 \
        --network-volume-id "$vol_id" --volume-mount-path /workspace --ports "8000/http" \
        --env "$env" --docker-args "$args" --ssh=false -o json | json "d.get('id') or d.get('pod',{}).get('id')")" || exit 1
  [ -n "$id" ] || { echo "pod create returned no id" >&2; exit 1; }
  echo "$id"
}

bulk_id=""; big_id=""
case "$SHAPE" in
  two)   bulk_id="$(pod bulk "$BULK_MODEL" "$BULK_GPU" 1 80 --max-num-seqs 32 --reasoning-parser qwen3)" || exit 1
         big_id="$(pod compile "$BIG_MODEL" "$BIG_GPU" "$BIG_GPU_COUNT" 300 --max-num-seqs 8)" || exit 1 ;;
  big)   big_id="$(pod compile "$BIG_MODEL" "$BIG_GPU" "$BIG_GPU_COUNT" 300 --max-num-seqs 16)" || exit 1 ;;
  small) bulk_id="$(pod bulk "$BULK_MODEL" "$BULK_GPU" 1 80 --max-num-seqs 32 --reasoning-parser qwen3)" || exit 1 ;;
  *) echo "SHAPE must be two, big or small" >&2; exit 2 ;;
esac

url() { echo "https://$1-8000.proxy.runpod.net/v1"; }
bulk_url=""; big_url=""
[ -n "$bulk_id" ] && bulk_url="$(url "$bulk_id")"
[ -n "$big_id" ] && big_url="$(url "$big_id")"
llm_url="${bulk_url:-$big_url}"; llm_model="$([ -n "$bulk_id" ] && echo "$BULK_MODEL" || echo "$BIG_MODEL")"

python3 - "$bulk_id" "$big_id" <<'EOF'
import json, sys, datetime
bulk, big = sys.argv[1], sys.argv[2]
json.dump({"created": datetime.datetime.now(datetime.timezone.utc).isoformat(), "pods": {k: v for k, v in {"bulk": bulk, "compile": big}.items() if v}},
          open(".runpod/pods.json", "w"), indent=2)
EOF

# .env lines for the local tools and the same settings for the API instance.
{
  grep -vE "^RESCAN_(LLM_BACKEND|LLM_BASE_URL|LLM_MODEL|LLM_API_KEY|LLM_DISABLE_THINKING|COMPILE_BASE_URL|COMPILE_MODEL|COMPILE_API_KEY)=" .env 2>/dev/null || true
  echo "RESCAN_LLM_BACKEND=openai"
  echo "RESCAN_LLM_BASE_URL=$llm_url"
  echo "RESCAN_LLM_MODEL=$llm_model"
  echo "RESCAN_LLM_API_KEY=$VLLM_API_KEY"
  echo "RESCAN_LLM_DISABLE_THINKING=true"
  if [ -n "$bulk_id" ] && [ -n "$big_id" ]; then
    echo "RESCAN_COMPILE_BASE_URL=$big_url"
    echo "RESCAN_COMPILE_MODEL=$BIG_MODEL"
    echo "RESCAN_COMPILE_API_KEY=$VLLM_API_KEY"
  fi
} > .env.new && mv .env.new .env

python3 - "$llm_url" "$llm_model" "$VLLM_API_KEY" "$big_url" "$BIG_MODEL" "$bulk_id" "$big_id" <<'EOF'
import json, sys
llm_url, llm_model, key, big_url, big_model, bulk, big = sys.argv[1:]
settings = {"RESCAN_LLM_BACKEND": "openai", "RESCAN_LLM_BASE_URL": llm_url, "RESCAN_LLM_MODEL": llm_model,
            "RESCAN_LLM_API_KEY": key, "RESCAN_LLM_DISABLE_THINKING": "true"}
if bulk and big:
    settings.update({"RESCAN_COMPILE_BASE_URL": big_url, "RESCAN_COMPILE_MODEL": big_model, "RESCAN_COMPILE_API_KEY": key})
with open("infra/aws/llm.auto.tfvars", "w") as f:
    f.write("llm_settings = " + json.dumps(settings, indent=2) + "\n")
EOF

echo
echo "pods: bulk=${bulk_id:-none} compile=${big_id:-none}  (ids in .runpod/pods.json)"
echo "env written to .env; API settings written to infra/aws/llm.auto.tfvars"
echo "next: wait for the weights to load, then ./scripts/deploy_aws.sh and python -m scripts.smoke_real_model"

if [ "${WAIT:-0}" = "1" ]; then
  for id in $bulk_id $big_id; do
    printf "waiting for %s to serve" "$id"
    for _ in $(seq 1 240); do
      if curl -fsS -m 10 -H "Authorization: Bearer $VLLM_API_KEY" "$(url "$id")/models" >/dev/null 2>&1; then echo " ready"; break; fi
      printf "."; sleep 15
    done
  done
fi
