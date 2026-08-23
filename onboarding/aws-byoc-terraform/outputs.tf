output "role_arn" {
  description = "ARN del rol creado que el cliente debe entregar a Sooniverse"
  value       = aws_iam_role.sooniverse_role.arn
}

output "external_id_used" {
  description = "External ID configurado para la asunción de rol"
  value       = var.external_id
  sensitive   = true
}
