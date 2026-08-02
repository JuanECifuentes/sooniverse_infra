# 06. Runbook (troubleshooting)

Formato: síntoma → causa → diagnóstico → solución. Amplía la sección 10 de `MANUAL_DESPLIEGUE.md` con los fallos introducidos por la red autogestionada (Fases 1-7).

---

## El Gateway arranca pero LiteLLM no tiene modelos

- **Síntoma:** `/v1/models` devuelve lista vacía; el log dice `litellm_config.yaml generado SIN deployments`.
- **Causa:** esperado en el primer arranque — las IPs privadas de los workers no existían todavía cuando se renderizó el config inicial.
- **Diagnóstico:** `sky status` para confirmar que los workers están `UP`.
- **Solución:** `python scripts/sync_endpoints.py --apply`.

## `sync_endpoints.py` no descubre IPs

- **Diagnóstico:** prueba 4 métodos en cascada (`discover_worker_ips`, `scripts/sync_endpoints.py:273`): API Python de SkyPilot → `describe_instances` por tag `ray-cluster-name`/`skypilot-cluster-name` → marcadores `SOONIVERSE_WORKER_READY` en logs → `sky status --ip`.
  ```bash
  sky status                                                  # ¿el clúster está UP?
  sky logs sooniverse-<cliente>-<entorno>-<workload> --no-follow | grep SOONIVERSE_
  aws ec2 describe-instances --filters "Name=tag:ray-cluster-name,Values=sooniverse-<cliente>-<entorno>-<workload>" --region <region>
  ```
- **Solución:** si ninguno de los 4 métodos encuentra nada, el worker nunca terminó de arrancar (ver siguiente entrada). Como último recurso, usa un pool manual: `sync_endpoints.py --endpoints-file endpoints.json --apply`.

## Un worker no arranca

```bash
sky logs sooniverse-<cliente>-<entorno>-<workload>
sky exec sooniverse-<cliente>-<entorno>-<workload> "nvidia-smi && sudo docker compose ps"
```

| Causa | Señal | Solución |
|---|---|---|
| Sin salida a Internet | `apt-get`/descarga de HF cuelga | Falta el NAT Gateway (`nat_gateway.modo: none` sin `vpc_endpoints.s3`, o el NAT no llegó a `available`) |
| VRAM insuficiente | `CUDA out of memory` | Bajar `gpu_memory_utilization` o `max_model_len` |
| Cuota de AWS | `InsufficientInstanceCapacity` | Otra región/AZ, o pedir aumento de cuota de GPU |
| SG mal formado | El worker arranca pero nunca aparece en `describe_instances` con el tag esperado | Revisar que `AwsNetworkManager.provision()` haya terminado sin errores (`infra_event` de esa fase) |

## El worker no tiene salida a Internet (NAT)

- **Síntoma:** `check_worker_has_internet_egress` en `verify_deployment.py` falla, o el worker se queda colgado descargando el modelo.
- **Diagnóstico:**
  ```bash
  aws ec2 describe-nat-gateways --filter Name=tag:sooniverse:deployment-id,Values=<deployment_id> --region <region>
  # State debe ser 'available', no 'pending' ni 'failed'
  aws ec2 describe-route-tables --filters Name=tag:sooniverse:component,Values=rtb-private --region <region>
  # debe existir una ruta 0.0.0.0/0 -> nat-*
  ```
- **Solución:** si el NAT está en `failed`, el `provision()` debió haber lanzado un `NetworkError` de timeout — revisar `infra_event` de esa corrida. Si está `available` pero la route table no tiene la ruta, es un bug de `ensure_route_tables()`; reportar con el `deployment_id`.

## El Gateway no alcanza a los workers

```bash
GW_WORKER_IP=10.0.x.y
sky exec sooniverse-<cliente>-<entorno>-gw "curl -sv --max-time 5 http://$GW_WORKER_IP:8007/health"
```

| Causa | Solución |
|---|---|
| SG de workers sin la regla del puerto | Verificar `check_workers_sg_no_open_cidr` en `verify_deployment.py`; si la regla SG→SG falta, es un bug de `ensure_security_groups()` — no debería pasar en una corrida exitosa |
| Clústeres en VPCs distintas | Confirmar que gateway y workers comparten `vpc_name` real (`NetworkOutputs.vpc_name`) |
| vLLM aún cargando el modelo | Esperar: la primera carga tarda varios minutos |

## `RequiresDestroyError` al correr `--run`

