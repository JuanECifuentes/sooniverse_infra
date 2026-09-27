# Manual de instalación — Acceso BYOC para Sooniverse (Azure)

**Para quién es este manual:** cualquier persona con acceso de **Propietario
(Owner)** a la suscripción de Azure de su empresa, **sin necesidad de
conocimientos técnicos de programación ni de la línea de comandos**. Se
explica cada paso con el detalle necesario para copiar y pegar.

**Tiempo estimado:** 10–15 minutos.

**Costo:** $0. Esto solo crea una "delegación de administración" dentro de
su suscripción de Azure. No se lanza ningún servidor, no hay ningún cargo
asociado a este proceso.

---

## 1. ¿Qué estamos haciendo y por qué?

Sooniverse necesita poder crear y administrar la infraestructura de IA
(servidores, redes, GPUs) **dentro de la suscripción de Azure de su propia
empresa** — así los datos, los modelos y el cómputo nunca salen de su nube.
Esto se llama modo **BYOC** ("Bring Your Own Cloud" — traiga su propia nube).

Azure ofrece para esto un mecanismo llamado **Azure Lighthouse**, pensado
justamente para que un proveedor externo (Sooniverse) administre recursos
dentro de la suscripción de un cliente **sin que el cliente tenga que
entregar ninguna contraseña ni clave**, y sin que Sooniverse tenga que crear
ninguna cuenta ni usuario nuevo en la suscripción del cliente.

