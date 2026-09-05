variable "region" { type = string }
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
  type        = string
  default     = null
  description = "Immutable ECR image URI; null uses the repository's bootstrap tag."
}
variable "worker_image" {
  type    = string
  default = null
}
variable "controller_jar" {
  type    = string
  default = "../controller/target/controller-0.1.0-SNAPSHOT.jar"
}
variable "db_instance_class" {
  type    = string
  default = "db.t4g.small"
}
variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}
variable "deletion_protection" {
  type    = bool
  default = true
}
