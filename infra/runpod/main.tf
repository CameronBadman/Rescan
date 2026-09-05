# One vLLM pod per deployment, weights on a network volume, interruptible by
# default. `terraform apply` brings the GPUs up, `terraform destroy` stops
# paying for them; the volumes are the only thing worth keeping between runs
# and they are cheap.

locals {
  # Weights live on the network volume so that neither a spot interruption
  # nor a redeploy downloads them again.
  hf_home = "/workspace/huggingface"

  vllm_args = {
    for name, d in var.deployments : name => concat(
      [
        "--model", d.model,
        "--served-model-name", coalesce(d.served_name, d.model),
        "--host", "0.0.0.0",
        "--port", "8000",
        "--tensor-parallel-size", tostring(coalesce(d.tensor_parallel, d.gpu_count)),
        "--max-model-len", tostring(d.max_model_len),
        "--max-num-seqs", tostring(d.max_num_seqs),
        "--gpu-memory-utilization", tostring(d.gpu_memory_util),
        "--limit-mm-per-prompt", "{\"image\": 0, \"video\": 0}",
      ],
      d.reasoning_parser == null ? [] : ["--reasoning-parser", d.reasoning_parser],
      d.disable_thinking ? ["--default-chat-template-kwargs", "{\"enable_thinking\": false}"] : [],
      d.extra_args,
    )
  }
}

resource "runpod_network_volume" "weights" {
  for_each = var.deployments

  name           = "${var.name_prefix}-${each.key}-weights"
  size           = each.value.volume_gb
  data_center_id = var.data_center_id
}

resource "runpod_pod" "vllm" {
  for_each = var.deployments

  name         = "${var.name_prefix}-${each.key}"
  image_name   = var.vllm_image
  cloud_type   = var.cloud_type
  compute_type = "GPU"

  gpu_type_ids      = each.value.gpu_type_ids
  gpu_type_priority = "availability"
  gpu_count         = each.value.gpu_count
  data_center_ids   = [var.data_center_id]

  # Spot. RunPod may stop the pod at any time; the volume keeps the weights
  # and Rescan retries and dead-letters rather than losing work.
  interruptible = each.value.interruptible

  container_disk_in_gb = each.value.container_disk_gb
  network_volume_id    = runpod_network_volume.weights[each.key].id
  volume_mount_path    = "/workspace"

  # 8000/http is reachable at https://<pod-id>-8000.proxy.runpod.net.
  ports = ["8000/http"]

  docker_start_cmd = local.vllm_args[each.key]

  env = merge(
    {
      HF_HOME                   = local.hf_home
      HF_HUB_ENABLE_HF_TRANSFER = "1"
      VLLM_API_KEY              = var.vllm_api_key
      # Prefix caching makes the 6k-token compile system prompt nearly free
      # after the first call; it is the default in vLLM v1 but stated here.
      VLLM_USE_V1 = "1"
    },
    var.hf_token == "" ? {} : { HF_TOKEN = var.hf_token },
  )
}
