# The frontend: a static Next.js export in a private bucket behind CloudFront.
# scripts/deploy_frontend.sh builds it with the API URL and the frontend key
# baked in and syncs the output here. The API's CORS allow-list includes this
# distribution's domain automatically.

resource "aws_s3_bucket" "frontend" {
  count  = local.api_count
  bucket = "rescan-frontend-${data.aws_caller_identity.current.account_id}"
  tags   = var.tags
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  count                   = local.api_count
  bucket                  = aws_s3_bucket.frontend[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_origin_access_control" "frontend" {
  count                             = local.api_count
  name                              = "rescan-frontend"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "frontend" {
  count               = local.api_count
  enabled             = true
  comment             = "Rescan frontend"
  default_root_object = "index.html"
  tags                = var.tags

  origin {
    domain_name              = aws_s3_bucket.frontend[0].bucket_regional_domain_name
    origin_id                = "rescan-frontend"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend[0].id
  }

  default_cache_behavior {
    target_origin_id       = "rescan-frontend"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    # AWS managed CachingOptimized.
    cache_policy_id = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }

  # A static export has no server; unknown paths fall back to the app shell.
  custom_error_response {
    error_code         = 403
    response_code      = 200
    response_page_path = "/index.html"
  }
  custom_error_response {
    error_code         = 404
    response_code      = 200
    response_page_path = "/index.html"
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

data "aws_iam_policy_document" "frontend_bucket" {
  count = local.api_count
  statement {
    sid       = "CloudFrontRead"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend[0].arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend[0].arn]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  count      = local.api_count
  bucket     = aws_s3_bucket.frontend[0].id
  policy     = data.aws_iam_policy_document.frontend_bucket[0].json
  depends_on = [aws_s3_bucket_public_access_block.frontend]
}

output "frontend" {
  value = var.deploy_api ? {
    url             = "https://${aws_cloudfront_distribution.frontend[0].domain_name}"
    bucket          = aws_s3_bucket.frontend[0].bucket
    distribution_id = aws_cloudfront_distribution.frontend[0].id
    deploy          = "./scripts/deploy_frontend.sh"
  } : null
}
