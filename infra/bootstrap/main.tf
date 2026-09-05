terraform {
  required_version = ">= 1.12, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.10.0" }
  }
}
provider "aws" {
  region = var.region
  default_tags { tags = { Project = "Rescan", ManagedBy = "Terraform" } }
}
variable "region" { default = "ap-southeast-2" }
variable "name" { default = "rescan" }
variable "oidc_provider_arn" {
  type        = string
  default     = null
  description = "Reuse an existing GitHub OIDC provider when present."
}
variable "subjects" {
  type        = object({ publish = string, plan = string, apply = string, demo = string })
  description = "Exact GitHub OIDC subjects for the four protected environments, including immutable IDs when enabled. No wildcards."
  validation {
    condition     = alltrue([for s in values(var.subjects) : startswith(s, "repo:") && !strcontains(s, "*")])
    error_message = "Supply exact repository/environment subjects without wildcards."
  }
}
variable "deployment_policy_arn" {
  type        = string
  description = "Administrator-reviewed provisioning policy scoped to this project's resources. Not AdministratorAccess."
  validation {
    condition     = !endswith(var.deployment_policy_arn, "/AdministratorAccess")
    error_message = "Use a project-scoped provisioning policy, not AdministratorAccess."
  }
}
data "aws_caller_identity" "current" {}
resource "aws_s3_bucket" "state" {
  bucket = "${var.name}-state-${data.aws_caller_identity.current.account_id}-${var.region}"
  lifecycle { prevent_destroy = true }
}
resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.name}-releases-${data.aws_caller_identity.current.account_id}-${var.region}"
  lifecycle { prevent_destroy = true }
}
locals { buckets = { state = aws_s3_bucket.state.id, artifacts = aws_s3_bucket.artifacts.id } }
resource "aws_s3_bucket_versioning" "private" {
  for_each = local.buckets
  bucket   = each.value
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_public_access_block" "private" {
  for_each                = local.buckets
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "private" {
  for_each = local.buckets
  bucket   = each.value
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_policy" "tls" {
  for_each = local.buckets
  bucket   = each.value
  policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Deny", Principal = "*", Action = "s3:*", Resource = ["arn:aws:s3:::${each.value}", "arn:aws:s3:::${each.value}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } }] })
}
resource "aws_ecr_repository" "images" {
  for_each             = toset(["api", "worker"])
  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_iam_openid_connect_provider" "github" {
  count          = var.oidc_provider_arn == null ? 1 : 0
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}
resource "aws_ecr_repository_policy" "lambda" {
  repository = aws_ecr_repository.images["api"].name
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "LambdaImageRetrieval", Effect = "Allow",
    Principal = { Service = "lambda.amazonaws.com" },
    Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
    Condition = { ArnLike = { "aws:SourceArn" = "arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${var.name}-api" } }
  }] })
}
resource "aws_iam_role" "github" {
  for_each           = var.subjects
  name               = "${var.name}-github-${each.key}"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = var.oidc_provider_arn == null ? aws_iam_openid_connect_provider.github[0].arn : var.oidc_provider_arn }, Condition = { StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com", "token.actions.githubusercontent.com:sub" = each.value } } }] })
}
resource "aws_iam_role_policy" "publisher" {
  role = aws_iam_role.github["publish"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"], Resource = [for r in aws_ecr_repository.images : r.arn] },
    { Effect = "Allow", Action = ["s3:PutObject", "s3:GetObject"], Resource = "${aws_s3_bucket.artifacts.arn}/releases/*" }
  ] })
}
resource "aws_iam_role_policy_attachment" "plan_read" {
  role       = aws_iam_role.github["plan"].name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}
resource "aws_iam_role_policy_attachment" "deploy" {
  role       = aws_iam_role.github["apply"].name
  policy_arn = var.deployment_policy_arn
}
resource "aws_iam_role_policy" "state" {
  for_each = toset(["plan", "apply"])
  role     = aws_iam_role.github[each.key].id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.state.arn },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.state.arn}/production/terraform.tfstate" },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], Resource = "${aws_s3_bucket.state.arn}/production/terraform.tfstate.tflock" },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.artifacts.arn}/*" }
    ], each.key == "apply" ? [
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.state.arn}/production/terraform.tfstate" },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.artifacts.arn}/installations/${var.name}/*" },
    { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = "arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${var.name}-controller" }
  ] : [], each.key == "plan" ? [{ Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.artifacts.arn}/plans/*" }] : []) })
}
resource "aws_iam_role_policy" "demo" {
  role   = aws_iam_role.github["demo"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = "arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${var.name}-controller" }] })
}
resource "aws_iam_role_policy" "pass_roles" {
  role = aws_iam_role.github["apply"].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["iam:PassRole"], Resource = [for n in ["api", "worker", "controller", "verifier", "execution"] : "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.name}-${n}"], Condition = { StringEquals = { "iam:PassedToService" = ["lambda.amazonaws.com", "ecs-tasks.amazonaws.com"] } } }
  ] })
}
output "state_bucket" { value = aws_s3_bucket.state.id }
output "artifact_bucket" { value = aws_s3_bucket.artifacts.id }
output "ecr_repositories" { value = { for k, v in aws_ecr_repository.images : k => v.repository_url } }
output "github_roles" { value = { for k, v in aws_iam_role.github : k => v.arn } }
