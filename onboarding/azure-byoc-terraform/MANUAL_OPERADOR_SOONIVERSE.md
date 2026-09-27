# Manual del operador — Preparar la identidad de Sooniverse para BYOC Azure

**Para quién es este manual:** el operador de Sooniverse (nosotros), en la
cuenta/tenant de Azure que actúa como "Sooniverse" -en el escenario de
prueba actual, una cuenta recién creada, **sin créditos ni suscripción
propia**, porque Lighthouse no la necesita: solo aporta identidad.

Esto se hace **una sola vez** (no por cliente). Cada cliente nuevo solo
necesita ejecutar `onboarding/azure-byoc-terraform/` (ver
`MANUAL_ONBOARDING_CLIENTE_AZURE.md`) y devolver su `subscription_id`.

---

## 1. Por qué no hace falta una suscripción de Azure

Azure Lighthouse delega recursos de la suscripción del **cliente** hacia una
identidad (Service Principal) que vive en el **tenant** de Sooniverse. Un
tenant de Azure AD/Entra ID no requiere ninguna suscripción de pago para
existir ni para alojar una aplicación/Service Principal -la cuenta gratuita
que se crea al registrarse en <https://portal.azure.com/> ya trae un tenant
por defecto. La cuota de GPU, el cómputo y la factura son siempre del
cliente; Sooniverse nunca paga nada en esta cuenta.

## 2. Crear el Service Principal `sooniverse-operator`

1. Entrar a [portal.azure.com](https://portal.azure.com/) con la cuenta de
   Sooniverse → **Microsoft Entra ID → Información general** → copiar el
   **Id. de inquilino (Tenant ID)**. Este es `sooniverse_tenant_id`.
2. **Microsoft Entra ID → Registros de aplicaciones → Nuevo registro**:
   - Nombre: `sooniverse-operator`.
   - Tipos de cuenta admitidos: *Solo las cuentas de este directorio
     organizativo* (single-tenant -no hace falta multi-tenant, Lighthouse no
     lo requiere).
   - Registrar.
   - Copiar el **Id. de aplicación (cliente)** -es `AZURE_CLIENT_ID`.
3. En esa misma app: **Certificados y secretos → Nuevo secreto de
   cliente** → elegir una expiración (24 meses recomendado) → copiar el
   **Valor** inmediatamente (no se vuelve a mostrar). Es `AZURE_CLIENT_SECRET`.
4. **Microsoft Entra ID → Aplicaciones empresariales** (Enterprise
   applications, NO "Registros de aplicaciones") → buscar
   `sooniverse-operator` → copiar su **Id. de objeto**. Este es
   `sooniverse_principal_id` -el que se le entrega a CADA cliente para su
   `terraform.tfvars`.

   ⚠️ **Este es el paso donde más se falla**: "Registros de aplicaciones" y
   "Aplicaciones empresariales" son dos objetos DISTINTOS en Entra ID para la
   misma app (el "App object" y el "Service Principal object"). Lighthouse
   exige el segundo (`principal_id` en `azurerm_lighthouse_definition`
   espera un **Service Principal Object ID**); usar el primero hace que la
   delegación se cree "bien" en apariencia pero el Service Principal nunca
   pueda autenticarse contra la suscripción delegada.

## 3. Datos que se entregan a CADA cliente nuevo

Los tres campos de `onboarding/azure-byoc-terraform/terraform.tfvars.example`:

| Campo | Valor | ¿Es secreto? |
|---|---|---|
| `sooniverse_tenant_id` | del paso 2.1 | No |
| `sooniverse_principal_id` | del paso 2.4 | No |
| `sooniverse_principal_display_name` | `Sooniverse Operator` (o el que se prefiera) | No |

Ninguno de los tres es secreto -son iguales para todos los clientes y no dan
acceso por sí mismos (la delegación la crea y controla el cliente). El
**secreto** (`AZURE_CLIENT_SECRET` del paso 2.3) nunca se comparte con el
cliente: es exclusivo del `.env` del operador (paso 4).

## 4. Configurar la máquina que despliega (WSL/operador)

En `.env` del repo (`~/sooniverse_infra/.env`), las variables `AZURE_*` pasan
a identificar a **Sooniverse**, no al cliente:

```bash
AZURE_TENANT_ID=<tenant id de Sooniverse, paso 2.1>
AZURE_CLIENT_ID=<client id de Sooniverse, paso 2.2>
AZURE_CLIENT_SECRET=<secreto de Sooniverse, paso 2.3>
# AZURE_SUBSCRIPTION_ID: se deja SIN definir, o apuntando a un cliente
# 'hosted' si se sigue usando esta misma máquina para ambos modos -en BYOC
# la suscripción real la aporta cada cliente vía
# 'red_y_aislamiento.azure_subscription_id' en su config_global.yaml, nunca
# el .env (ver scripts/azure_network.py, sección "Autenticación").
```

Luego, con el `subscription_id` que devolvió el cliente (Sección 5 de
`MANUAL_ONBOARDING_CLIENTE_AZURE.md`):

```bash
az login --service-principal \
  -u "$AZURE_CLIENT_ID" -p "$AZURE_CLIENT_SECRET" --tenant "$AZURE_TENANT_ID" \
  --allow-no-subscriptions
# --allow-no-subscriptions: esta cuenta de Sooniverse no tiene ninguna
# suscripción PROPIA -sin este flag, 'az login' falla o advierte.

az account list --output table
# La suscripción del cliente debe aparecer aquí (puede tardar unos minutos
# en propagarse tras el 'terraform apply' del cliente). Si no aparece,
# revisar 'az account list --refresh' y, en el Portal, que la delegación siga
# vigente (Proveedores de servicios).
```

A partir de ahí, `clients/<cliente>/config_global.yaml` con
`cliente.modo: byoc` + `red_y_aislamiento.cloud: azure` +
`red_y_aislamiento.azure_subscription_id: <la suscripción devuelta>` ya
puede desplegarse con el flujo normal (`generate_infra.py --run`) -ver
`docs/05_MULTICLIENTE.md`.

## 5. Dar de baja a un cliente

1. `scripts/destroy_infra.py --config clients/<cliente>/config_global.yaml`
   (destruye la infraestructura igual que en modo hosted).
2. Retirar la delegación: como Sooniverse tiene el rol "Managed Services
   Registration assignment Delete Role" (ver `main.tf`), puede eliminarla
   sin depender del cliente, desde el Portal del cliente si se tiene acceso,
   o pidiéndole al cliente que corra `terraform destroy` /la borre desde
   **Proveedores de servicios**.
