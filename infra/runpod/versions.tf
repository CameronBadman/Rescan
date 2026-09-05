terraform {
  required_version = ">= 1.6"

  required_providers {
    runpod = {
      source  = "decentralized-infrastructure/runpod"
      version = "~> 1.0"
    }
  }
}

# RUNPOD_API_KEY in the environment also works.
provider "runpod" {
  api_key = var.runpod_api_key
}
