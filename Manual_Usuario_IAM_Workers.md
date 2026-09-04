# Guía: Usuario IAM para Apagar/Arrancar Workers (configuración manual desde la consola AWS)

> Aplica cuando el usuario con el que despliegas (`red_y_aislamiento.aws_profile`,
> o el perfil por defecto) **no tiene permisos IAM** para que
> `scripts/generate_infra.py --run` cree este usuario solo. Si los tiene, no
> necesitas esta guía: la fase `network` lo crea/actualiza automáticamente en
> cada despliegue (ver `scripts/aws_iam_worker_control.py`) y verás en la
> consola de `--run` una línea `[IAM] Usuario '...' creado/reutilizado...`. Si
> en cambio ves `[WARNING] No se pudo aprovisionar el usuario IAM...`, sigue
> esta guía una vez por cliente/entorno.

## 1. Por qué existe este usuario

Los botones **Apagar** / **Arrancar** del panel (card "Workers en la VPC")
llaman a `ec2:StopInstances`/`StartInstances` desde el propio Gateway. Esas
credenciales viven en el `.env` del Gateway y **nunca deben ser las mismas
con las que se despliega** (`sooniverse-ec2-managment` o el perfil BYOC del
cliente): esas tienen permisos amplios (VPC, EC2 sin restricción, a veces
IAM) y quedarían corriendo sin supervisión, indefinidamente, dentro de una
instancia expuesta a internet.

Este usuario en cambio solo puede:

- `ec2:DescribeInstances` (sin restricción — EC2 no soporta scoping por
  recurso para esta acción, es una limitación de AWS, no de este contrato).
- `ec2:StartInstances` / `ec2:StopInstances` **únicamente** sobre las
  instancias que tengan las tags exactas de tu cliente/entorno (`cliente_id`,
  `entorno`, `rol=worker`) — no puede tocar el Gateway, ni NAT, ni nada fuera
  de los workers de este despliegue, ni los de otro cliente/entorno en la
  misma cuenta.

Si este usuario no existe (o sus credenciales no están en el `.env` del
Gateway), `metrics/workers.py::ec2_disponible()` hace un `DryRun` real y
falla en modo seguro: el panel **oculta** los botones Apagar/Arrancar en vez
de mostrarlos deshabilitados.

## 2. Antes de empezar

Necesitas:

- Acceso a la consola AWS con permisos de **administrador IAM** (`iam:CreateUser`,
  `iam:CreateAccessKey`, `iam:PutUserPolicy` como mínimo) — normalmente NO es
  el mismo usuario con el que despliegas.
- Estos cuatro datos de tu contrato (`config_global.yaml`):

  | Dato | Dónde lo ves en `config_global.yaml` | Ejemplo de este despliegue |
  |---|---|---|
  | `cliente_id` | `cliente.id` | `acme` |
  | `entorno` | `cliente.entorno` | `prod` |
  | `region` | `red_y_aislamiento.region` | `us-east-1` |
  | `account_id` (de la cuenta AWS donde despliegas) | Consola AWS, esquina superior derecha, o `aws sts get-caller-identity` | `861870144465` |

  El nombre del usuario se arma como `sooniverse-<cliente_id>-<entorno>-worker-ctrl`
  (para el ejemplo de arriba: `sooniverse-acme-prod-worker-ctrl`) — es el
  mismo nombre determinista que usa `scripts/aws_iam_worker_control.py`, así
  que si más adelante el usuario de despliegue SÍ obtiene permisos IAM, lo
  reconocerá como "ya existe" en vez de crear un duplicado.

## 3. Crear el usuario IAM

1. Consola AWS → busca **IAM** → menú izquierdo **Users** (Usuarios) →
   **Create user** (Crear usuario).
2. **User name**: `sooniverse-<cliente_id>-<entorno>-worker-ctrl`
   (ej. `sooniverse-acme-prod-worker-ctrl`).
3. **NO marques** "Provide user access to the AWS Management Console" — este
   usuario es solo programático (access key), nunca necesita iniciar sesión
   en la consola.
