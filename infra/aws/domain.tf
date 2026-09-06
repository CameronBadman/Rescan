# fairmatch.dev on Cloudflare DNS in front of the two CloudFront distributions.
#
# CloudFront only answers for hostnames it knows, with a certificate in
# us-east-1, so this is two phases:
#
#   1. apply with domain_validated = false: the certificate is requested and
#      `dns_records` prints the validation CNAMEs plus the app/API CNAMEs to
#      add in Cloudflare (DNS-only or proxied with SSL mode "Full (strict)").
#   2. once ACM shows the certificate issued, apply with domain_validated =
#      true: the aliases and certificate are attached, the API's CORS list
#      already includes the new origins, and deploy_frontend.sh builds the
#      frontend against https://api.<domain>.

variable "domain" {
  description = "Apex domain for the frontend (also served at www.). Empty disables custom domains."
  type        = string
  default     = "fairmatch.dev"
}

variable "api_subdomain" {
  description = "Subdomain for the API under `domain`."
  type        = string
  default     = "api"
}

variable "domain_validated" {
  description = "Set true after the ACM validation CNAMEs are in Cloudflare and the certificate is issued."
  type        = bool
  default     = false
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1" # CloudFront certificates must live here
}

locals {
  use_domain    = var.deploy_api && var.domain != ""
  domain_count  = local.use_domain ? 1 : 0
  api_hostname  = "${var.api_subdomain}.${var.domain}"
  www_hostname  = "www.${var.domain}"
  aliases_ready = local.use_domain && var.domain_validated
  domain_origins = local.use_domain ? [
    "https://${var.domain}", "https://${local.www_hostname}",
  ] : []
}

resource "aws_acm_certificate" "site" {
  count                     = local.domain_count
  provider                  = aws.us_east_1
  domain_name               = var.domain
  subject_alternative_names = [local.www_hostname, local.api_hostname]
  validation_method         = "DNS"
  tags                      = var.tags

  lifecycle {
    create_before_destroy = true
  }
}

output "dns_records" {
  description = "Records to create in Cloudflare for the domain."
  value = local.use_domain ? {
    validation = [
      for option in aws_acm_certificate.site[0].domain_validation_options : {
        type  = option.resource_record_type
        name  = trimsuffix(option.resource_record_name, ".")
        value = trimsuffix(option.resource_record_value, ".")
        note  = "ACM validation — DNS only (grey cloud)"
      }
    ]
    site = [
      { type = "CNAME", name = var.domain, value = aws_cloudfront_distribution.frontend[0].domain_name, note = "frontend (Cloudflare flattens the apex CNAME)" },
      { type = "CNAME", name = local.www_hostname, value = aws_cloudfront_distribution.frontend[0].domain_name, note = "frontend" },
      { type = "CNAME", name = local.api_hostname, value = aws_cloudfront_distribution.api[0].domain_name, note = "API" },
    ]
    cloudflare_ssl_mode = "Full (strict) if proxied; the origin is CloudFront with a valid certificate"
    next                = var.domain_validated ? "aliases attached" : "add the validation records, wait for ACM to issue, then apply with -var domain_validated=true"
  } : null
}

output "site_urls" {
  value = local.aliases_ready ? {
    frontend = "https://${var.domain}"
    api      = "https://${local.api_hostname}"
  } : null
}
