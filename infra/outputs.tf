output "url" {
  value       = "http://${aws_lb.main.dns_name}"
  description = "The demo endpoint. /console for the funnel, /health/ready for the probe."
}

output "ecr_repository" {
  value       = aws_ecr_repository.main.repository_url
  description = "Push target. deploy.sh reads this."
}

output "cluster" {
  value = aws_ecs_cluster.main.name
}

# deploy.sh reads these to run the migration task in the same network the
# services use. Emitted rather than looked up so the script never has to guess
# at a name Terraform already knows.
output "subnets" {
  value = aws_subnet.public[*].id
}

output "task_security_group" {
  value = aws_security_group.tasks.id
}

output "log_group" {
  value = aws_cloudwatch_log_group.main.name
}

output "secrets_to_populate" {
  description = <<-EOT
    Terraform creates these empty and the app refuses to start without them.
    Deliberate: a Fernet key or a BSP app secret invented by `random_password`
    would be a credential nobody can rotate in step with the thing that trusts
    it, and the vault key in particular makes every stored token undecryptable
    if it changes. Populate them once, by hand, before the first deploy.
  EOT
  value = {
    vault_key           = aws_secretsmanager_secret.vault_key.name
    customer_ref_secret = aws_secretsmanager_secret.customer_ref_secret.name
    whatsapp_app_secret = aws_secretsmanager_secret.whatsapp_app_secret.name
    gemini_api_key      = aws_secretsmanager_secret.gemini_api_key.name
  }
}

output "migrate_command" {
  description = "Migrations are a one-shot task, not a container entrypoint — two replicas starting together must not race the same migration."
  value = join(" ", [
    "aws ecs run-task --cluster ${aws_ecs_cluster.main.name}",
    "--launch-type FARGATE --task-definition ${aws_ecs_task_definition.api.family}",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", aws_subnet.public[*].id)}],securityGroups=[${aws_security_group.tasks.id}],assignPublicIp=ENABLED}'",
    "--overrides '{\"containerOverrides\":[{\"name\":\"api\",\"command\":[\"sh\",\"-c\",\"alembic upgrade head && python -m app.graph.checkpointer\"]}]}'",
  ])
}
