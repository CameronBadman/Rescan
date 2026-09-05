#!/usr/bin/env bash
# Serve Qwen3.8-27B with vLLM for Rescan.
#
# Rescan's passes are all schema-guided JSON, and every pass that needs
# reasoning carries it in its schema, so thinking is off by default on the
# server and the client also turns it off per request. The reasoning parser
# is still configured so that, if a request enables thinking, the <think>
# block lands in reasoning_content instead of the JSON body (vLLM >= 0.9).
#
#   ./scripts/serve_vllm.sh                      # Qwen/Qwen3.8-27B on :8000
#   MODEL=Qwen/Qwen3.8-27B-FP8 ./scripts/serve_vllm.sh
#   TP=2 MAX_LEN=65536 ./scripts/serve_vllm.sh
set -euo pipefail

MODEL="${MODEL:-Qwen/Qwen3.8-27B}"
PORT="${PORT:-8000}"
TP="${TP:-1}"                   # tensor parallel size (GPUs)
MAX_LEN="${MAX_LEN:-32768}"     # a resume + the compile prompt fit comfortably in 32k
GPU_UTIL="${GPU_UTIL:-0.90}"
MAX_SEQS="${MAX_SEQS:-32}"      # continuous batching depth; pair with RESCAN_LLM_MAX_CONCURRENCY

exec vllm serve "$MODEL" \
  --host 0.0.0.0 --port "$PORT" \
  --served-model-name "$MODEL" \
  --tensor-parallel-size "$TP" \
  --max-model-len "$MAX_LEN" \
  --gpu-memory-utilization "$GPU_UTIL" \
  --max-num-seqs "$MAX_SEQS" \
  --reasoning-parser qwen3 \
  --default-chat-template-kwargs '{"enable_thinking": false}' \
  --limit-mm-per-prompt '{"image": 0, "video": 0}' \
  "$@"
