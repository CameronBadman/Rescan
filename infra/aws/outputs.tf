output "bucket" {
  value = {
    name   = aws_s3_bucket.resumes.bucket
    arn    = aws_s3_bucket.resumes.arn
    region = var.region
    layout = "s3://${aws_s3_bucket.resumes.bucket}/${var.prefix}/<jobId>/<resume files>"
  }
}

output "iam_user" {
  value = aws_iam_user.rescan.arn
}

output "rescan_env" {
  description = "Lines for Rescan's .env. Contains the service user's secret key."
  sensitive   = true
  value = join("\n", compact([
    "RESCAN_OBJECT_STORE=s3",
    "RESCAN_S3_BUCKET=${aws_s3_bucket.resumes.bucket}",
    "RESCAN_S3_REGION=${var.region}",
    "RESCAN_S3_PREFIX=${var.prefix}",
    var.create_access_key ? "RESCAN_S3_ACCESS_KEY_ID=${aws_iam_access_key.rescan[0].id}" : "",
    var.create_access_key ? "RESCAN_S3_SECRET_ACCESS_KEY=${aws_iam_access_key.rescan[0].secret}" : "",
  ]))
}

output "upload_example" {
  value = "aws s3 cp ./resumes/ s3://${aws_s3_bucket.resumes.bucket}/${var.prefix}/<jobId>/ --recursive"
}