4. **Next**. En "Set permissions" elige **Attach policies directly** pero
   **no selecciones ninguna política todavía** — se la das como inline policy
   en el paso 5, con el scoping exacto de tags. Pulsa **Next**.
5. (Opcional pero recomendado) En "Tags" agrega, para que quede claro en la
   consola qué es este usuario y de qué despliegue:

   | Key | Value |
   |---|---|
   | `sooniverse:managed` | `true` |
   | `sooniverse:client-id` | `acme` |
   | `sooniverse:environment` | `prod` |
   | `sooniverse:component` | `worker-control` |
   | `gestionado_por` | `sooniverse` |

6. **Create user**.

## 4. Adjuntar la política de permisos mínimos

1. Entra al usuario recién creado → pestaña **Permissions** → **Add permissions**
   → **Create inline policy**.
2. Pestaña **JSON** del editor → borra el contenido de ejemplo y pega esto,
   **sustituyendo `region`, `account_id`, `cliente_id` y `entorno` por los
   tuyos** (los valores de abajo son los de este despliegue de ejemplo):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "DescribeInstancesNoSoportaScopingPorRecurso",
         "Effect": "Allow",
         "Action": "ec2:DescribeInstances",
         "Resource": "*"
       },
       {
         "Sid": "StartStopSoloWorkersDeEsteClienteEntorno",
         "Effect": "Allow",
         "Action": ["ec2:StartInstances", "ec2:StopInstances"],
         "Resource": "arn:aws:ec2:us-east-1:861870144465:instance/*",
         "Condition": {
           "StringEquals": {
             "aws:ResourceTag/cliente_id": "acme",
             "aws:ResourceTag/entorno": "prod",
             "aws:ResourceTag/rol": "worker"
           }
         }
       }
     ]
   }
   ```

   > **Importante:** las claves de tag en la condición (`cliente_id`, `entorno`,
   > `rol`) son las que SkyPilot escribe DE VERDAD en la instancia EC2 del
   > worker (`generate_infra.py::TopologyBuilder.build_worker()` le pasa a
   > SkyPilot `labels: {cliente_id, modo, entorno, gestionado_por, rol: "worker", workload: <id>}`,
   > y SkyPilot las traduce en tags EC2 tal cual, sin prefijo). **No** son las
   > tags `sooniverse:client-id`/`sooniverse:environment` que usa la capa de
   > red (VPC/SG/NAT) — ese es un espacio de tags distinto. Si escribes mal
   > estas tres claves o sus valores, la política queda sin efecto: ninguna
   > instancia hará match y el start/stop fallará con `UnauthorizedOperation`
   > para TODOS los workers, sin ningún aviso visible salvo ese error.
3. **Next**. **Policy name**: `sooniverse-worker-control` (mismo nombre que
   usa el aprovisionamiento automático, por consistencia).
4. **Create policy**.

## 5. Crear el access key

1. En el mismo usuario → pestaña **Security credentials** → sección
   **Access keys** → **Create access key**.
2. **Use case**: elige **Application running outside AWS** (o **Other** si esa
   opción no aparece en tu consola) → **Next**.
3. Description tag (opcional): `worker-ctrl del panel Sooniverse` → **Create access key**.
4. **Copia YA el "Access key" y el "Secret access key"** (o descarga el
   `.csv`) — AWS solo te lo muestra una vez; si lo pierdes, tendrás que
   crear un access key nuevo (máximo 2 activos por usuario) desde este mismo
   panel.

## 6. Poner las credenciales en el despliegue

Estas credenciales van al `.env` del Gateway como `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` — **nunca** las variables del despliegue automático,
son un archivo/servicio distinto.

**Si todavía no desplegaste** (vas a correr `--run` por primera vez):

1. Edita el `.env` LOCAL (raíz del repo, el mismo que usa `generate_infra.py`)
   y agrega o reemplaza estas dos líneas:
   ```
   AWS_ACCESS_KEY_ID=<el access key id del paso 5>
   AWS_SECRET_ACCESS_KEY=<el secret access key del paso 5>
   ```
2. Corre el despliegue normalmente (`python scripts/generate_infra.py --run`).
   Se sincronizan al Gateway como cualquier otra variable de `.env`.

**Si el Gateway ya está desplegado** (infra ya arriba):

1. Edita igual el `.env` LOCAL con las dos líneas de arriba.
2. Empuja el cambio y recrea el contenedor `metrics` (el único que las usa):
   ```bash
   python scripts/sync_openwebui_models.py --config config_global.yaml --apply
   ```
   Si prefieres no tocar Open WebUI, puedes limitarte a `metrics` a mano:
   ```bash
   sky exec <cluster-del-gateway> \
     'cd /home/ubuntu/sooniverse_infra/docker_images/gateway && \
      sudo docker compose --env-file /home/ubuntu/sooniverse_infra/.env up -d --build metrics'
   ```
   (sustituye `<cluster-del-gateway>`, ej. `sooniverse-acme-prod-gw`; el
   `.env` remoto también debe tener las dos líneas nuevas — cópialas ahí
   igual, con `sky exec ... "cat >> .env"` o editándolo por SSH).

## 7. Verificar que funcionó

1. Entra al panel (`https://tu-dominio/panel/`) con una cuenta de rol
   Administrador o Acceso al panel.
