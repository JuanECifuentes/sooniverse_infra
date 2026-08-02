# Guía: VPC y Security Groups para `sooniverse` (SkyPilot)

> **📜 Documento histórico / anexo.** Desde la Fase 2 de la automatización de red,
> este procedimiento manual **ya no es necesario** en el modo por defecto
> (`red_y_aislamiento.gestion_red: "auto"`): `python scripts/generate_infra.py --run`
> crea y destruye toda esta infraestructura por sí solo, vía
> `scripts/aws_network.py::AwsNetworkManager` (boto3 puro). Ver
> **[`docs/02_RED_AWS.md`](docs/02_RED_AWS.md)** para el equivalente automatizado de
> cada paso de esta guía, y **[`docs/04_DESTRUCCION.md`](docs/04_DESTRUCCION.md)**
> para la destrucción segura (en vez del paso 3 de más abajo).
>
> Esta guía sigue siendo válida y se conserva **solo** para el modo
> `gestion_red: "existente"` (VPC creada a mano, por política interna o cuenta
> compartida) — ver `MANUAL_DESPLIEGUE.md`, Anexo A. La pregunta abierta de la
> línea 124 original de este documento ("¿el contrato espera el ID o el nombre del
> SG?") ya está resuelta: SkyPilot 0.13.0 empareja/crea Security Groups **por
> nombre** (`group_name`), no por `sg-id` — confirmado leyendo
> `sky/provision/aws/config.py` del paquete instalado.

Esta guía cubre tres tareas desde la consola de AWS:
1. Crear la VPC con subred pública + privada + NAT Gateway
2. Crear los Security Groups (gateway y workers)
3. Eliminar todo al terminar las pruebas, para evitar cobros

---

## 1. Crear la VPC

### Requisito que resuelve
`workers_en_subred_privada: true` exige subred pública + subred privada + NAT Gateway. La VPC por defecto de AWS no cumple esto (todas sus subredes rutan directo al Internet Gateway).

### Pasos

1. Consola → buscar **"VPC"**
2. Menú lateral → **Your VPCs** → botón **"Create VPC"**
3. Selecciona **"VPC and more"** (no "VPC only")
4. Configura:

   | Campo | Valor recomendado |
   |---|---|
   | Name tag auto-generation | `sooniverse` |
   | IPv4 CIDR block | `10.0.0.0/16` (default) |
   | Number of Availability Zones | 1 (para pruebas) |
   | Number of public subnets | 1 |
   | Number of private subnets | 1 |
   | NAT gateways | **Zonal** (evita "Ninguna" — los workers necesitan salida a Internet) |
   | VPC endpoints | None (o S3 Gateway endpoint si usas buckets S3, es gratis) |
   | DNS options | ✅ Enable DNS hostnames <br> ✅ Enable DNS resolution |

5. Clic en **"Create VPC"** (tarda 2-3 min)

### Verificación

- **Subnets** → la subred pública debe tener ruta `0.0.0.0/0 → igw-xxxxx`
- **Subnets** → la subred privada debe tener ruta `0.0.0.0/0 → nat-xxxxx`

### Registrar en el contrato

```yaml
red_y_aislamiento:
  vpc_name: "sooniverse-vpc"
  workers_en_subred_privada: true
```

### ⚠️ Costos a tener en cuenta

| Recurso | Costo |
|---|---|
| VPC, subredes, route tables, IGW, SGs | Gratis |
| NAT Gateway (por hora, exista o no tráfico) | ~$0.045/hora (~$32-33/mes) |
| NAT Gateway (procesamiento de datos) | ~$0.045/GB |
| Elastic IP asociada al NAT | ~$0.005/hora (~$3.60/mes) |

---

## 2. Crear los Security Groups

### Por qué usar referencia por SG (no por CIDR)

Aún no existe el Nodo Gateway, así que no hay una IP fija que referenciar. La solución es crear una regla **SG → SG**: se puede declarar hoy contra un Security Group vacío, y en cuanto el Gateway reciba ese SG al lanzarse, la regla se activa automáticamente sin editar nada.

### 2.1 Crear `sg-sooniverse-gateway` (vacío, reserva de identidad)

1. **VPC → Security Groups → Create security group**
2. **Basic details:**
   - Security group name: `sg-sooniverse-gateway`
   - Description: `SG del nodo gateway - sooniverse`
   - VPC: `sooniverse-vpc`
3. **Inbound rules:** vacío por ahora (o SSH desde tu IP si ya sabes que lo necesitarás)
4. **Outbound rules:** dejar el default (`All traffic → 0.0.0.0/0`)
5. Tags (opcional): `Name` = `sg-sooniverse-gateway`
6. **Create security group**
7. **Copiar el Security Group ID** generado (ej. `sg-0abc123def456789`)

### 2.2 Crear `sg-sooniverse-workers` (con reglas apuntando al gateway)

1. **Security Groups → Create security group**
2. **Basic details:**
   - Security group name: `sg-sooniverse-workers`
   - Description: `SG de workers - acceso solo desde gateway`
   - VPC: `sooniverse-vpc`
3. **Inbound rules** — agregar dos reglas:

   **Regla 1 — SSH:**
   | Campo | Valor |
   |---|---|
   | Type | SSH |
   | Source type | Custom |
   | Source | escribir `sg-` y seleccionar `sg-sooniverse-gateway` de la lista |
   | Description | SSH solo desde gateway |

   **Regla 2 — puerto SkyPilot:**
   | Campo | Valor |
   |---|---|
   | Type | Custom TCP |
   | Port range | 8007 |
   | Source type | Custom |
   | Source | escribir `sg-` y seleccionar `sg-sooniverse-gateway` de la lista |
   | Description | Puerto SkyPilot solo desde gateway |

4. **Outbound rules:** dejar el default (`All traffic → 0.0.0.0/0`) — los workers necesitan salir vía NAT
5. Tags (opcional): `Name` = `sg-sooniverse-workers`
6. **Create security group**
7. **Copiar el Security Group ID** generado (ej. `sg-0fedcba987654321`)

### 2.3 Verificación

- **Security Groups** → filtrar por `sooniverse-vpc`
- Abrir `sg-sooniverse-workers` → pestaña **Inbound rules**
- Confirmar que el **Source** de ambas reglas es el SG del gateway (no `0.0.0.0/0`)

### 2.4 Declarar en el contrato

```yaml
red_y_aislamiento:
  vpc_name: "sooniverse-vpc"
  workers_en_subred_privada: true
  security_group_workers: "sg-0fedcba987654321"   # ID real de sg-sooniverse-workers
```

> ⚠️ Verificar si el contrato espera el **ID** (`sg-xxxxx`) o el **nombre** (`sg-sooniverse-workers`) — usar el que corresponda según la documentación de SkyPilot.

### 2.5 Pendiente para cuando se levante la infraestructura

- [ ] Al lanzar el Nodo Gateway: asignarle manualmente el SG `sg-sooniverse-gateway`
- [ ] Confirmar que `security_group_workers` en el contrato coincide con el ID real
- [ ] Probar conectividad (`telnet <ip-privada-worker> 8007`) desde el gateway una vez ambos existan

---

## 3. Eliminar la VPC (para evitar cobros)

### Método recomendado

1. **VPC → Your VPCs**
2. Seleccionar `sooniverse-vpc`
3. **Actions → Delete VPC**
4. AWS muestra la lista de recursos asociados a eliminar (subredes, IGW, route tables, NAT Gateway, etc.)
5. Escribir "delete" para confirmar

### ⚠️ Verificación posterior (paso crítico)

El asistente de borrado a veces **no libera la Elastic IP** automáticamente, y esta sigue cobrando aunque el NAT ya no exista:

1. **VPC → Elastic IPs**
2. Buscar cualquier IP sin "Associated instance ID"
3. Seleccionar → **Actions → Release Elastic IP addresses**

También revisar:

- **VPC → NAT Gateways** → confirmar estado **"Deleted"** (no "Pending" colgado)
- **Security Groups** → si `sg-sooniverse-gateway` y `sg-sooniverse-workers` no se borraron junto con la VPC, eliminarlos manualmente:
  - Seleccionar ambos → **Actions → Delete security groups**
  - Si da error de "dependency violation", significa que alguna instancia aún los tiene asignados — eliminar esa instancia primero

### Tip para pruebas repetidas

Configurar una alerta de **AWS Budgets** (Billing → Budgets → Create budget) con un límite bajo (ej. $5) para recibir aviso por email si algo queda corriendo sin darse cuenta. Toma 2 minutos y es gratis.

---

## Resumen de costos

| Recurso | Costo mientras existe | Acción al terminar |
|---|---|---|
| VPC, subredes, IGW, route tables | $0 | Se borran con "Delete VPC" |
| Security Groups | $0 | Se borran con "Delete VPC" (o manual si quedan huérfanos) |
| NAT Gateway | ~$32-35/mes + $0.045/GB | Eliminar explícitamente, no esperar |
| Elastic IP | ~$3.60/mes | **Liberar manualmente** — es el punto que más se olvida |