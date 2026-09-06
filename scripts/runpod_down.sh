#!/usr/bin/env bash
# Tear down the pods runpod_up.sh created. Network volumes (the weights) are
# kept so the next runpod_up.sh boots without re-downloading.
#
#   ./scripts/runpod_down.sh          # delete the pods (stops the meter)
#   ./scripts/runpod_down.sh --stop   # stop instead of delete (container disk still billed)
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -z "${RUNPOD_API_KEY:-}" ] && [ -f .env ]; then
  RUNPOD_API_KEY="$(sed -n 's/^RUNPOD_API_KEY=//p' .env | tail -1)"
  export RUNPOD_API_KEY
fi
[ -f .runpod/pods.json ] || { echo "no .runpod/pods.json — nothing to take down" >&2; exit 0; }

action="delete"; [ "${1:-}" = "--stop" ] && action="stop"
for id in $(python3 -c 'import json; print(" ".join(json.load(open(".runpod/pods.json"))["pods"].values()))'); do
  echo "$action pod $id"
  runpodctl pod "$action" "$id" >/dev/null || echo "  (already gone?)"
done
[ "$action" = "delete" ] && rm -f .runpod/pods.json
echo "done. the API instance still points at the old URLs; run runpod_up.sh again or set RESCAN_LLM_BACKEND=stub in infra/aws/llm.auto.tfvars and ./scripts/deploy_aws.sh"
