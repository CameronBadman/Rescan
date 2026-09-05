variable "region" {
  type    = string
  default = "ap-southeast-2"
}
variable "name" {
  type    = string
  default = "rescan"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,19}$", var.name))
    error_message = "Use 3–20 lowercase letters, digits, or hyphens."
  }
}
variable "api_domain" { type = string }
variable "route53_zone_id" { type = string }
variable "frontend_origin" { type = string }
variable "auth_callback_url" { type = string }
variable "api_image" {
  type = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.api_image))
    error_message = "Use a tested, digest-qualified API Lambda image."
  }
}
variable "worker_image" {
  type = string
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.worker_image))
    error_message = "Use a tested, digest-qualified worker image."
  }
}
variable "controller_jar" {
  type    = string
  default = "../controller/target/controller-0.1.0-SNAPSHOT.jar"
}
variable "turso_database_url" {
  type = string
  validation {
    condition     = can(regex("^https://", var.turso_database_url))
    error_message = "Supply the HTTPS Turso database endpoint."
  }
}
variable "turso_secret_arn" { type = string }
variable "redis_secret_arn" { type = string }
variable "operations_email" { type = string }
variable "controller_enabled" {
  type        = bool
  default     = false
  description = "Enable only after migrations and authenticated smoke checks."
}
variable "worker_profile_verified" {
  type        = bool
  default     = false
  description = "Set true only after the 1-vCPU/4-GB OCR acceptance benchmark passes."
}
