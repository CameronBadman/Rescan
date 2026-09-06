# The Rescan API on one small instance, fronted by CloudFront for TLS.
#
# No container registry or image build is needed: the instance bootstraps
# from a source archive in a private artifacts bucket (scripts/deploy_aws.sh
# uploads it and triggers a refresh over SSM). The instance reads the resumes
# bucket through its role — no access keys on the box — and is reachable only
# from CloudFront, which terminates HTTPS on its own domain so no Route 53
# zone or certificate is needed. Inference is wherever RESCAN_LLM_BASE_URL
# points; the default is the deterministic stub until a GPU is up.

variable "deploy_api" {
  description = "Create the API instance and CloudFront distribution."
  type        = bool
  default     = true
}

variable "instance_type" {
  description = "arm64 instance for the API and Tika. t4g.medium (4 GB) leaves room for the Tika JVM."
  type        = string
  default     = "t4g.medium"
}

variable "llm_settings" {
  description = "RESCAN_LLM_* / RESCAN_COMPILE_* values for the API's environment file. Flip these when inference is up."
  type        = map(string)
  default = {
    RESCAN_LLM_BACKEND = "stub"
  }
  sensitive = true
}

variable "extra_env" {
  description = "Any additional RESCAN_* settings for the API."
  type        = map(string)
  default     = {}
}

variable "frontend_origins" {
  description = "Browser origins allowed to call the API (CORS). Local dev servers plus the deployed frontend."
  type        = list(string)
  default     = ["http://localhost:3000", "http://localhost:5173", "http://127.0.0.1:3000"]
}

locals {
  api_count    = var.deploy_api ? 1 : 0
  artifact_key = "rescan.zip"
  tika_version = "2.9.2"
  env_file_lines = merge(
    {
      RESCAN_DATA_DIR               = "/var/lib/rescan"
      RESCAN_DB_PATH                = "/var/lib/rescan/rescan.db"
      RESCAN_UPLOAD_DIR             = "/var/lib/rescan/uploads"
      RESCAN_LOCAL_OBJECT_STORE_DIR = "/var/lib/rescan/bucket"
      RESCAN_TIKA_URL               = "http://127.0.0.1:9998"
      RESCAN_OBJECT_STORE           = "s3"
      RESCAN_S3_BUCKET              = aws_s3_bucket.resumes.bucket
      RESCAN_S3_REGION              = var.region
      RESCAN_S3_PREFIX              = var.prefix
      # Two keys: one for agents/MCP, one for the frontend, rotatable apart.
      RESCAN_API_KEYS         = "${random_password.api_key.result},${random_password.frontend_key.result}"
      RESCAN_CORS_ORIGINS     = join(",", concat(var.frontend_origins, var.deploy_api ? ["https://${aws_cloudfront_distribution.frontend[0].domain_name}"] : []))
      # Candidates processed concurrently. Each is a few sequential model
      # calls, so this is what keeps vLLM's continuous batching fed.
      RESCAN_PIPELINE_WORKERS = "12"
    },
    var.llm_settings,
    var.extra_env,
  )
}

resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "random_password" "frontend_key" {
  length  = 40
  special = false
}

# --------------------------------------------------------------------------
# Artifacts: the source archive the instance installs from
# --------------------------------------------------------------------------

