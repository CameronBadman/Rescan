locals {
  common_env = {
    TURSO_DATABASE_URL = var.turso_database_url
    TURSO_SECRET_ARN   = var.turso_secret_arn
    S3_BUCKET          = aws_s3_bucket.documents.id
  }
  lambda_trust = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }] })
  ecs_trust    = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_ecs_cluster" "main" { name = var.name }
resource "aws_cloudwatch_log_group" "services" {
  for_each          = toset(["api", "worker", "controller", "verifier"])
  name              = each.key == "worker" ? "/rescan/${var.name}/worker" : "/aws/lambda/${var.name}-${each.key}"
  retention_in_days = 7
}
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = local.ecs_trust
}
resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role" "tasks" {
  for_each           = toset(["api", "worker", "controller", "verifier"])
  name               = "${var.name}-${each.key}"
  assume_role_policy = each.key == "worker" ? local.ecs_trust : local.lambda_trust
}
resource "aws_iam_role_policy" "tasks" {
  for_each = aws_iam_role.tasks
  role     = each.value.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = concat([var.turso_secret_arn], contains(["worker", "controller"], each.key) ? [var.redis_secret_arn] : []) },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = ["${aws_cloudwatch_log_group.services[each.key].arn}:*"] }
    ], each.key == "api" ? [
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.documents.arn}/jobs/*/originals/*"] },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${aws_s3_bucket.documents.arn}/jobs/*/results/*"] }
    ] : [], each.key == "worker" ? [
    { Effect = "Allow", Action = ["s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.documents.arn}/jobs/*/originals/*"] },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.documents.arn}/jobs/*/results/*"] },
    { Effect = "Allow", Action = ["ecs:UpdateTaskProtection"], Resource = ["arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task/${aws_ecs_cluster.main.name}/*"] }
    ] : [], each.key == "verifier" ? [
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = [aws_s3_bucket.documents.arn] },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${aws_s3_bucket.documents.arn}/jobs/*/originals/*"] }
    ] : [], each.key == "controller" ? [
    { Effect = "Allow", Action = ["s3:ListBucketVersions"], Resource = [aws_s3_bucket.documents.arn] },
    { Effect = "Allow", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = ["${aws_s3_bucket.documents.arn}/jobs/*"] },
    { Effect = "Allow", Action = ["ecs:UpdateService"], Resource = ["arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:service/${var.name}/${var.name}-worker"] },
    { Effect = "Allow", Action = ["lambda:InvokeFunction"], Resource = ["arn:aws:lambda:${var.region}:${data.aws_caller_identity.current.account_id}:function:${var.name}-verifier"] }
  ] : []) })
}
resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name}-worker"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "1024"
  memory                   = "4096"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.tasks["worker"].arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name             = "worker", image = var.worker_image, essential = true, stopTimeout = 120,
    linuxParameters  = { initProcessEnabled = true },
    healthCheck = { command = ["CMD-SHELL", "test -f /tmp/rescan-worker-ready"], interval = 30, timeout = 5, retries = 3, startPeriod = 180 },
    environment      = [for k, v in merge(local.common_env, { AWS_REGION = var.region, REDIS_SECRET_ARN = var.redis_secret_arn, OCR_THREADS = "1", OCR_PRELOAD = "true" }) : { name = k, value = v }],
    logConfiguration = { logDriver = "awslogs", options = { awslogs-group = aws_cloudwatch_log_group.services["worker"].name, awslogs-region = var.region, awslogs-stream-prefix = "worker" } }
  }])
}
resource "aws_ecs_service" "worker" {
  name                               = "${var.name}-worker"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.worker.arn
  desired_count                      = 0
  launch_type                        = "FARGATE"
  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0
  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.services.id]
    assign_public_ip = true
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  lifecycle { ignore_changes = [desired_count] }
  depends_on = [aws_iam_role_policy.tasks, aws_iam_role_policy_attachment.execution, aws_route_table_association.public]
}
resource "aws_lambda_function" "api" {
  function_name                  = "${var.name}-api"
  role                           = aws_iam_role.tasks["api"].arn
  package_type                   = "Image"
  image_uri                      = var.api_image
  architectures                  = ["x86_64"]
  memory_size                    = 2048
  timeout                        = 28
  reserved_concurrent_executions = 5
  publish                        = true
  environment { variables = merge(local.common_env, {
    AUTH_ISSUER     = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}",
    AUTH_CLIENT_ID  = aws_cognito_user_pool_client.frontend.id,
    FRONTEND_ORIGIN = var.frontend_origin
  }) }
  depends_on = [aws_iam_role_policy.tasks]
}
resource "aws_lambda_function" "controller" {
  function_name                  = "${var.name}-controller"
  role                           = aws_iam_role.tasks["controller"].arn
  runtime                        = "java21"
  handler                        = "dev.rescan.controller.Controller::handleRequest"
  filename                       = var.controller_jar
  source_code_hash               = filebase64sha256(var.controller_jar)
  memory_size                    = 512
  timeout                        = 60
  reserved_concurrent_executions = 1
  publish                        = true
  environment { variables = merge(local.common_env, {
    REDIS_SECRET_ARN   = var.redis_secret_arn,
    ECS_CLUSTER        = aws_ecs_cluster.main.name,
    ECS_WORKER_SERVICE = aws_ecs_service.worker.name,
    VERIFIER_FUNCTION  = aws_lambda_function.verifier.function_name
  }) }
  depends_on = [aws_iam_role_policy.tasks]
}
resource "aws_lambda_function" "verifier" {
  function_name                  = "${var.name}-verifier"
  role                           = aws_iam_role.tasks["verifier"].arn
  runtime                        = "java21"
  handler                        = "dev.rescan.controller.Verifier::handleRequest"
  filename                       = var.controller_jar
  source_code_hash               = filebase64sha256(var.controller_jar)
  memory_size                    = 1024
  timeout                        = 60
  reserved_concurrent_executions = 2
  publish                        = true
  environment { variables = local.common_env }
  depends_on = [aws_iam_role_policy.tasks]
}
resource "aws_apigatewayv2_api" "api" {
  name          = "${var.name}-api"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = [var.frontend_origin]
    allow_methods = ["GET", "POST", "DELETE", "OPTIONS"]
    allow_headers = ["Authorization", "Content-Type", "Idempotency-Key"]
  }
}
resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}
resource "aws_apigatewayv2_route" "api" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}
resource "aws_apigatewayv2_stage" "api" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}
resource "aws_lambda_permission" "api" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}
resource "aws_acm_certificate" "api" {
  domain_name       = var.api_domain
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
}
resource "aws_route53_record" "validation" {
  for_each = { for option in aws_acm_certificate.api.domain_validation_options : option.domain_name => option }
  zone_id  = var.route53_zone_id
  name     = each.value.resource_record_name
  type     = each.value.resource_record_type
  ttl      = 60
  records  = [each.value.resource_record_value]
}
resource "aws_acm_certificate_validation" "api" {
  certificate_arn         = aws_acm_certificate.api.arn
  validation_record_fqdns = [for record in aws_route53_record.validation : record.fqdn]
}

resource "aws_apigatewayv2_domain_name" "api" {
  domain_name = var.api_domain
  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.api.certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}
resource "aws_apigatewayv2_api_mapping" "api" {
  api_id      = aws_apigatewayv2_api.api.id
  domain_name = aws_apigatewayv2_domain_name.api.id
  stage       = aws_apigatewayv2_stage.api.id
}
resource "aws_route53_record" "api" {
  zone_id = var.route53_zone_id
  name    = var.api_domain
  type    = "A"
  alias {
    name                   = aws_apigatewayv2_domain_name.api.domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.api.domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}
resource "aws_cloudwatch_event_rule" "controller" {
  name                = "${var.name}-controller"
  schedule_expression = "rate(1 minute)"
  state               = var.controller_enabled ? "ENABLED" : "DISABLED"
  lifecycle {
    precondition {
      condition     = !var.controller_enabled || var.worker_profile_verified
      error_message = "Complete the small-worker OCR benchmark before activating processing."
    }
  }
}
resource "aws_cloudwatch_event_target" "controller" {
  rule = aws_cloudwatch_event_rule.controller.name
  arn  = aws_lambda_function.controller.arn
}
resource "aws_lambda_permission" "schedule" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.controller.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.controller.arn
}
resource "aws_sns_topic" "alarms" { name = "${var.name}-alarms" }
resource "aws_sns_topic_subscription" "operations" {
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.operations_email
}
resource "aws_cloudwatch_metric_alarm" "errors" {
  for_each            = toset(["api", "controller", "verifier"])
  alarm_name          = "${var.name}-${each.key}-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = "${var.name}-${each.key}" }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}
resource "aws_budgets_budget" "hackathon" {
  name         = "${var.name}-hackathon"
  budget_type  = "COST"
  limit_amount = "25"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  cost_filter {
    name   = "TagKeyValue"
    values = ["user:Project$Rescan"]
  }
  dynamic "notification" {
    for_each = [10, 25]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "ABSOLUTE_VALUE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.operations_email]
    }
  }
}
