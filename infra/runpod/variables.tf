variable "runpod_api_key" {
  description = "RunPod API key (Settings > API Keys). Leave null to use RUNPOD_API_KEY from the environment."
  type        = string
  default     = null
  sensitive   = true
}

variable "name_prefix" {
  description = "Prefix for every RunPod resource this module creates."
  type        = string
  default     = "rescan"
}

variable "data_center_id" {
  description = <<-EOT
    RunPod data centre for the network volumes and pods. A network volume is
    pinned to one data centre, so the pod must be created in the same one.
    Pick one with storage support and stock of the GPUs you want; US-GA-2 and
    US-CA-2 had both H100 80GB and H200 at the time of writing.
  EOT
  type        = string
  default     = "US-GA-2"
}

variable "cloud_type" {
  description = "SECURE (RunPod-operated, public IP, network volumes) or COMMUNITY (cheaper, no network volumes)."
  type        = string
  default     = "SECURE"
  validation {
    condition     = contains(["SECURE", "COMMUNITY"], var.cloud_type)
    error_message = "cloud_type must be SECURE or COMMUNITY."
  }
}

variable "vllm_image" {
  description = "vLLM OpenAI-compatible server image. Pin a tag for reproducible deploys."
  type        = string
  default     = "vllm/vllm-openai:latest"
}

variable "vllm_api_key" {
  description = <<-EOT
    Bearer token vLLM requires on every request. The pod's port is reachable
    through RunPod's public proxy, so it must not be left open. Set the same
    value as RESCAN_LLM_API_KEY / RESCAN_COMPILE_API_KEY.
  EOT
  type        = string
  sensitive   = true
}

variable "hf_token" {
  description = "Hugging Face token, only needed for gated repositories. The Qwen models used here are public."
  type        = string
  default     = ""
  sensitive   = true
}

variable "deployments" {
  description = <<-EOT
    One vLLM pod per entry, each with its own network volume for the model
    weights so a spot interruption or a redeploy does not re-download them.

    The default is one pod: Qwen3.8-27B serving every pass. A second entry
    with role "compile" (see terraform.tfvars.example) routes the once-per-job
    plan-compile pass to a bigger model; it is optional.
  EOT
  type = map(object({
    model             = string
    served_name       = optional(string)
    gpu_type_ids      = list(string)
    gpu_count         = number
    tensor_parallel   = optional(number)
    max_model_len     = optional(number, 32768)
    max_num_seqs      = optional(number, 32)
    gpu_memory_util   = optional(number, 0.92)
    volume_gb         = number
    container_disk_gb = optional(number, 40)
    interruptible     = optional(bool, true)
    # Qwen3.8 thinks by default and has a reasoning parser; the 2507 Instruct
    # 235B is non-thinking and needs neither.
    reasoning_parser = optional(string)
    disable_thinking = optional(bool, false)
    extra_args       = optional(list(string), [])
    role             = optional(string, "bulk") # "bulk" | "compile"
  }))
  default = {
    bulk = {
      model            = "Qwen/Qwen3.8-27B"
      gpu_type_ids     = ["NVIDIA H100 80GB HBM3", "NVIDIA H100 NVL", "NVIDIA A100-SXM4-80GB"]
      gpu_count        = 1
      volume_gb        = 80
      reasoning_parser = "qwen3"
      disable_thinking = true
      role             = "bulk"
    }
  }

  validation {
    condition     = alltrue([for d in var.deployments : contains(["bulk", "compile"], d.role)])
    error_message = "Each deployment's role must be \"bulk\" or \"compile\"."
  }
  validation {
    condition     = alltrue([for d in var.deployments : d.gpu_count >= 1 && d.gpu_count <= 8])
    error_message = "gpu_count must be between 1 and 8."
  }
}
