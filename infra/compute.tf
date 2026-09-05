locals {
  common_env = {
    DATABASE_URL        = "jdbc:postgresql://${aws_db_instance.main.endpoint}/rescan?sslmode=require"
    DATABASE_SECRET_ARN = aws_db_instance.main.master_user_secret[0].secret_arn
    S3_BUCKET           = aws_s3_bucket.documents.id
    AWS_REGION          = var.region
  }
  ecs_trust = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_ecr_repository" "images" {
  for_each             = toset(["api", "worker"])
  name                 = "${var.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_ecs_cluster" "main" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}
resource "aws_cloudwatch_log_group" "services" {
  for_each          = toset(["api", "worker", "controller"])
  name              = each.key == "controller" ? "/aws/lambda/${var.name}-controller" : "/rescan/${var.name}/${each.key}"
  retention_in_days = 30
}
resource "aws_iam_role" "execution" {
  name_prefix        = "${var.name}-execution-"
  assume_role_policy = local.ecs_trust
}
resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "execution_secrets" {
  role   = aws_iam_role.execution.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_secretsmanager_secret.redis.arn] }] })
}
resource "aws_iam_role" "tasks" {
  for_each           = toset(["api", "worker"])
  name_prefix        = "${var.name}-${each.key}-"
  assume_role_policy = local.ecs_trust
}
resource "aws_iam_role_policy" "tasks" {
  for_each = aws_iam_role.tasks
  role     = each.value.id
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_db_instance.main.master_user_secret[0].secret_arn] },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.documents.arn}/jobs/*"] },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.documents.arn}/jobs/*/${each.key == "api" ? "originals" : "results"}/*"] }
  ], each.key == "worker" ? [{ Effect = "Allow", Action = ["ecs:UpdateTaskProtection"], Resource = ["arn:aws:ecs:${var.region}:${data.aws_caller_identity.current.account_id}:task/${aws_ecs_cluster.main.name}/*"] }] : []) })
}
resource "aws_ecs_task_definition" "services" {
  for_each                 = toset(["api", "worker"])
  family                   = "${var.name}-${each.key}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = each.key == "worker" ? "4096" : "1024"
  memory                   = each.key == "worker" ? "16384" : "2048"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.tasks[each.key].arn
  container_definitions = jsonencode([{
    name            = each.key
    image           = each.key == "api" ? coalesce(var.api_image, "${aws_ecr_repository.images["api"].repository_url}:bootstrap") : coalesce(var.worker_image, "${aws_ecr_repository.images["worker"].repository_url}:bootstrap")
    essential       = true
    stopTimeout     = 120
    linuxParameters = { initProcessEnabled = true }
    portMappings    = each.key == "api" ? [{ containerPort = 8080, protocol = "tcp" }] : []
    environment = [for k, v in merge(local.common_env, each.key == "api" ? {
      AUTH_ISSUER     = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.main.id}"
      AUTH_CLIENT_ID  = aws_cognito_user_pool_client.frontend.id
      FRONTEND_ORIGIN = var.frontend_origin
    } : {}) : { name = k, value = v }]
    secrets          = [{ name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis.arn }]
    logConfiguration = { logDriver = "awslogs", options = { awslogs-group = aws_cloudwatch_log_group.services[each.key].name, awslogs-region = var.region, awslogs-stream-prefix = each.key } }
  }])
}
resource "aws_lb" "api" {
  name               = "${var.name}-api"
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]
  idle_timeout       = 300
}
resource "aws_lb_target_group" "api" {
  name        = "${var.name}-api"
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id
  health_check { path = "/actuator/health" }
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
resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.api.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
resource "aws_route53_record" "api" {
  zone_id = var.route53_zone_id
  name    = var.api_domain
  type    = "A"
  alias {
    name                   = aws_lb.api.dns_name
    zone_id                = aws_lb.api.zone_id
    evaluate_target_health = true
  }
}
resource "aws_ecs_service" "api" {
  name                              = "${var.name}-api"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.services["api"].arn
  desired_count                     = 1
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 120
  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.services.id]
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8080
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  depends_on = [aws_lb_listener.https, aws_iam_role_policy.tasks, aws_iam_role_policy.execution_secrets]
}
resource "aws_ecs_service" "worker" {
  name                               = "${var.name}-worker"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.services["worker"].arn
  desired_count                      = 0
  launch_type                        = "FARGATE"
  deployment_maximum_percent         = 100
  deployment_minimum_healthy_percent = 0
  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.services.id]
  }
  lifecycle { ignore_changes = [desired_count] }
  depends_on = [aws_iam_role_policy.tasks, aws_iam_role_policy.execution_secrets]
}
resource "aws_iam_role" "controller" {
  name_prefix        = "${var.name}-controller-"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }] })
}
resource "aws_iam_role_policy_attachment" "controller" {
  role       = aws_iam_role.controller.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}
resource "aws_iam_role_policy" "controller" {
  role = aws_iam_role.controller.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = [aws_db_instance.main.master_user_secret[0].secret_arn, aws_secretsmanager_secret.redis.arn] },
    { Effect = "Allow", Action = ["s3:ListBucketVersions"], Resource = [aws_s3_bucket.documents.arn] },
    { Effect = "Allow", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = ["${aws_s3_bucket.documents.arn}/jobs/*"] },
    { Effect = "Allow", Action = ["ecs:UpdateService"], Resource = [aws_ecs_service.worker.id] }
  ] })
}
resource "aws_lambda_function" "controller" {
  function_name                  = "${var.name}-controller"
  role                           = aws_iam_role.controller.arn
  runtime                        = "java21"
  handler                        = "dev.rescan.controller.Controller::handleRequest"
  filename                       = var.controller_jar
  source_code_hash               = filebase64sha256(var.controller_jar)
  memory_size                    = 1024
  timeout                        = 60
  reserved_concurrent_executions = 1
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.services.id]
  }
  environment { variables = merge({ for k, v in local.common_env : k => v if k != "AWS_REGION" }, {
    REDIS_SECRET_ARN   = aws_secretsmanager_secret.redis.arn
    ECS_CLUSTER        = aws_ecs_cluster.main.name
    ECS_WORKER_SERVICE = aws_ecs_service.worker.name
  }) }
  depends_on = [aws_iam_role_policy_attachment.controller, aws_iam_role_policy.controller, aws_cloudwatch_log_group.services]
}
resource "aws_cloudwatch_event_rule" "controller" {
  name                = "${var.name}-controller"
  schedule_expression = "rate(1 minute)"
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
resource "aws_cloudwatch_metric_alarm" "controller_errors" {
  alarm_name          = "${var.name}-controller-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.controller.function_name }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}
