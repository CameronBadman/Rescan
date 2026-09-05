variable "region" {
  description = "AWS region for the bucket and IAM resources."
  type        = string
  default     = "ap-southeast-2" # Sydney: keep candidate data in Australia.
}

variable "bucket_name" {
  description = "Globally unique name for the resumes bucket. Leave null for rescan-resumes-<account id>."
  type        = string
  default     = null
}

variable "prefix" {
  description = "Key prefix a job's resumes live under: <prefix>/<jobId>/... Matches RESCAN_S3_PREFIX."
  type        = string
  default     = "jobs"
}

variable "retention_days" {
  description = <<-EOT
    Days after which a job's resumes are deleted. Candidate documents are
    personal information collected for one hiring round; the Privacy Act
    expects them not to be kept longer than needed. 0 disables expiry.
  EOT
  type        = number
  default     = 90
}

variable "create_access_key" {
  description = "Create an access key for the Rescan IAM user and expose it as a sensitive output."
  type        = bool
  default     = true
}

variable "tags" {
  type = map(string)
  default = {
    project = "rescan"
  }
}
