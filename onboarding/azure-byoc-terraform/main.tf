terraform {
  required_version = ">= 1.3.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 3.90, < 5.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
  # El provider registra automáticamente los Resource Providers que sus
  # recursos necesitan (incluido Microsoft.ManagedServices, requerido por
  # azurerm_lighthouse_definition). Por eso NO se declara aquí un recurso
  # azurerm_resource_provider_registration explícito: Terraform lo gestiona
  # solo, y declararlo a mano provoca un conflicto ("automatically registered
  # by Terraform").
}

# -----------------------------------------------------------------------------
# Azure Lighthouse: el equivalente de AssumeRole+ExternalId (AWS) para Azure.
# A diferencia de AWS -donde Sooniverse asume un ROL en la cuenta del
# cliente con credenciales propias de su cuenta-, aquí el cliente DELEGA su
# propia suscripción a la identidad de Sooniverse (un Service Principal
# 'sooniverse-operator' que vive en el tenant/directorio de Sooniverse, sin
# ninguna suscripción ni tarjeta de crédito propia). Sooniverse nunca ve ni
# necesita credenciales del cliente: sigue usando SU PROPIO login (dentro de
# SU tenant), y la suscripción del cliente aparece ahí listada como delegada
# -exactamente como AWS Organizations muestra las cuentas asumibles.
# -----------------------------------------------------------------------------

resource "azurerm_lighthouse_definition" "sooniverse" {
  name               = "Sooniverse - Despliegue y gestión de infraestructura de IA"
  description        = "Delegación BYOC: permite a Sooniverse aprovisionar y operar la infraestructura de inferencia (VNet/NSG/VMs GPU) dentro de esta suscripción, sin acceso a facturación ni a otras suscripciones."
  managing_tenant_id = var.sooniverse_tenant_id
  scope              = "/subscriptions/${var.subscription_id}"

  authorization {
    principal_id           = var.sooniverse_principal_id
    role_definition_id     = local.role_contributor
    principal_display_name = var.sooniverse_principal_display_name
  }

  # Permite que Sooniverse retire la delegación por sí mismo al dar de baja
  # a un cliente (sin depender de que el cliente recuerde hacerlo) -el
  # cliente conserva SIEMPRE, además, la revocación unilateral desde el
  # Portal o con 'terraform destroy' (ver MANUAL_ONBOARDING_CLIENTE_AZURE.md).
  authorization {
    principal_id           = var.sooniverse_principal_id
    role_definition_id     = local.role_managed_services_registration_assignment_delete
    principal_display_name = var.sooniverse_principal_display_name
  }
}

resource "azurerm_lighthouse_assignment" "sooniverse" {
  scope                    = "/subscriptions/${var.subscription_id}"
  lighthouse_definition_id = azurerm_lighthouse_definition.sooniverse.id
}

locals {
  # Contributor: rol integrado de Azure (mismo GUID en todos los tenants).
  # Suficiente para todo lo que crea/gestiona AzureNetworkManager y SkyPilot:
  # no incluye 'Microsoft.Authorization/roleAssignments/write' (por diseño,
  # ver azure_network.py::ensure_remote_identity -las VM usan una identidad
  # administrada propia, 'remote_identity', para evitar exigir ESE permiso
  # de más alto privilegio), ni acceso a facturación ni a IAM del tenant.
  role_contributor = "b24988ac-6180-42a0-ab88-20f7382dd24c"

  # Permite a Sooniverse eliminar ESTA MISMA delegación (Managed Services
  # Registration Assignment Delete Role) -así puede revocarse su propio
  # acceso al dar de baja al cliente, sin depender de que el cliente lo haga.
  role_managed_services_registration_assignment_delete = "91c1777a-f3dc-4fae-b103-61d183457e46"
}