En términos simples, usted va a **delegar** un permiso específico ("Rol de
colaborador"/Contributor) de su suscripción hacia la identidad de Sooniverse
que:

- ✅ Solo Sooniverse puede usar (es una identidad propia de Sooniverse, que
  usted autoriza explícitamente por su identificador único).
- ✅ Nunca expone ninguna clave de acceso ni contraseña de su empresa.
- ✅ Queda completamente registrado: cada acción que Sooniverse haga en su
  suscripción aparece en el historial de Azure (**Activity Log**).
- ✅ **No incluye acceso a facturación**, ni a crear/eliminar usuarios, ni a
  otras suscripciones de su empresa.
- ✅ Usted puede eliminarla cuando quiera, sin tener que avisarnos ni
  pedirnos permiso — su suscripción, sus reglas.

Este manual le muestra cómo crear esa delegación usando una herramienta
llamada **Terraform**, que automatiza todo el proceso en un solo comando —
usted no necesita escribir ni entender código, solo copiar y pegar los
comandos tal como aparecen aquí.

---

## 2. Antes de empezar

Sooniverse le habrá entregado **tres datos** por un canal seguro (correo,
mensaje directo, etc.). Ninguno de los tres es secreto -son identificadores,
no contraseñas- pero guárdelos a mano, los va a necesitar en el Paso 4.3:

| Dato | Ejemplo | Para qué sirve |
|---|---|---|
| **Tenant ID de Sooniverse** | `11111111-1111-1111-1111-111111111111` | Identifica el directorio (tenant) de Azure donde vive la identidad de Sooniverse |
| **Principal ID de Sooniverse** | `22222222-2222-2222-2222-222222222222` | Identifica la identidad exacta de Sooniverse autorizada a administrar su suscripción |
| **Nombre visible** (opcional) | `Sooniverse Operator` | Cómo va a aparecer Sooniverse listado en su Portal |

También necesita:

- Iniciar sesión en el [Portal de Azure](https://portal.azure.com/) con una
  cuenta que tenga el rol **Owner (Propietario)** sobre la suscripción que
  quiere delegar (o pedirle a su equipo de IT que realice estos pasos con
  esas credenciales).
- Saber el **ID de su suscripción** (Portal → Suscripciones → copiar el
  "ID de suscripción" de la que tiene la cuota de GPU que va a usar
  Sooniverse).

---

## 3. Abrir una terminal dentro de Azure (sin instalar nada en su computadora)

Azure ofrece una terminal en la nube llamada **Cloud Shell**, que ya viene
con Terraform preinstalado — no hay que instalar ni configurar nada.

1. Inicie sesión en el [Portal de Azure](https://portal.azure.com/).
2. Haga clic en el ícono de terminal (`>_`) en la barra superior.
3. Si es la primera vez, elija **Bash** (no PowerShell) cuando se lo
   pregunte.
4. Espere unos segundos a que la terminal termine de iniciar.

Va a ver una pantalla oscura con texto — es normal, es su terminal.

---

## 4. Copiar los archivos y ejecutar el proceso

### 4.1. Confirmar que Terraform ya está disponible

```bash
terraform -version
```

Si ve algo como `Terraform v1.x.x`, ya está listo (Cloud Shell lo trae
preinstalado, a diferencia de la CloudShell de AWS).

### 4.2. Subir la carpeta que Sooniverse le envió

Sooniverse le entregó (o le indicó dónde descargar) una carpeta llamada
`azure-byoc-terraform`. Súbala a Cloud Shell:

1. En Cloud Shell, haga clic en el ícono de **Cargar/descargar archivos**
   (una flecha hacia arriba) en la barra superior de la terminal.
2. Elija **Upload** ("Cargar") para cada archivo (`main.tf`, `variables.tf`,
   `outputs.tf`, `terraform.tfvars.example`), o suba un `.zip` si se lo
   dieron comprimido y descomprímalo con:
   ```bash
   mkdir -p azure-byoc-terraform && cd azure-byoc-terraform
   unzip -o -q ../azure-byoc-terraform.zip
   ```
   (los archivos individuales se suben a `~/clouddrive` o al home de Cloud
   Shell; muévalos a una carpeta con `mkdir azure-byoc-terraform && mv *.tf *.example azure-byoc-terraform/` si hace falta, y luego `cd azure-byoc-terraform`).

### 4.3. Configurar los datos que le dio Sooniverse

Cree el archivo de configuración con este comando — **reemplace los valores
de ejemplo** por los suyos y los que Sooniverse le entregó:

```bash
cat > terraform.tfvars << 'EOF'
subscription_id         = "REEMPLACE_CON_EL_ID_DE_SU_SUSCRIPCION"
sooniverse_tenant_id    = "REEMPLACE_CON_EL_TENANT_ID_QUE_LE_DIO_SOONIVERSE"
sooniverse_principal_id = "REEMPLACE_CON_EL_PRINCIPAL_ID_QUE_LE_DIO_SOONIVERSE"
EOF
```

### 4.4. Ejecutar la instalación

```bash
terraform init
```
(descarga los componentes necesarios; toma unos segundos)

```bash
terraform plan
```
(le muestra **exactamente** qué se va a crear, antes de crear nada)

```bash
terraform apply
```
Terraform le va a preguntar `Do you want to perform these actions?` — escriba
`yes` y presione Enter para confirmar.

En unos segundos va a ver un mensaje `Apply complete!` y, debajo, algo como:

```
Outputs:

lighthouse_assignment_id = "/subscriptions/.../Microsoft.ManagedServices/..."
subscription_id = "abcdef12-3456-7890-abcd-ef1234567890"
tenant_id = "..."
```

---

## 5. Enviar el resultado a Sooniverse

Copie el valor de `subscription_id` que apareció y envíelo a su contacto en
Sooniverse por el mismo canal donde le compartieron los datos del Paso 2.

**Eso es todo de su lado.** Sooniverse usará ese dato para conectarse a su
suscripción de forma segura y comenzar a desplegar su infraestructura de IA.

---

## 6. ¿Cómo reviso o revoco el acceso más adelante?

Usted tiene control total en cualquier momento, sin necesidad de avisarle a
Sooniverse:

- **Ver la delegación:** Portal de Azure → busque su suscripción →
  **Proveedores de servicios** (Service providers) en el menú lateral. Ahí
  aparece "Sooniverse" con el rol delegado (Colaborador/Contributor) y la
  fecha de creación.
- **Ver qué hizo Sooniverse en su suscripción:** **Monitor → Activity log**,
  filtrando por la identidad de Sooniverse.
- **Revocar el acceso por completo:** vuelva a Cloud Shell, entre a la
  carpeta `azure-byoc-terraform` y ejecute:
  ```bash
  terraform destroy
  ```
  Escriba `yes` cuando se lo pida. La delegación se elimina al instante y
  Sooniverse pierde el acceso a su suscripción de inmediato. También puede
  hacerlo sin Terraform, directamente desde **Proveedores de servicios →
  Sooniverse → Eliminar**.

---

## 7. Preguntas frecuentes

**¿Sooniverse puede ver mi tarjeta de crédito o cambiar mi método de pago?**
No. La delegación no incluye ningún acceso a facturación (Cost Management +
Billing).

**¿Sooniverse puede crear otros usuarios, roles o delegaciones en mi
suscripción?**
No. El rol delegado (Contributor) alcanza para crear la infraestructura de
cómputo/red necesaria (VNets, NSGs, Máquinas Virtuales con GPU) -no incluye
`Microsoft.Authorization/roleAssignments/write` (gestión de permisos de
Azure AD/Entra ID).

**¿Qué pasa si mi empresa usa más de una suscripción de Azure (por ejemplo,
una para desarrollo y otra para producción)?**
Repita este mismo proceso en cada suscripción donde quiera que Sooniverse
despliegue infraestructura.

**¿Tengo que repetir este proceso cada cierto tiempo?**
No. Se hace una sola vez. La delegación queda activa indefinidamente hasta
que usted decida revocarla (Sección 6).