- **Síntoma:** `[CAMBIOS NO APLICABLES EN CALIENTE] Uno o más cambios requieren destroy + provision: red_y_aislamiento.vpc_cidr`.
- **Causa:** el contrato cambió un campo que no es modificable sobre una VPC viva (`vpc_cidr`, `azs`, `nat_gateway.modo`) — ver `docs/03_ESTADO_Y_BD.md` §6.
- **Diagnóstico:** el mensaje ya lista exactamente qué campo(s) causaron el bloqueo.
- **Solución:** `python scripts/destroy_infra.py --yes` y luego `python scripts/generate_infra.py --run` de nuevo. No hay atajo: cambiar el CIDR de una VPC en uso no es una operación de AWS soportada.

## `destroy_infra.py` falla con `DependencyViolation`

- **Síntoma:** un recurso queda en `report.failed` con código `DependencyViolation`.
- **Causa:** típicamente una ENI (Elastic Network Interface) huérfana de una instancia terminada hace poco, que aún no se liberó, bloqueando el borrado de una subred o SG.
- **Diagnóstico:** el `DestroyReport.manual_actions_required` ya trae el comando exacto, p.ej.:
  ```bash
  aws ec2 describe-network-interfaces --filters Name=vpc-id,Values=<vpc-id> --region <region>
  ```
- **Solución:** esperar 1-2 minutos (las ENIs de instancias recién terminadas tardan en liberarse) y re-correr `destroy_infra.py --yes` — es idempotente, solo reintenta lo que falló.

## `destroy_infra.py` no puede borrar la VPC: queda un SG `sky-sg-*` sin dueño

- **Síntoma:** tras varios reintentos, todo se borra salvo la VPC, con `DependencyViolation`; `aws ec2 describe-security-groups --filters Name=vpc-id,Values=<vpc-id>` muestra un SG adicional (además de `default` y los nuestros) con un nombre tipo `sky-sg-<usuario>-<hash>` y tag `skypilot=true`.
- **Causa:** confirmado en una corrida real. SkyPilot puede auto-crear su propio Security Group ("Auto-created security group for Ray workers") dentro de nuestra VPC en ciertas circunstancias, y **no lo registramos en `sooniverse.infra_resource`** -el mecanismo de propiedad correctamente se niega a borrar algo que no reconoce, pero eso bloquea el borrado de la VPC. Esto es exactamente el escenario "discovered_dependency" que la especificación original del proyecto contemplaba detectar automáticamente (por VPC + prefijo de nombre `sky-sg-`) y que **todavía no está implementado**.
- **Diagnóstico:**
  ```bash
  aws ec2 describe-security-groups --region <region> --filters Name=vpc-id,Values=<vpc-id> \
    --query 'SecurityGroups[].{Id:GroupId,Name:GroupName}'
  ```
- **Solución manual** (verificar primero que no tenga ENIs asociadas, es decir que `describe-network-interfaces` para esa VPC ya esté vacío):
  ```bash
  aws ec2 revoke-security-group-ingress --region <region> --group-id <sg-id> --ip-permissions '<copiar de describe-security-groups>'
  aws ec2 revoke-security-group-egress  --region <region> --group-id <sg-id> --ip-permissions '<copiar de describe-security-groups>'
  aws ec2 delete-security-group --region <region> --group-id <sg-id>
  python scripts/destroy_infra.py --yes   # ahora sí borra la VPC
  ```
- **Pendiente (mejora futura, no implementada):** que `AwsNetworkManager` detecte SGs `sky-sg-*` dentro de su propia VPC durante `destroy()`, los registre como `discovered_dependency` y los borre en el mismo ciclo, sin intervención manual.

## `--scan-orphans` encuentra recursos inesperados

- **Causa posible 1:** un `destroy_infra.py` anterior falló a medias (algún recurso en `report.failed`).
- **Causa posible 2:** alguien borró la fila de `infra_deployment` a mano en PostgreSQL sin pasar por `destroy_infra.py`.
- **Solución:** revisar la salida (`deployment_status` de cada huérfano) antes de purgar. `--purge-orphans --yes` los borra en orden seguro, pero es irreversible — confirmar que de verdad no pertenecen a un despliegue activo de otro cliente antes de correrlo.
- **Falso positivo conocido:** un NAT Gateway recién borrado (por nosotros, correctamente) puede seguir apareciendo unos minutos en `describe-nat-gateways` con `State: deleted` -AWS conserva el registro visible un rato-, y `--scan-orphans` lo listará como "no-registrado" (porque nuestro propio `mark_resource_state` ya lo marcó `deleted` en la BD, así que deja de contar como "conocido"). No representa ningún coste ni acción pendiente; verificar el campo `State` antes de asumir que es un huérfano real.