2. En la card "Workers en la VPC": si antes no aparecían los botones
   **Apagar**/**Arrancar**, ahora deberían verse junto a **Salud** y
   **Reiniciar**.
3. Si siguen sin aparecer, revisa (en este orden):
   - ¿Las dos variables llegaron al `.env` **remoto** del Gateway? (`sky exec <cluster> 'grep AWS_ .env'` desde tu máquina, con `sky` autenticado).
   - ¿Se recreó el contenedor `metrics` DESPUÉS de escribir las variables? (un `docker compose up -d` sin `--build`/sin cambio de imagen a veces no basta si solo cambiaron variables de entorno — usa `--build metrics` o `up -d --force-recreate metrics`).
   - ¿La política inline quedó con las tres claves de tag exactas (`cliente_id`, `entorno`, `rol`) y los valores correctos de TU despliegue?
   - ¿El `Resource` de la política usa la región y el account id correctos?

## 8. Alternativa: automatizarlo (opcional)

Si prefieres que `--run` cree/renueve este usuario solo en cada despliegue
(sin repetir esta guía por cada cliente nuevo), dale al usuario CON EL QUE
DESPLIEGAS estos permisos IAM adicionales, idealmente restringidos por un
`Condition` a nombres de usuario `sooniverse-*-worker-ctrl` (para que ni
siquiera el usuario de despliegue pueda crear/editar usuarios IAM fuera de
ese patrón):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GestionarSoloUsuariosWorkerCtrl",
      "Effect": "Allow",
      "Action": [
        "iam:GetUser",
        "iam:CreateUser",
        "iam:TagUser",
        "iam:PutUserPolicy",
        "iam:DeleteUserPolicy",
        "iam:ListAccessKeys",
        "iam:CreateAccessKey",
        "iam:DeleteAccessKey",
        "iam:GetAccessKeyLastUsed",
        "iam:DeleteUser"
      ],
      "Resource": "arn:aws:iam::*:user/sooniverse-*-worker-ctrl"
    },
    {
      "Sid": "ResolverElAccountId",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}
```

Con esto, la próxima vez que corras `--run` verás `[IAM] Usuario '...' creado`
en la consola, y ya no necesitas repetir los pasos 3-6 de esta guía.

## 9. Seguridad — qué NO hacer

- No le des a este usuario ninguna acción distinta de `DescribeInstances`/
  `StartInstances`/`StopInstances`, ni un `Resource: "*"` en las dos últimas.
- No reutilices el access key del despliegue (`sooniverse-ec2-managment` u
  otro con permisos amplios) como atajo — es exactamente el riesgo que este
  usuario existe para evitar.
- Si sospechas que el access key se filtró, revócalo desde el mismo panel
  **Security credentials** (Deactivate o Delete) y crea uno nuevo — la
  política inline no se pierde, solo el access key.
- Rota el access key periódicamente (créalo nuevo, actualiza `.env`, luego
  borra el viejo) — AWS permite hasta 2 activos por usuario justo para poder
  hacer esta rotación sin downtime.
