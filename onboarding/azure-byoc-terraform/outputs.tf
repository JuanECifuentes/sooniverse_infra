output "subscription_id" {
  description = "La misma suscripción delegada (para confirmar/copiar de vuelta a Sooniverse)."
  value       = var.subscription_id
}

output "tenant_id" {
  description = "Id. de inquilino (tenant) del CLIENTE -no confundir con 'sooniverse_tenant_id' de variables.tf. Sooniverse lo usa solo como referencia/registro, nunca para autenticarse (sigue usando su propio tenant)."
  value       = data.azurerm_client_config.current.tenant_id
}

output "lighthouse_assignment_id" {
  description = "ID de la delegación creada, visible también en el Portal -> Proveedores de servicios."
  value       = azurerm_lighthouse_assignment.sooniverse.id
}

data "azurerm_client_config" "current" {}
