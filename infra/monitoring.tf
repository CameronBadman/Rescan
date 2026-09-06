resource "aws_cloudwatch_metric_alarm" "controller_missing" {
  count               = var.controller_enabled ? 1 : 0
  alarm_name          = "${var.name}-controller-missing"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.controller.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${var.name}-api-5xx"
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  dimensions          = { ApiId = aws_apigatewayv2_api.api.id }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}
resource "aws_cloudwatch_metric_alarm" "processing" {
  for_each            = { QueueAgeSeconds = 300, FailedDocuments = 1 }
  alarm_name          = "${var.name}-${each.key}"
  namespace           = "Rescan"
  metric_name         = each.key
  dimensions          = { Cluster = aws_ecs_cluster.main.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = each.value
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
}