## Aviso de solape de CIDR entre clientes

- **Síntoma:** `[WARNING] 'vpc_cidr' 10.0.0.0/16 se solapa con el despliegue activo 'globex/prod' (10.0.0.0/16)...`.
- **Causa:** dos clientes con el mismo `vpc_cidr` (o rangos que se cruzan) en la misma región.
- **Es un error real:** no, mientras esas VPCs nunca se conecten por peering. El aviso es preventivo.
- **Solución:** si en algún momento vas a necesitar peering, cambia el `vpc_cidr` del cliente nuevo al CIDR libre que sugiere el aviso (o corre `list_deployments.py` primero para planificarlo).

## `db_setup.py`/cualquier script no conecta a PostgreSQL

```
[ERROR DB] No se pudo conectar a PostgreSQL en X:5432 -> ...
```

| Causa | Solución |
|---|---|
| Credenciales incorrectas | Revisar `DB_*` en `.env` (**el archivo manda sobre el shell**, ver `resolve_db_config()`) |
| Sin acceso de red | Security Group / `pg_hba.conf` deben permitir la IP de quien ejecuta el script |
| Falta `psycopg2` | `pip install psycopg2-binary` |
| Base de datos inexistente | El script crea el **esquema** `sooniverse`, no la base de datos en sí — `createdb` es un paso previo |

**Importante:** si esto falla durante `generate_infra.py --run`, el aprovisionamiento **aborta antes de tocar AWS** (`PostgresInfraStateStore.ping()` en `_open_state_store()`). Es el comportamiento buscado, no un bug.

## El panel Django rompe enlaces/estáticos detrás de nginx

- **Síntoma:** el panel carga en `/panel/` pero los enlaces internos o el CSS apuntan a rutas incorrectas (sin el prefijo `/panel`).
- **Causa:** `FORCE_SCRIPT_NAME`/`STATIC_URL` desincronizados, o `collectstatic` no corrió dentro del contenedor `metrics`.
- **Diagnóstico:**
  ```bash
  sky exec sooniverse-<cliente>-<entorno>-gw "sudo docker compose -f docker_images/gateway/docker-compose.yml exec metrics env | grep FORCE_SCRIPT_NAME"
  sky exec sooniverse-<cliente>-<entorno>-gw "sudo docker compose -f docker_images/gateway/docker-compose.yml logs metrics | grep -i collectstatic"
  ```
- **Solución:** `FORCE_SCRIPT_NAME` debe ser `/panel` (default en `docker-compose.yml` generado); si se cambió a vacío para depurar con `exponer_puertos_directos: true`, es esperado que los enlaces sean distintos (sin prefijo) en ese modo.

## Certificado TLS autofirmado no aparece / navegador lo rechaza

- **Síntoma:** `https://<gateway_ip>/` no conecta, o el navegador marca el certificado como inválido.
- **Causa 1 (esperada):** es autofirmado — el navegador SIEMPRE lo marcará como no confiable; hay que aceptar la excepción manualmente. No es un bug.
- **Causa 2:** el certificado nunca se generó.
- **Diagnóstico:**
  ```bash
  sky exec sooniverse-<cliente>-<entorno>-gw "ls -la /home/ubuntu/sooniverse_infra/docker_images/gateway/nginx/certs/"
  ```
- **Solución:** si faltan los archivos, revisar el log del `setup` del gateway (`TLS_SELF_SIGNED_SETUP` en `scripts/generate_infra.py`) — requiere `openssl` en la AMI (está en toda AMI Ubuntu estándar).

## `exponer_puertos_directos: false` pero igual puedo acceder a :4000

- **Causa:** el Security Group cachea reglas antiguas de una corrida anterior con el flag en `true`, o el operador está probando desde DENTRO de la VPC (donde el SG no aplica igual que desde Internet).
- **Diagnóstico:** `aws ec2 describe-security-groups --group-ids <sg-gateway-id>` y confirmar que no hay una regla de ingreso para 4000/8000/8080.
- **Solución:** re-correr `generate_infra.py --run --only network` para que `ensure_security_groups()` sincronice las reglas (añade las que faltan, **revoca las que sobran**).

## `pytest` falla sin credenciales de AWS/PostgreSQL

- No debería. `tests/test_aws_network.py` usa `moto` (sin AWS real); `tests/test_infra_state.py` y `tests/test_nginx_smoke.py` se **saltan** (no fallan) si PostgreSQL/Docker no están disponibles. Si ves un fallo duro (no un skip) en cualquiera de estos, es un bug real — repórtalo con el traceback completo.
