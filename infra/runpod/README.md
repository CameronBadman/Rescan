# Rescan inference on RunPod

Terraform for the GPU side of Rescan: one vLLM pod per model, weights on a
network volume, spot by default. Bring it up for a batch, tear it down after.

```bash
cd infra/runpod
cp terraform.tfvars.example terraform.tfvars   # set vllm_api_key, choose the shape
export RUNPOD_API_KEY=...                      # or set runpod_api_key in tfvars
terraform init && terraform apply
terraform output -raw rescan_env > ../../.env  # base URLs, model names, key
terraform destroy                              # stops the meter; volumes are kept only if you keep them
```

## The two shapes

**Default — two pods.** The per-candidate passes (structure, anonymize, judge,
rank) run hundreds of times per batch on **Qwen3.8-27B** (1× H100 80 GB,
~$3/h list). The once-per-job plan-compile pass — the one where model quality
shows — runs on **Qwen3-235B-A22B-Instruct-2507-FP8** (4× H100, ~$12/h list,
less on spot). The output wires `RESCAN_LLM_*` to the first and
`RESCAN_COMPILE_*` to the second; Rescan's client routes `compile_dsl` and
its repair round to the compile server and everything else to the bulk one.

**One big pod.** `terraform.tfvars.example` runs everything on the 235B. Fewer
moving parts, higher per-candidate cost, and the 22B-active MoE decodes about
as fast as the 27B dense, so the batch is not slower — just pricier per hour.

Drop the `compile` entry instead to run everything on the 27B and see whether
its DSL compilation is good enough before paying for the bigger model. That is
the right first experiment: `python -m scripts.smoke_real_model` prints the
compiled program.

## What to know

- **Spot.** `interruptible = true` is RunPod's spot tier. The pod can be
  stopped at any time; the network volume keeps the weights, and Rescan's
  client retries once and dead-letters a document rather than losing it. Set
  `interruptible = false` for an interactive demo.
- **Network volumes are pinned to a data centre**, so the pod is created in
  the same one (`data_center_id`). Choose one with storage support and stock
  of the GPU you want; `US-GA-2` and `US-CA-2` had H100 80 GB and H200 at the
  time of writing. Volumes cost a few cents per GB-month and are the only
  thing worth keeping between applies — first boot downloads ~55 GB for the
  27B and ~235 GB for the 235B, subsequent boots load from the volume.
- **The endpoint is public.** `https://<pod-id>-8000.proxy.runpod.net/v1`
  is reachable from anywhere, which is why vLLM is started with an API key
  (`vllm_api_key`) that the output puts into `RESCAN_LLM_API_KEY`.
- **RunPod's HTTP proxy times out at 100 s per request.** Every Rescan pass
  finishes well inside that on a GPU (the compile pass is the longest, a few
  thousand output tokens). If a request ever needs longer, expose `8000/tcp`
  instead and use the pod's public IP and mapped port.
- **235B on two H200s** (282 GB) also works with `tensor_parallel = 2` and a
  shorter `max_model_len`; the model card's recipe is tensor-parallel 4,
  which is what the default uses.
- **Thinking.** Qwen3.8 thinks by default, so its deployment sets the `qwen3`
  reasoning parser and turns thinking off server-side; the 2507 Instruct 235B
  is non-thinking and needs neither. Rescan's client also disables thinking
  per request and strips any `<think>` block, so either way the JSON is clean.

## Validating without a key

`terraform validate` needs no credentials. `terraform plan` and `apply` need
`RUNPOD_API_KEY`.
