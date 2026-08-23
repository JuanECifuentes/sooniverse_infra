# Guía: Dominio propio + HTTPS (certbot/Let's Encrypt) para el Gateway

> Aplica al modo por defecto (`red_y_aislamiento.gestion_red: "auto"`). Esta guía
> es el paso manual **único** que exige el operador antes de un despliegue con
> `gateway.dominio.habilitado: true`: crear el registro DNS A. Todo lo demás
> (reservar la IP fija, emitir el certificado, configurar nginx, renovar) lo hace
> `python scripts/generate_infra.py --run` por sí solo.

## Índice

1. [Qué necesitas antes de empezar](#1-qué-necesitas-antes-de-empezar)
2. [Elegir el subdominio](#2-elegir-el-subdominio)
3. [Reservar la IP fija del Gateway](#3-reservar-la-ip-fija-del-gateway)
4. [Crear el registro DNS A](#4-crear-el-registro-dns-a)
5. [Verificar la resolución DNS](#5-verificar-la-resolución-dns)
6. [Configurar el contrato](#6-configurar-el-contrato)
7. [Desplegar](#7-desplegar)
8. [Advertencias importantes](#8-advertencias-importantes)
9. [Solución de problemas](#9-solución-de-problemas)

---

## 1. Qué necesitas antes de empezar

- **Un dominio registrado** en cualquier registrador (Route53, GoDaddy, Namecheap,
  Cloudflare, etc.). **No hace falta que el dominio esté en Route53** ni en la
  misma cuenta de AWS: solo necesitas poder crear un registro `A` en su DNS.
- Acceso al panel de DNS de ese registrador (o a Route53 si ya vive ahí).
- Un correo de contacto real (`email_acme`) — Let's Encrypt lo usa **solo** para
  avisar antes de que un certificado caduque, nunca para nada más.
- El despliegue del cliente ya inicializado (`clients/<id>/config_global.yaml` o el
  `config_global.yaml` raíz), con `red_y_aislamiento.cidr_permitido_gateway` en
  `"0.0.0.0/0"` (ver advertencia en la sección 8).

## 2. Elegir el subdominio

Se recomienda un **subdominio dedicado** (`ia.acme.com`, `chat.acme.com`) en vez
del dominio raíz (`acme.com`):

- Aísla el registro DNS de este proyecto de cualquier otro que ya use el dominio
  raíz (sitio web corporativo, correo, etc.).
- Si el despliegue se destruye y se recrea con otra IP, solo hay que tocar ese
  registro, no arriesgar el resto del DNS del dominio.
- Permite tener varios entornos (`chat-dev.acme.com`, `chat-prod.acme.com`) sin
  chocar entre sí.

## 3. Reservar la IP fija del Gateway

Antes de crear el registro DNS necesitas una IP que **no cambie** en cada
despliegue. Este proyecto reserva una Elastic IP dedicada para el Gateway (nunca
existió antes de esta iteración — el Gateway usaba una IP pública efímera de
SkyPilot que cambiaba en cada `sky launch`).

```bash
python scripts/generate_infra.py --run --only network
```

Al final de la fase `[RED]` verás una línea como:

```
[RED] Elastic IP del Gateway reservada: 34.201.55.12 (eipalloc-0abc123...)
```

Esa IP es **estable**: correr `--only network` de nuevo la reutiliza (busca
primero por tags antes de crear una nueva), y sobrevive a `sky stop`/`sky start`
del Gateway porque se re-asocia automáticamente en la fase `gateway`.

## 4. Crear el registro DNS A

Con la IP de la sección anterior, crea un registro `A` apuntando el subdominio
elegido a esa IP, con TTL bajo (300s) mientras pruebas.

### Opción A — Route53 (consola)

1. Consola AWS -> **Route53** -> **Hosted zones** -> tu zona (o crea una nueva si
   el dominio no está delegado a Route53 todavía).
2. **Create record**:
   - Record name: `ia` (si la zona es `acme.com`, esto crea `ia.acme.com`)
   - Record type: `A`
   - Value: la Elastic IP de la sección 3
   - TTL: `300`
3. **Create records**.

### Opción A — Route53 (CLI)

Guarda esto como `change-batch.json`:

```json
{
  "Changes": [
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "ia.acme.com",
        "Type": "A",
        "TTL": 300,
        "ResourceRecords": [{ "Value": "34.201.55.12" }]
      }
    }
  ]
}
```

Y aplícalo:

```bash
aws route53 change-resource-record-sets --hosted-zone-id ZONA_ID --change-batch file://change-batch.json
```

### Opción B — Registrador genérico (GoDaddy / Namecheap / Cloudflare / otro)

1. Entra al panel de DNS de tu dominio.
2. Añade un registro:
   - Tipo: `A`
   - Host/Nombre: `ia` (o el subdominio elegido)
   - Valor/Apunta a: la Elastic IP de la sección 3
   - TTL: 300 segundos (o el mínimo permitido)
3. Guarda los cambios.

**Si usas Cloudflare:** desactiva el proxy naranja (Proxied) y déjalo en modo
DNS only (nube gris). Con el proxy activo, la validación HTTP-01 de Let's
Encrypt llega a los servidores de Cloudflare, no a tu Gateway, y la emisión del
certificado falla.

## 5. Verificar la resolución DNS

Antes de desplegar, confirma que el registro ya propagó y apunta exactamente a
la Elastic IP:

```bash
nslookup ia.acme.com
```

o

```bash
dig +short ia.acme.com
```

La salida debe ser **exactamente** la Elastic IP de la sección 3. Si tarda en
propagar (puede llevar desde minutos hasta un par de horas según el TTL previo
del registrador), espera y vuelve a comprobar antes de continuar — el
despliegue también lo verifica automáticamente (ver sección 7), pero confirmarlo
tú mismo antes ahorra un ciclo de espera.

## 6. Configurar el contrato

Edita `config_global.yaml` (o `clients/<id>/config_global.yaml`):

```yaml
gateway:
  dominio:
    habilitado: true
    seleccionado: "ia.acme.com"
    disponibles:
      - nombre: "ia.acme.com"
        email_acme: "ops@acme.com"
    staging: false              # true para las primeras pruebas (ver sección 8)
    redirigir_http: true
    eip_persistente: true
    esperar_dns_segundos: 300
```

No toques `gateway.tls.*`: con `dominio.habilitado: true` el generador deriva
automáticamente `tls.habilitado`, `tls.modo` y `tls.dominio` a partir de este
bloque.

Puedes registrar **varios dominios disponibles** para el mismo cliente y elegir
cuál usar con `seleccionado` (por ejemplo, para migrar de un dominio a otro sin
perder el historial de configuración):

```yaml
gateway:
  dominio:
    habilitado: true
    seleccionado: "chat.acme.com"   # el que se usa AHORA
    disponibles:
      - nombre: "ia.acme.com"
        email_acme: "ops@acme.com"
      - nombre: "chat.acme.com"
        email_acme: "ops@acme.com"
```

## 7. Desplegar

```bash
python scripts/generate_infra.py --run
```

La fase nueva `[DOMINIO]` (entre `gateway` y `workers`) verifica que el registro
A resuelva a la Elastic IP antes de pedir el certificado. Si `esperar_dns_segundos
> 0` y el DNS aún no propagó, la fase espera y reintenta dentro de ese
presupuesto, imprimiendo el registro exacto que falta. Si sigue sin resolver al
agotar el tiempo, el despliegue **no aborta**: continúa en HTTP con un
`[WARNING]`, y puedes re-emitir el certificado más tarde con:

```bash
python scripts/generate_infra.py --run --only dominio
```

Al terminar, el reporte final imprime las URLs con el dominio y `https://`:

```
 Chat (Open WebUI) : https://ia.acme.com/
 API (LiteLLM)     : https://ia.acme.com/v1
 Panel (Django)    : https://ia.acme.com/panel/
 Salud (nginx)     : https://ia.acme.com/healthz
```

## 8. Advertencias importantes

- **El puerto 80 debe quedar abierto a `0.0.0.0/0`** (`cidr_permitido_gateway:
  "0.0.0.0/0"`) mientras el certificado se emite o renueva: los servidores de
  validación de Let's Encrypt se conectan desde rangos de IP que no puedes
  predecir ni acotar. El validador del contrato rechaza
  `gateway.dominio.habilitado: true` combinado con un `cidr_permitido_gateway`
  restringido, con un mensaje que apunta a esta sección.
- **Límites de Let's Encrypt:** 5 emisiones fallidas por hora por dominio exacto,
  y 50 certificados por semana por dominio registrable. Si vas a probar el flujo
  repetidamente (por ejemplo, mientras ajustas el DNS), pon `staging: true`
  primero — el directorio de pruebas de Let's Encrypt no tiene esos límites,
  pero emite certificados que los navegadores no confían por defecto (correcto
  para probar la mecánica, no para el tráfico real).
- **`destroy_infra.py` conserva la Elastic IP por defecto**
  (`dominio.eip_persistente: true`), precisamente para que el registro A no se
  invalide entre un destroy y el siguiente despliegue. Con `eip_persistente:
  false`, la IP se libera al destruir y el siguiente despliegue obtendrá una
  nueva — tendrás que actualizar el registro A a mano.
- **La renovación es automática:** un contenedor `certbot` en el propio Gateway
  reintenta la renovación cada 12 horas (Let's Encrypt renueva certificados con
  30 días o menos de vigencia), y nginx recarga la configuración cada 6 horas
  para tomar el certificado renovado sin caídas. No hay que hacer nada de forma
  manual.

## 9. Solución de problemas

### El registro A todavía no resuelve

```bash
dig +short ia.acme.com
```

Si no devuelve nada o devuelve una IP distinta a la Elastic IP reservada,
revisa el registro en tu proveedor DNS y espera a que propague. Vuelve a correr
`python scripts/generate_infra.py --run --only dominio` cuando resuelva.

### "Challenge failed" / certbot no puede emitir el certificado

Causas más comunes:

1. El puerto 80 está bloqueado o restringido a un CIDR distinto de
   `0.0.0.0/0` — revisa `cidr_permitido_gateway`.
2. El DNS todavía no resuelve a la Elastic IP correcta (ver punto anterior).
3. Cloudflare (u otro proxy) tiene el proxy activo — desactívalo (sección 4).
4. Ya agotaste el límite de 5 intentos/hora — espera una hora o usa
   `staging: true` para seguir probando la mecánica.

### nginx no arranca

Si el certificado no se pudo emitir por ninguna vía, el despliegue genera un
certificado autofirmado en la misma ruta como último recurso, así que nginx
**siempre** debería arrancar (con una advertencia de certificado no confiable en
el navegador hasta que el DNS resuelva y se re-emita). Si aun así no arranca,
revisa los logs del Gateway:

```bash
ssh -i ~/.sky/generated/ssh-keys/sooniverse-<cliente>-<entorno>-gw.key ubuntu@<ip-o-dominio>
cd sooniverse_infra/docker_images/gateway
sudo docker compose logs proxy --tail 100
```

### El certificado caducó

No debería pasar con la renovación automática activa, pero para forzar una
reemisión manual:

```bash
python scripts/generate_infra.py --run --only dominio
```

### Quiero volver a HTTP / quitar el dominio

```yaml
gateway:
  dominio:
    habilitado: false
```

y vuelve a correr `--run --only network` (para cerrar el puerto 443 en el
Security Group) seguido de `--run` completo.
