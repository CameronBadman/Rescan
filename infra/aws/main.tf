# The bucket the pipeline pulls resumes from, and an identity scoped to it.
#
# Resumes are personal information. The bucket is private, encrypted at rest,
# versioned (an accidental overwrite of a candidate's document is recoverable),
# and expires objects after the retention period. The Rescan service gets its
# own IAM user with read access to this bucket and nothing else — the API must
# never run on the account's root credentials.

data "aws_caller_identity" "current" {}

locals {
  bucket_name = coalesce(var.bucket_name, "rescan-resumes-${data.aws_caller_identity.current.account_id}")
}

resource "aws_s3_bucket" "resumes" {
  bucket = local.bucket_name
  tags   = var.tags
}

resource "aws_s3_bucket_public_access_block" "resumes" {
  bucket                  = aws_s3_bucket.resumes.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "resumes" {
  bucket = aws_s3_bucket.resumes.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "resumes" {
  bucket = aws_s3_bucket.resumes.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "resumes" {
  bucket = aws_s3_bucket.resumes.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "resumes" {
  count  = var.retention_days > 0 ? 1 : 0
  bucket = aws_s3_bucket.resumes.id

  rule {
    id     = "expire-job-documents"
    status = "Enabled"
    filter {
      prefix = "${var.prefix}/"
    }
    expiration {
      days = var.retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}

# Deny every request that is not over TLS.
data "aws_iam_policy_document" "bucket" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = [
      aws_s3_bucket.resumes.arn,
      "${aws_s3_bucket.resumes.arn}/*",
    ]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "resumes" {
  bucket = aws_s3_bucket.resumes.id
  policy = data.aws_iam_policy_document.bucket.json

  depends_on = [aws_s3_bucket_public_access_block.resumes]
}

# --------------------------------------------------------------------------
# Identity for the Rescan service
# --------------------------------------------------------------------------

resource "aws_iam_user" "rescan" {
  name = "rescan-service"
  tags = var.tags
}

# Read the job prefixes; nothing else in the account. Uploads come from
# whatever collects applications, under its own identity.
data "aws_iam_policy_document" "rescan" {
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
}

resource "aws_iam_user_policy" "rescan" {
  name   = "rescan-read-resumes"
  user   = aws_iam_user.rescan.name
  policy = data.aws_iam_policy_document.rescan.json
}

resource "aws_iam_access_key" "rescan" {
  count = var.create_access_key ? 1 : 0
  user  = aws_iam_user.rescan.name
}
