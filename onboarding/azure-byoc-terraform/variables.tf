variable "subscription_id" {
  description = "ID de la suscripción de Azure del cliente (la que tiene la cuota de GPU) que se delega a Sooniverse."
  type        = string
}

variable "sooniverse_tenant_id" {
  description = "Id. de inquilino (tenant) de Azure de Sooniverse -provisto por Sooniverse, NO es un dato secreto."
  type        = string
}

variable "sooniverse_principal_id" {
  description = "Id. de OBJETO (Object ID) del Service Principal 'sooniverse-operator' en el tenant de Sooniverse. IMPORTANTE: es el Object ID de 'Aplicaciones empresariales' (Enterprise Applications), NO el de 'Registros de aplicaciones' (App registrations) -son objetos distintos en Entra ID y la delegación falla en silencio si se usa el equivocado. Provisto por Sooniverse, no es secreto."
  type        = string
}

variable "sooniverse_principal_display_name" {
  description = "Nombre visible del Service Principal de Sooniverse en el portal del cliente (Proveedores de servicios)."
  type        = string
  default     = "Sooniverse Operator"
}
