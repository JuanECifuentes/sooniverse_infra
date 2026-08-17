# 04. Destrucción

## 1. Comando

```bash
python scripts/destroy_infra.py --dry-run                       # imprime el plan, no toca nada
python scripts/destroy_infra.py --yes                           # destruye todo (confirmación)
python scripts/destroy_infra.py --only network --yes            # solo la capa de red (asume sky down ya hecho)
python scripts/destroy_infra.py --config clients/acme/config_global.yaml --dry-run
python scripts/destroy_infra.py --scan-orphans                  # barrido de huérfanos en la región
python scripts/destroy_infra.py --scan-orphans --purge-orphans --yes
```

Sin `--yes`, `destroy_infra.py` pide escribir el `cliente.id` por teclado antes de proceder (`confirm_destructive_action()`, `scripts/destroy_infra.py:82`); `--dry-run` nunca pide confirmación porque no muta nada.

## 2. Orden estricto (por qué ese orden)

```
1. sky down de cada clúster worker      -- primero: sin el gateway como bastion,
                                            SkyPilot pierde el SSH a instancias
                                            sin IP pública, así que hay que
                                            bajarlas MIENTRAS el bastion sigue vivo.
2. sky down del clúster gateway
3. AwsNetworkManager.destroy():
   a. Security Groups (revocar TODAS las reglas primero, luego borrar)
                                         -- las reglas SG->SG crean dependencias
                                            cruzadas entre sg-gateway y sg-workers;
                                            hay que romperlas antes de poder
                                            borrar cualquiera de los dos.
   b. VPC Endpoints
   c. NAT Gateways (+ esperar 'nat_gateway_deleted')
                                         -- borrar antes que las EIP: una EIP
                                            asociada a un NAT no se puede liberar.
   d. Elastic IPs (release_address)      -- EL PASO QUE MÁS SE OLVIDA A MANO
                                            y el que sigue cobrando si se saltea.
   e. Route tables (desasociar no-main, luego borrar)
   f. Internet Gateway (detach + delete)
   g. Subredes
   h. VPC
```

Es exactamente el inverso del orden de creación en `docs/02_RED_AWS.md`. `AwsNetworkManager.plan_destroy()` (`scripts/aws_network.py:771`) lee `resources_in_delete_order()` de PostgreSQL (ordenado por la columna `delete_order`, que cada `ensure_*` fija según la constante `DELETE_ORDER` al crear el recurso), así que el orden vive en datos, no en código repetido.

## 3. Qué se borra y qué NO

**Se borra** (si managed_by_us=True Y los tags AWS coinciden con el deployment_id — las DOS condiciones del mecanismo de propiedad, ver `docs/00_ARQUITECTURA.md` §4.5):
- Todo lo listado en la tabla de `docs/02_RED_AWS.md`.

**NUNCA se borra:**
- La base de datos PostgreSQL ni el esquema `sooniverse` — el histórico de métricas, API Keys y auditoría sobrevive a la destrucción de la infraestructura. `destroy_infra.py` no tiene ningún código que toque `DROP SCHEMA`/`DROP TABLE`.
- **Desde esta iteración, esto incluye usuarios y chats de Open WebUI**: al vivir en `sooniverse.*` (Postgres) en vez de en un volumen Docker local (ver `docs/00_ARQUITECTURA.md` §4.7 y `docs/03_ESTADO_Y_BD.md` §7), cuentas, conversaciones, modelos configurados y capacidades sondeadas (`sooniverse.model_capability`) **sobreviven** a un `destroy_infra.py --yes` y a la recreación del Gateway. Es un cambio de comportamiento real respecto a la versión anterior (SQLite efímero en `webui_data`, que sí se perdía con la instancia).
- **Lo que SÍ sigue muriendo con la instancia**: el volumen Docker `webui_data` (`docker_images/gateway/docker-compose.yml`) — ficheros subidos por los usuarios y el vector store local (Chroma) de RAG, que no son relacionales y nunca se migraron a Postgres (ver `docker_images/openwebui/README.md`).
- Recursos de otro `deployment_id` (incluso si comparten región/cuenta).
- Recursos sin nuestros tags (`sooniverse:managed=true`).
- La VPC por defecto de la cuenta (`DefaultVpcGuardError`, `scripts/aws_network.py:87` — guarda explícita en `ensure_vpc()`).
- Las claves SSH que SkyPilot genera (`~/.sky/generated/ssh-keys/*`) — son de ámbito de cuenta/usuario local, compartidas entre despliegues.
- Recursos con `managed_by_us=False` (modo `adopt_existing`, VPC creada a mano) — se omiten con `[DESTROY] Omitido (managed_by_us=False)`, salvo que se pase `--force` (documentado como "solo para depuración, usar con cuidado").

## 4. Manejo de fallos parciales

`AwsNetworkManager.destroy()` (`scripts/aws_network.py:820`) **continúa** aunque un recurso individual falle: cada fallo se agrega a `DestroyReport.failed` con el código de error de AWS (p.ej. `DependencyViolation`) y una sugerencia de comando de diagnóstico (`aws ec2 describe-network-interfaces --filters Name=vpc-id,Values=<vpc-id>`). Al final:

- `report.succeeded` — lo que sí se borró.
- `report.failed` — lo que falló, con el motivo.
- `report.skipped_not_ours` — lo que se omitió por el mecanismo de propiedad.
- `report.manual_actions_required` — comandos AWS CLI exactos para resolver a mano.

Si `report.ok` (sin fallos), el `deployment.status` pasa a `destroyed` y se cierra. Si hubo fallos, queda en `degraded` con `last_error` — **no queda "colgado" en `destroying`**.

## 5. `--scan-orphans` / `--purge-orphans`

`scan_orphans()` (`scripts/destroy_infra.py:125`) es el seguro contra estados corruptos: compara **todos** los recursos `tag:sooniverse:managed=true` de la región (no solo los de un `deployment_id`) contra `sooniverse.infra_resource` de **cualquier** despliegue no borrado. Un recurso es huérfano si:

- No aparece registrado en ningún despliegue no-destruido, **o**
- Aparece registrado, pero su despliegue ya está `destroyed`/`error` (es decir, el destroy se saltó ese recurso en su momento).

`--purge-orphans --yes` los borra en el mismo orden inverso de `DELETE_ORDER`, con la misma guarda anti-VPC-default.

## 6. Checklist de verificación de costes cero

Tras `destroy_infra.py --yes` con `report.ok == True`, verificar manualmente (o vía `verify`, ver más abajo):

```bash
aws ec2 describe-addresses --filters Name=tag:sooniverse:managed,Values=true --region <region>
# -> debe devolver [] para el deployment_id destruido

aws ec2 describe-nat-gateways --filter Name=tag:sooniverse:managed,Values=true --region <region>
# -> los del deployment_id destruido deben aparecer State=deleted (o no aparecer)

aws ec2 describe-vpcs --filters Name=tag:sooniverse:managed,Values=true --region <region>
# -> no debe aparecer la VPC de ese deployment_id

python scripts/list_deployments.py
# -> ese cliente/entorno debe figurar como 'destroyed', 0 recursos activos
```

Si algo sigue apareciendo: revisar `report.manual_actions_required` de la corrida de destroy, o correr `--scan-orphans`.
