locals {
  base_urls = {
    for name, pod in runpod_pod.vllm : name => "https://${pod.id}-8000.proxy.runpod.net/v1"
  }
  bulk_name    = try([for n, d in var.deployments : n if d.role == "bulk"][0], null)
  compile_name = try([for n, d in var.deployments : n if d.role == "compile"][0], null)

  # With only one deployment, every pass goes to it.
  bulk_target    = coalesce(local.bulk_name, local.compile_name)
  compile_target = local.compile_name != null && local.compile_name != local.bulk_target ? local.compile_name : null
}

output "pods" {
  description = "Pod id, endpoint and hourly cost per deployment."
  value = {
    for name, pod in runpod_pod.vllm : name => {
      id          = pod.id
      base_url    = local.base_urls[name]
      model       = var.deployments[name].model
      gpus        = "${var.deployments[name].gpu_count}x ${join("|", var.deployments[name].gpu_type_ids)}"
      spot        = var.deployments[name].interruptible
      cost_per_hr = pod.cost_per_hr
      data_center = pod.actual_data_center
    }
  }
}

output "network_volumes" {
  description = "Volume ids holding the model weights. Keep these between applies."
  value       = { for name, vol in runpod_network_volume.weights : name => vol.id }
}

output "rescan_env" {
  description = "Lines for Rescan's .env. Contains the vLLM API key."
  sensitive   = true
  value = join("\n", compact([
    "RESCAN_LLM_BACKEND=openai",
    "RESCAN_LLM_BASE_URL=${local.base_urls[local.bulk_target]}",
    "RESCAN_LLM_MODEL=${coalesce(var.deployments[local.bulk_target].served_name, var.deployments[local.bulk_target].model)}",
    "RESCAN_LLM_API_KEY=${var.vllm_api_key}",
    "RESCAN_LLM_DISABLE_THINKING=${var.deployments[local.bulk_target].disable_thinking}",
    local.compile_target == null ? "" : "RESCAN_COMPILE_BASE_URL=${local.base_urls[local.compile_target]}",
    local.compile_target == null ? "" : "RESCAN_COMPILE_MODEL=${coalesce(var.deployments[local.compile_target].served_name, var.deployments[local.compile_target].model)}",
    local.compile_target == null ? "" : "RESCAN_COMPILE_API_KEY=${var.vllm_api_key}",
  ]))
}

output "smoke_test" {
  description = "Run this once the pods report RUNNING and vLLM has loaded the weights."
  value       = "terraform output -raw rescan_env > ../../.env && cd ../.. && python -m scripts.smoke_real_model"
}
