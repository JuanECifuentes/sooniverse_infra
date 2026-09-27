# Sooniverse BYOC (Azure) — Onboarding Terraform Module

Este módulo de Terraform delega, mediante **Azure Lighthouse**, el acceso
necesario para que **Sooniverse** despliegue y gestione la infraestructura de
inferencia (LLMs / GPUs) dentro de la **propia suscripción de Azure del
cliente** (**BYOC - Bring Your Own Cloud**).

Es el equivalente Azure de `onboarding/aws-byoc-terraform/`, con una
diferencia de diseño importante: en AWS, Sooniverse **asume un rol** en la
cuenta del cliente usando credenciales propias de SU cuenta AWS. En Azure no
existe ese mecanismo entre tenants distintos -en su lugar, el cliente
**delega su suscripción** al mismo Service Principal que Sooniverse ya usa
en su propio tenant (que no tiene ninguna suscripción propia ni tarjeta de
crédito asociada). Sooniverse nunca ve ni necesita credenciales del cliente.

---

## 🔒 Garantías de seguridad

1. **Sin intercambio de claves**: no se crea ni comparte ningún secreto de
   cliente. Sooniverse sigue autenticándose con su propio Service Principal,
   en su propio tenant.
2. **Alcance acotado**: el rol delegado es **Contributor** sobre la
   suscripción -permite crear/gestionar red y VMs, pero **no** incluye
   facturación, gestión de usuarios/roles de Azure AD, ni acceso a otras
   suscripciones.
3. **Auditabilidad total**: cada acción de Sooniverse queda registrada en el
   **Activity Log** de la suscripción del cliente, con la identidad del
   Service Principal de Sooniverse.
4. **Revocación inmediata**: el cliente puede retirar la delegación en
   cualquier momento, desde el Portal (**Proveedores de servicios**) o con
   `terraform destroy` -sin avisar a Sooniverse ni pedir permiso.

## Requisitos previos del cliente

- Ser **Owner** (o tener `Microsoft.Authorization/roleAssignments/write`) de
  la suscripción a delegar.
- El proveedor de recursos `Microsoft.ManagedServices` registrado en la
  suscripción (este módulo lo hace por usted si no lo está).

## Datos que entrega Sooniverse (no son secretos)

| Variable | Qué es |
|---|---|
| `sooniverse_tenant_id` | Id. de inquilino (tenant) de Azure de Sooniverse |
| `sooniverse_principal_id` | Id. de **objeto** del Service Principal `sooniverse-operator`, tomado de **Aplicaciones empresariales** (no de "Registros de aplicaciones") |

Ver `MANUAL_OPERADOR_SOONIVERSE.md` (en esta misma carpeta) para cómo
Sooniverse obtiene y mantiene estos valores.

## Uso

```bash
cp terraform.tfvars.example terraform.tfvars
# editar terraform.tfvars con los 3 valores de arriba + su subscription_id

terraform init
terraform plan
terraform apply
```

Al finalizar, Terraform muestra:

```text
Outputs:

lighthouse_assignment_id = "/subscriptions/.../providers/Microsoft.ManagedServices/..."
subscription_id = "<su suscripción>"
tenant_id = "<su tenant>"
```

Envíe `subscription_id` (y opcionalmente `tenant_id`, para registro) a su
contacto de Sooniverse -es el único dato que falta para iniciar el
despliegue: `red_y_aislamiento.azure_subscription_id` en
`clients/<su-id>/config_global.yaml`.

Ver `MANUAL_ONBOARDING_CLIENTE_AZURE.md` para la versión paso a paso sin
línea de comandos previa (Cloud Shell), pensada para quien no programa.

## Revocar el acceso

```bash
terraform destroy
```

o desde el Portal: **Suscripciones -> [su suscripción] -> Proveedores de
servicios -> Sooniverse -> Eliminar**.
