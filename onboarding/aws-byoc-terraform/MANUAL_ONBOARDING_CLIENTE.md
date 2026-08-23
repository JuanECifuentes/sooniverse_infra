# Manual de instalación — Acceso BYOC para Sooniverse

**Para quién es este manual:** cualquier persona con acceso de administrador a la
cuenta de AWS de su empresa, **sin necesidad de conocimientos técnicos de
programación ni de la línea de comandos**. Se explica cada paso con el detalle
necesario para copiar y pegar.

**Tiempo estimado:** 10–15 minutos.

**Costo:** $0. Esto solo crea un permiso (un "rol") dentro de su cuenta de AWS.
No se lanza ningún servidor, no hay ningún cargo asociado a este proceso.

---

## 1. ¿Qué estamos haciendo y por qué?

Sooniverse necesita poder crear y administrar la infraestructura de IA (servidores,
redes, GPUs) **dentro de la cuenta de AWS de su propia empresa** — así los datos,
los modelos y el cómputo nunca salen de su nube. Esto se llama modo **BYOC**
("Bring Your Own Cloud" — traiga su propia nube).

Para lograrlo sin que su empresa tenga que entregarnos ninguna contraseña ni
clave permanente, usamos el mecanismo oficial y recomendado por AWS para este
tipo de colaboración: un **rol IAM** con **acceso temporal y auditable**.

En términos simples, usted va a crear dentro de su cuenta un "permiso de
invitado" que:

- ✅ Solo Sooniverse puede usar (verificado con una contraseña secreta única,
  el **External ID**, que nosotros le entregamos).
- ✅ Nunca expone ninguna clave de acceso fija — el acceso se renueva solo
  cada hora y se puede revocar al instante.
- ✅ Queda completamente registrado: cada acción que Sooniverse haga en su
  cuenta aparece en el historial de AWS (**CloudTrail**), con su propio
  identificador.
- ✅ Usted puede eliminarlo cuando quiera, sin tener que avisarnos ni pedirnos
  permiso — su cuenta, sus reglas.

Este manual le muestra cómo crear ese permiso usando una herramienta llamada
**Terraform**, que automatiza todo el proceso en un solo comando — usted no
necesita escribir ni entender código, solo copiar y pegar los comandos tal
como aparecen aquí.

---

## 2. Antes de empezar

Sooniverse le habrá entregado dos datos por un canal seguro (correo, mensaje
directo, etc.). Guárdelos a mano, los va a necesitar en el Paso 4:

| Dato | Ejemplo | Para qué sirve |
|---|---|---|
| **ID de cuenta de Sooniverse** | `551626544576` | Identifica a Sooniverse como el único autorizado a usar el permiso |
| **External ID** | `sooniverse-<su-empresa>-xxxxxxxx` | La "contraseña secreta" que impide que cualquier otra persona use el permiso, incluso si adivinara el ID de cuenta |

⚠️ **El External ID es secreto.** Trátelo como una contraseña: no lo publique,
no lo suba a ningún repositorio público, no lo comparta por canales no
seguros.

También necesita:

- Iniciar sesión en la [consola de AWS](https://console.aws.amazon.com/) de
  su empresa con un usuario que tenga permisos de **administrador**
  (o pedirle a su equipo de IT que realice estos pasos con esas credenciales).

---

## 3. Abrir una terminal dentro de AWS (sin instalar nada en su computadora)

AWS ofrece una terminal en la nube llamada **CloudShell**, que ya viene lista
para usar con su sesión — no hay que instalar ni configurar nada.

1. Inicie sesión en la consola de AWS: <https://console.aws.amazon.com/>
2. Verifique en la esquina superior derecha que la **región** sea
   **US East (N. Virginia) / `us-east-1`** (a menos que Sooniverse le indique
   otra región distinta).
3. Haga clic en el ícono de terminal (`>_`) en la barra superior, junto a la
   campana de notificaciones. Se llama **CloudShell**.
4. Espere unos segundos a que la terminal termine de iniciar (dice
   "Starting environment...").

Va a ver una pantalla negra con texto — es normal, es su terminal.

---

## 4. Copiar los archivos y ejecutar el proceso

En la terminal de CloudShell que acaba de abrir, copie y pegue **cada bloque**
de comandos, uno a la vez, presionando Enter después de cada uno.

### 4.1. Instalar Terraform

CloudShell no trae Terraform preinstalado, así que lo instalamos con este
comando (es un único paquete oficial de HashiCorp, la empresa que crea
Terraform):

```bash
curl -sSLo terraform.zip https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip \
  && unzip -o -q terraform.zip -d ~/bin \
  && export PATH="$HOME/bin:$PATH" \
  && terraform -version
```

Si ve algo como `Terraform v1.9.8`, funcionó correctamente.

### 4.2. Subir la carpeta que Sooniverse le envió

Sooniverse le entregó (o le indicó dónde descargar) una carpeta llamada
`aws-byoc-terraform`. Súbala a CloudShell:

1. En CloudShell, haga clic en el botón **Actions** (⋮ o "Acciones") en la
   barra superior de la terminal.
2. Elija **Upload file** ("Subir archivo").
3. Si le dan la carpeta comprimida (`.zip`), suba ese archivo y luego
   descomprímalo con:
   ```bash
   unzip -o -q aws-byoc-terraform.zip
   cd aws-byoc-terraform
   ```
   Si le dieron los archivos sueltos, cree la carpeta y súbalos ahí:
   ```bash
   mkdir -p aws-byoc-terraform && cd aws-byoc-terraform
   ```
   (y repita "Upload file" para cada archivo: `main.tf`, `variables.tf`,
   `outputs.tf`)

### 4.3. Configurar los dos datos que le dio Sooniverse

Cree el archivo de configuración con este comando — **reemplace únicamente
los dos valores de ejemplo** por los que Sooniverse le entregó:

```bash
cat > terraform.tfvars << 'EOF'
sooniverse_account_id = "REEMPLACE_CON_EL_ID_DE_CUENTA_QUE_LE_DIO_SOONIVERSE"
external_id           = "REEMPLACE_CON_EL_EXTERNAL_ID_QUE_LE_DIO_SOONIVERSE"
aws_region            = "us-east-1"
EOF
```

Tip: puede editar el archivo directamente desde el editor de texto integrado
de CloudShell (ícono de lápiz/archivo) si prefiere no usar el comando `cat`.

### 4.4. Ejecutar la instalación

Ahora sí, los tres comandos que crean el permiso dentro de su cuenta:

```bash
terraform init
```
(descarga los componentes necesarios; toma unos segundos)

```bash
terraform plan
```
(le muestra **exactamente** qué se va a crear, antes de crear nada — revíselo
si quiere, no cambia nada todavía)

```bash
terraform apply
```
Terraform le va a preguntar `Do you want to perform these actions?` — escriba
`yes` y presione Enter para confirmar.

En unos segundos va a ver un mensaje `Apply complete!` y, debajo, algo como:

```
Outputs:

role_arn = "arn:aws:iam::123456789012:role/SooniverseDeployRole"
```

---

## 5. Enviar el resultado a Sooniverse

Copie exactamente el valor de `role_arn` que apareció (la línea que empieza
con `arn:aws:iam::...`) y envíelo a su contacto en Sooniverse por el mismo
canal donde le compartieron el External ID.

**Eso es todo de su lado.** Sooniverse usará ese dato para conectarse a su
cuenta de forma segura y comenzar a desplegar su infraestructura de IA.

---

## 6. ¿Cómo reviso o revoco el acceso más adelante?

Usted tiene control total en cualquier momento, sin necesidad de avisarle a
Sooniverse:

- **Ver qué permisos tiene el rol:** en la consola de AWS, vaya a
  **IAM → Roles → SooniverseDeployRole**. Ahí puede ver exactamente qué
  acciones puede realizar (creación de servidores, redes, etc. — nunca acceso
  a facturación, a otros roles/usuarios, ni a borrar la cuenta).
- **Ver qué hizo Sooniverse en su cuenta:** en **CloudTrail**, busque el
  nombre de usuario/rol `SooniverseDeployRole` para ver el historial completo
  de acciones, con fecha y hora.
- **Revocar el acceso por completo:** vuelva a CloudShell, entre a la carpeta
  `aws-byoc-terraform` y ejecute:
  ```bash
  export PATH="$HOME/bin:$PATH"
  terraform destroy
  ```
  Escriba `yes` cuando se lo pida. El rol se elimina al instante y Sooniverse
  pierde el acceso a su cuenta de inmediato.

---

## 7. Preguntas frecuentes

**¿Sooniverse puede ver mi tarjeta de crédito o cambiar mi método de pago?**
No. El permiso creado no incluye ningún acceso a facturación
("Billing"/"Cost Management").

**¿Sooniverse puede crear otros usuarios o roles en mi cuenta?**
No. El permiso solo alcanza para crear la infraestructura de cómputo/red
necesaria para el servicio de IA (servidores EC2, VPCs, Security Groups) —
no incluye permisos de gestión de usuarios ni de otros roles IAM.

**¿Qué pasa si pierdo el External ID?**
Pídaselo de nuevo a su contacto de Sooniverse por el mismo canal seguro que
usaron la primera vez. No afecta nada de lo ya creado.

**¿Tengo que repetir este proceso cada cierto tiempo?**
No. Se hace una sola vez. El rol queda activo indefinidamente hasta que usted
decida revocarlo (Sección 6).

**¿Qué pasa si mi empresa usa más de una cuenta de AWS (por ejemplo, una para
desarrollo y otra para producción)?**
Repita este mismo proceso en cada cuenta donde quiera que Sooniverse
despliegue infraestructura, usando un External ID distinto para cada una
(pídaselo a su contacto de Sooniverse).