resource "aws_s3_bucket" "artifacts" {
  count  = local.api_count
  bucket = "rescan-artifacts-${data.aws_caller_identity.current.account_id}"
  tags   = var.tags
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  count                   = local.api_count
  bucket                  = aws_s3_bucket.artifacts[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  count  = local.api_count
  bucket = aws_s3_bucket.artifacts[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

# --------------------------------------------------------------------------
# Instance role: read resumes, read artifacts, be managed over SSM
# --------------------------------------------------------------------------

data "aws_iam_policy_document" "instance_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  count              = local.api_count
  name               = "rescan-api-instance"
  assume_role_policy = data.aws_iam_policy_document.instance_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "api_instance" {
  count = local.api_count
  statement {
    sid       = "ListJobPrefixes"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.resumes.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.prefix}/*", "${var.prefix}/"]
    }
  }
  statement {
    sid       = "ReadJobDocuments"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.resumes.arn}/${var.prefix}/*"]
  }
  statement {
    sid       = "ReadArtifacts"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts[0].arn, "${aws_s3_bucket.artifacts[0].arn}/*"]
  }
}

resource "aws_iam_role_policy" "api_instance" {
  count  = local.api_count
  name   = "rescan-api-instance"
  role   = aws_iam_role.api[0].id
  policy = data.aws_iam_policy_document.api_instance[0].json
}

resource "aws_iam_role_policy_attachment" "api_ssm" {
  count      = local.api_count
  role       = aws_iam_role.api[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "api" {
  count = local.api_count
  name  = "rescan-api-instance"
  role  = aws_iam_role.api[0].name
}

# --------------------------------------------------------------------------
# Network: default VPC, port 8080 open to CloudFront only, no SSH
# --------------------------------------------------------------------------

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "api" {
  count       = local.api_count
  name        = "rescan-api"
  description = "Rescan API: 8080 from CloudFront origin-facing ranges only"
  vpc_id      = data.aws_vpc.default.id
  tags        = var.tags

  ingress {
    description     = "CloudFront origin fetches"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    prefix_list_ids = [data.aws_ec2_managed_prefix_list.cloudfront.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --------------------------------------------------------------------------
# Instance
# --------------------------------------------------------------------------

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_instance" "api" {
  count                       = local.api_count
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = var.instance_type
  subnet_id                   = sort(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids      = [aws_security_group.api[0].id]
  iam_instance_profile        = aws_iam_instance_profile.api[0].name
  associate_public_ip_address = true

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required"
  }

  # The AMI lookup tracks the latest AL2023 release; an AMI change would
  # replace the instance and lose its job store. Rebuild deliberately instead.
  lifecycle {
    ignore_changes = [ami, user_data]
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    artifacts_bucket = aws_s3_bucket.artifacts[0].bucket
    artifact_key     = local.artifact_key
    region           = var.region
    tika_version     = local.tika_version
    env_file         = join("\n", [for k, v in local.env_file_lines : "${k}=${v}"])
  })
  user_data_replace_on_change = true

  tags = merge(var.tags, { Name = "rescan-api" })
}

# --------------------------------------------------------------------------
# CloudFront: HTTPS on a cloudfront.net domain, no caching, all methods
# --------------------------------------------------------------------------

resource "aws_cloudfront_distribution" "api" {
  count   = local.api_count
  enabled = true
  comment = "Rescan API"
  tags    = var.tags

  origin {
    domain_name = aws_instance.api[0].public_dns
    origin_id   = "rescan-api"
    custom_origin_config {
      http_port                = 8080
      https_port               = 443
      origin_protocol_policy   = "http-only"
      origin_ssl_protocols     = ["TLSv1.2"]
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id       = "rescan-api"
    viewer_protocol_policy = "https-only"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # AWS managed policies: CachingDisabled, AllViewerExceptHostHeader.
    cache_policy_id          = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
    origin_request_policy_id = "b689b0a8-53d0-40ab-baf2-68738e2966ac"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

output "api" {
  value = var.deploy_api ? {
    url         = "https://${aws_cloudfront_distribution.api[0].domain_name}"
    instance_id = aws_instance.api[0].id
    public_dns  = aws_instance.api[0].public_dns
    artifacts   = aws_s3_bucket.artifacts[0].bucket
    deploy      = "./scripts/deploy_aws.sh"
    logs        = "aws ssm start-session --target ${aws_instance.api[0].id} --region ${var.region}  # then: journalctl -u rescan -f"
  } : null
}

output "api_key" {
  sensitive = true
  value     = random_password.api_key.result
}

output "api_env_file" {
  description = "Contents of /etc/rescan/env on the instance; scripts/deploy_aws.sh pushes it."
  sensitive   = true
  value       = join("\n", [for k, v in local.env_file_lines : "${k}=${v}"])
}

output "frontend_env" {
  description = "For the frontend's environment: the API base URL and its own key."
  sensitive   = true
  value = var.deploy_api ? join("\n", [
    "NEXT_PUBLIC_RESCAN_API_URL=https://${aws_cloudfront_distribution.api[0].domain_name}",
    "NEXT_PUBLIC_RESCAN_API_KEY=${random_password.frontend_key.result}",
  ]) : ""
}

output "mcp_env" {
  description = "Environment for running the MCP server against the deployed API."
  sensitive   = true
  value = var.deploy_api ? join("\n", [
    "RESCAN_MCP_REMOTE_URL=https://${aws_cloudfront_distribution.api[0].domain_name}",
    "RESCAN_MCP_REMOTE_KEY=${random_password.api_key.result}",
  ]) : ""
}
