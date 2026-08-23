#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - SkyPilot Multi-Node Generator & Provisioner (FASE 1)
==============================================================================
Lee 'config_global.yaml', valida el contrato y genera la topología distribuida:

  ┌───────────────────────────── VPC ──────────────────────────────┐
  │  Subred pública                    Subred privada               │
  │  ┌──────────────────────┐          ┌────────────────────────┐   │
  │  │ NODO GATEWAY (1)     │          │ WORKERS vLLM (N)       │   │
  │  │  LiteLLM  :4000      │─────────▶│  vllm :8007  (GPU)     │   │
  │  │  OpenWebUI:8080/80   │  interno │  sin IP pública        │   │
  │  │  Django   :8000      │          │  SSH vía bastion       │   │
  │  └──────────▲───────────┘          └────────────────────────┘   │
  └─────────────┼──────────────────────────────────────────────────┘
                │ público (80 / 4000 / 8000 / 8080)

Artefactos generados:
  .sky_generated.gateway.yaml          -> tarea SkyPilot del Nodo Gateway
  .sky_generated.worker-<id>.yaml      -> una tarea por workload (num_nodes = replicas)
  .sky_config_workers.yaml             -> config de cliente SkyPilot (VPC + IPs internas + bastion)
"""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

CLIENT_ID_RE = re.compile(r"^[a-z0-9-]{1,20}$")

try:
    import yaml
except ImportError:
    print("[ERROR] La librería 'pyyaml' no está instalada. Ejecuta: pip install pyyaml")
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent

GATEWAY_MANIFEST = ".sky_generated.gateway.yaml"
WORKER_MANIFEST_FMT = ".sky_generated.worker-{wl_id}.yaml"
SKY_GATEWAY_CONFIG = ".sky_config_gateway.yaml"
SKY_WORKERS_CONFIG = ".sky_config_workers.yaml"
ENDPOINTS_CACHE = ".sooniverse_endpoints.json"
CAPACITY_CACHE = ".sooniverse_capacity.json"
DEFAULT_CONFIG_PATH = REPO_ROOT / "config_global.yaml"

# Planificador de vLLM. El default histórico de docker_images/qwen3.5/entrypoint.sh
# era max_num_seqs=2, lo que limitaba cada worker a DOS peticiones concurrentes
# por mucha VRAM libre que hubiera -era, por sí solo, el techo de capacidad del
# sistema-. Estos defaults se aplican cuando el workload no declara
# 'concurrencia'; el razonamiento del valor está en config_global.yaml.
DEFAULT_MAX_NUM_SEQS = 16
DEFAULT_MAX_NUM_BATCHED_TOKENS = 8192

# Rampa por defecto del benchmark de capacidad (sección 'capacidad').
DEFAULT_NIVELES_CONCURRENCIA = [1, 2, 4, 8, 16]


def artifacts_dir_for(config_path: Path, config: Dict[str, Any]) -> Path:
    """Directorio donde se escriben los manifiestos SkyPilot/config de bastion de
    ESTE cliente (Fase 6, multi-cliente): `.artifacts/<cliente.id>-<entorno>/`.

    Compatibilidad hacia atrás: si `config_path` es el `config_global.yaml` de
    la raíz del repo (el único punto de entrada antes de la Fase 6), los
    artefactos se siguen escribiendo en la raíz, sin subcarpeta -así una
    instalación existente de un solo cliente no cambia de comportamiento.
    Cualquier otro `--config` (p.ej. `clients/<id>/config_global.yaml`) recibe
    su propio directorio, para que dos clientes en la misma cuenta AWS no se
    pisen los manifiestos ni el bastion.
    """
    try:
        is_default_root_config = config_path.resolve() == DEFAULT_CONFIG_PATH.resolve()
    except OSError:
        is_default_root_config = False

    if is_default_root_config:
        return REPO_ROOT

    cliente = config["cliente"]
    per_client_dir = REPO_ROOT / ".artifacts" / f"{cliente['id']}-{cliente['entorno']}"
    per_client_dir.mkdir(parents=True, exist_ok=True)
    return per_client_dir

REMOTE_ROOT = "/home/ubuntu/sooniverse_infra"


class ConfigValidationError(Exception):
    """Excepción personalizada para errores de validación en config_global.yaml."""


# =============================================================================
# VALIDACIÓN DEL CONTRATO
# =============================================================================
class ConfigValidator:
    """Validador de esquema y reglas de negocio para la configuración global."""

    ALLOWED_ENTORNOS = {"prod", "dev"}
    ALLOWED_MODOS = {"byoc", "hosted"}
    ALLOWED_TAREAS = {"llm-texto", "embeddings"}
    ALLOWED_LB_STRATEGIES = {
        "latency-based-routing",
        "simple-shuffle",
        "least-busy",
        "usage-based-routing",
        "usage-based-routing-v2",
    }
    ALLOWED_GESTION_RED = {"auto", "existente"}
    ALLOWED_NAT_MODOS = {"single", "per-az", "none"}
    ALLOWED_TLS_MODOS = {"self-signed", "letsencrypt", "acm"}
    # 'segun_capacidades' (default): la verdad observada en sooniverse.model_capability
    # decide. 'activado'/'desactivado': escape manual del operador.
    ALLOWED_OPEN_WEBUI_OVERRIDES = {"segun_capacidades", "activado", "desactivado"}
    # Desde dónde mide el benchmark de capacidad. Los números de un origen NO son
    # comparables con los del otro (fuera de la VPC el RTT del ISP domina el TTFT).
    ALLOWED_BENCH_ORIGENES = {"gateway", "operador"}
    # Tope de cordura para max_num_seqs: por encima, casi seguro es un error de
    # tecleo y el worker moriría por OOM en el arranque.
    MAX_NUM_SEQS_LIMITE = 1024
    # FQDN simple, sin wildcard: usado tanto por 'gateway.dominio.disponibles[].nombre'
    # como (indirectamente) por 'gateway.tls.dominio' una vez derivado.
    _FQDN_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(\.[a-z0-9-]{1,63})+$", re.IGNORECASE)

    @classmethod
    def validate(cls, config: Dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ConfigValidationError("El archivo de configuración debe ser un mapa YAML válido.")

        cls._validate_cliente(config)
        cls._validate_red(config)
        cls._validate_gateway(config)
        cls._validate_base_de_datos(config)
        cls._validate_workloads(config)
        cls._validate_capacidad(config)

    # -- secciones -------------------------------------------------------------
    @classmethod
    def _validate_cliente(cls, config: Dict[str, Any]) -> None:
        cliente = config.get("cliente")
        if not cliente or not isinstance(cliente, dict):
            raise ConfigValidationError("Falta la sección obligatoria 'cliente'.")

        if not cliente.get("id") or not isinstance(cliente["id"], str):
            raise ConfigValidationError("Falta 'cliente.id' o no es una cadena válida.")

        # Todo nombre de recurso AWS/clúster/clave de estado se deriva de este id
        # (sooniverse-<cliente.id>-<entorno>-...): debe ser válido como componente
        # de nombre de SG (<=255, sin espacios) y de tag Name sin sorpresas.
        if not CLIENT_ID_RE.match(cliente["id"]):
            raise ConfigValidationError(
                f"'cliente.id' inválido: '{cliente['id']}'. Debe ser minúsculas, [a-z0-9-], "
                "máx. 20 caracteres (ej. 'acme', 'globex-corp')."
            )

        if cliente.get("entorno") not in cls.ALLOWED_ENTORNOS:
            raise ConfigValidationError(
                f"'cliente.entorno' inválido: '{cliente.get('entorno')}'. Permitidos: {cls.ALLOWED_ENTORNOS}"
            )

        if cliente.get("modo") not in cls.ALLOWED_MODOS:
            raise ConfigValidationError(
                f"'cliente.modo' inválido: '{cliente.get('modo')}'. Permitidos: {cls.ALLOWED_MODOS}"
            )

    @classmethod
    def _validate_red(cls, config: Dict[str, Any]) -> None:
        red = config.get("red_y_aislamiento")
        if not red or not isinstance(red, dict):
            raise ConfigValidationError("Falta la sección obligatoria 'red_y_aislamiento'.")

        if not red.get("region"):
            raise ConfigValidationError("Falta 'red_y_aislamiento.region'.")

        tags = red.get("tags_obligatorios")
        if not tags or not isinstance(tags, dict):
            raise ConfigValidationError("Falta 'red_y_aislamiento.tags_obligatorios'.")

        privada = red.get("workers_en_subred_privada", True)
        if not isinstance(privada, bool):
            raise ConfigValidationError("'red_y_aislamiento.workers_en_subred_privada' debe ser booleano.")

        gestion_red = red.get("gestion_red", "auto")
        if gestion_red not in cls.ALLOWED_GESTION_RED:
            raise ConfigValidationError(
                f"'red_y_aislamiento.gestion_red' inválido: '{gestion_red}'. Permitidos: {cls.ALLOWED_GESTION_RED}"
            )

        if gestion_red == "existente":
            # Modo legado: la VPC/SGs ya existen y se referencian por nombre.
            if privada and not red.get("vpc_name"):
                print(
                    "[WARNING] 'workers_en_subred_privada: true' sin 'vpc_name'. SkyPilot usará la VPC por "
                    "defecto y sus subredes; verifica que exista una subred sin ruta directa a Internet "
                    "Gateway y con NAT, o los workers no podrán descargar el modelo."
                )
            return

        # gestion_red == "auto": AwsNetworkManager crea la VPC; validar el resto del contrato.
        cls._validate_red_auto(red)

    @classmethod
    def _validate_red_auto(cls, red: Dict[str, Any]) -> None:
        vpc_cidr_raw = red.get("vpc_cidr")
        if not vpc_cidr_raw:
            raise ConfigValidationError("Falta 'red_y_aislamiento.vpc_cidr' (requerido en modo 'auto').")
        try:
            vpc_net = ipaddress.ip_network(vpc_cidr_raw, strict=True)
        except ValueError as exc:
            raise ConfigValidationError(f"'red_y_aislamiento.vpc_cidr' inválido: {exc}") from exc

        azs = red.get("azs", 1)
        if not isinstance(azs, int) or azs < 1:
            raise ConfigValidationError("'red_y_aislamiento.azs' debe ser un entero >= 1.")

        nat = red.get("nat_gateway") or {}
        if not isinstance(nat, dict):
            raise ConfigValidationError("'red_y_aislamiento.nat_gateway' debe ser un mapa.")
        nat_modo = nat.get("modo", "single")
        if nat_modo not in cls.ALLOWED_NAT_MODOS:
            raise ConfigValidationError(
                f"'red_y_aislamiento.nat_gateway.modo' inválido: '{nat_modo}'. Permitidos: {cls.ALLOWED_NAT_MODOS}"
            )

        privada = red.get("workers_en_subred_privada", True)
        endpoints = red.get("vpc_endpoints") or {}
        if privada and nat_modo == "none" and not endpoints.get("s3"):
            raise ConfigValidationError(
                "'workers_en_subred_privada: true' con 'nat_gateway.modo: none' requiere al menos "
                "'vpc_endpoints.s3: true'; de lo contrario los workers no tendrán salida a internet "
                "para descargar el modelo ni acceder a otros servicios AWS."
            )

        # Validar CIDRs explícitos de subredes (si el operador los fija a mano en vez de dejar
        # el cálculo automático determinista).
        subredes = red.get("subredes") or {}
        for clave in ("publicas", "privadas"):
            cidrs = subredes.get(clave)
            if cidrs is None:
                continue
            if not isinstance(cidrs, list) or not cidrs:
                raise ConfigValidationError(f"'red_y_aislamiento.subredes.{clave}' debe ser una lista de CIDR.")
            for cidr in cidrs:
                try:
                    subnet = ipaddress.ip_network(cidr, strict=True)
                except ValueError as exc:
                    raise ConfigValidationError(
                        f"'red_y_aislamiento.subredes.{clave}' contiene un CIDR inválido '{cidr}': {exc}"
                    ) from exc
                if not subnet.subnet_of(vpc_net):
                    raise ConfigValidationError(
                        f"'red_y_aislamiento.subredes.{clave}': el CIDR '{cidr}' no está contenido en "
                        f"'vpc_cidr' ({vpc_cidr_raw})."
                    )

        all_subnet_cidrs = list(subredes.get("publicas") or []) + list(subredes.get("privadas") or [])
        seen_networks = []
        for cidr in all_subnet_cidrs:
            net = ipaddress.ip_network(cidr, strict=True)
            for other in seen_networks:
                if net.overlaps(other):
                    raise ConfigValidationError(
                        f"'red_y_aislamiento.subredes': los CIDR '{cidr}' y '{other}' se solapan."
                    )
            seen_networks.append(net)

    @classmethod
    def _validate_gateway(cls, config: Dict[str, Any]) -> None:
        gw = config.get("gateway")
        if not gw or not isinstance(gw, dict):
            raise ConfigValidationError("Falta la sección obligatoria 'gateway' (Fase 1).")

        if not isinstance(gw.get("habilitado", True), bool):
            raise ConfigValidationError("'gateway.habilitado' debe ser booleano.")

        puertos = gw.get("puertos_publicos")
        if not puertos or not isinstance(puertos, list) or not all(isinstance(p, int) for p in puertos):
            raise ConfigValidationError("'gateway.puertos_publicos' debe ser una lista de enteros.")

        # El login único del clúster es SSO por cabecera de confianza: nginx
        # inyecta la identidad ya verificada por Django hacia Open WebUI
        # (WEBUI_AUTH_TRUSTED_EMAIL_HEADER), y Open WebUI confía en ella sin
        # volver a pedir contraseña. Si el puerto 8080 quedara publicado
        # directo (exponer_puertos_directos: true), cualquiera podría
        # alcanzarlo saltándose nginx e inyectar esa cabecera él mismo,
        # suplantando a cualquier usuario -ver docker_images/openwebui/README.md.
        if gw.get("exponer_puertos_directos", False):
            raise ConfigValidationError(
                "'gateway.exponer_puertos_directos: true' es incompatible con el login único "
                "por SSO (cabecera de confianza) del clúster: expondría el puerto 8080 de Open "
                "WebUI sin pasar por nginx, permitiendo suplantar a cualquier usuario. Déjalo en "
                "'false' (el default)."
            )

        strategy = gw.get("load_balancing_strategy", "latency-based-routing")
        if strategy not in cls.ALLOWED_LB_STRATEGIES:
            raise ConfigValidationError(
                f"'gateway.load_balancing_strategy' inválida: '{strategy}'. "
                f"Permitidas: {cls.ALLOWED_LB_STRATEGIES}"
            )

        tls = gw.get("tls") or {}
        if not isinstance(tls, dict):
            raise ConfigValidationError("'gateway.tls' debe ser un mapa.")
        if tls.get("habilitado", False):
            modo_tls = tls.get("modo", "self-signed")
            if modo_tls not in cls.ALLOWED_TLS_MODOS:
                raise ConfigValidationError(
                    f"'gateway.tls.modo' inválido: '{modo_tls}'. Permitidos: {cls.ALLOWED_TLS_MODOS}"
                )
            if modo_tls == "acm":
                raise ConfigValidationError(
                    "'gateway.tls.modo: acm' no está implementado todavía (requiere un ALB); "
                    "usa 'self-signed' o 'letsencrypt', o pon 'tls.habilitado: false'."
                )
            if not tls.get("dominio"):
                raise ConfigValidationError(
                    "'gateway.tls.dominio' es obligatorio cuando 'gateway.tls.habilitado: true' "
                    "(se usa como CN del certificado y server_name de nginx)."
                )
            if modo_tls == "letsencrypt" and not tls.get("email_acme"):
                raise ConfigValidationError(
                    "'gateway.tls.email_acme' es obligatorio con 'gateway.tls.modo: letsencrypt' "
                    "(Let's Encrypt lo usa solo para avisos de caducidad). Se rellena solo si "
                    "usas el catálogo 'gateway.dominio.disponibles[].email_acme' -ver Manual_Dominio_AWS.md."
                )

        owui = gw.get("open_webui") or {}
        if not isinstance(owui, dict):
            raise ConfigValidationError("'gateway.open_webui' debe ser un mapa.")
        for campo in ("tareas_automaticas", "code_interpreter"):
            valor = owui.get(campo, "segun_capacidades")
            if valor not in cls.ALLOWED_OPEN_WEBUI_OVERRIDES:
                raise ConfigValidationError(
                    f"'gateway.open_webui.{campo}' inválido: '{valor}'. "
                    f"Permitidos: {cls.ALLOWED_OPEN_WEBUI_OVERRIDES}"
                )

        cls._validate_dominio(config)

    @classmethod
    def _validate_dominio(cls, config: Dict[str, Any]) -> None:
        """Valida 'gateway.dominio' (catálogo + selector) y sus dos reglas cruzadas.
        No deriva nada aquí -la derivación hacia 'gateway.tls' ocurre en
        load_config(), DESPUÉS de que esta validación pase, para que el resto del
        código (nginx, SG, resources.ports, report) siga leyendo solo 'tls.*'."""
        gw = config.get("gateway", {})
        dominio_cfg = gw.get("dominio") or {}
        if not isinstance(dominio_cfg, dict):
            raise ConfigValidationError("'gateway.dominio' debe ser un mapa.")
        if not dominio_cfg.get("habilitado", False):
            return

        disponibles = dominio_cfg.get("disponibles") or []
        if not isinstance(disponibles, list) or not disponibles:
            raise ConfigValidationError(
                "'gateway.dominio.disponibles' debe tener al menos un dominio cuando "
                "'gateway.dominio.habilitado: true' (ver Manual_Dominio_AWS.md)."
            )

        nombres = set()
        for entrada in disponibles:
            if not isinstance(entrada, dict) or not entrada.get("nombre"):
                raise ConfigValidationError(
                    "Cada entrada de 'gateway.dominio.disponibles' debe tener 'nombre'."
                )
            nombre = entrada["nombre"]
            if not cls._FQDN_RE.match(nombre):
                raise ConfigValidationError(
                    f"'gateway.dominio.disponibles': '{nombre}' no es un nombre de dominio válido "
                    "(sin wildcard, ej. 'ia.acme.com')."
                )
            email = entrada.get("email_acme")
            if not email or "@" not in email:
                raise ConfigValidationError(
                    f"'gateway.dominio.disponibles': el dominio '{nombre}' necesita 'email_acme' "
                    "válido (avisos de caducidad de Let's Encrypt)."
                )
            if nombre in nombres:
                raise ConfigValidationError(
                    f"'gateway.dominio.disponibles': el dominio '{nombre}' está repetido."
                )
            nombres.add(nombre)

        seleccionado = dominio_cfg.get("seleccionado")
        if not seleccionado or seleccionado not in nombres:
            raise ConfigValidationError(
                f"'gateway.dominio.seleccionado' ('{seleccionado}') debe coincidir con el 'nombre' "
                f"de una entrada de 'gateway.dominio.disponibles': {sorted(nombres)}"
            )

        # Regla cruzada: HTTP-01 exige que Let's Encrypt pueda alcanzar el puerto 80
        # desde cualquier IP -sus validadores no viven en un rango predecible.
        red = config.get("red_y_aislamiento", {}) or {}
        cidr_publico = red.get("cidr_permitido_gateway", "0.0.0.0/0")
        if cidr_publico != "0.0.0.0/0":
            raise ConfigValidationError(
                "'gateway.dominio.habilitado: true' exige "
                "'red_y_aislamiento.cidr_permitido_gateway: \"0.0.0.0/0\"': los validadores HTTP-01 "
                "de Let's Encrypt no viven en un rango de IP predecible. Ver Manual_Dominio_AWS.md §8."
            )

    @classmethod
    def _validate_base_de_datos(cls, config: Dict[str, Any]) -> None:
        db = config.get("base_de_datos")
        if not db or not isinstance(db, dict):
            raise ConfigValidationError("Falta la sección obligatoria 'base_de_datos'.")

        if "AUTO_INIT_DB" not in db:
            raise ConfigValidationError("Falta el flag 'base_de_datos.AUTO_INIT_DB' (true | false).")

        if not isinstance(db["AUTO_INIT_DB"], bool):
            raise ConfigValidationError("'base_de_datos.AUTO_INIT_DB' debe ser booleano (true | false).")

        schema_dir = db.get("schema_dir", "database")
        schema_path = REPO_ROOT / schema_dir
        if not schema_path.is_dir() or not any(schema_path.glob("*.sql")):
            raise ConfigValidationError(
                f"'base_de_datos.schema_dir' no existe o no contiene .sql: {schema_dir}"
            )

    @classmethod
    def _validate_workloads(cls, config: Dict[str, Any]) -> None:
        workloads = config.get("workloads")
        if not workloads or not isinstance(workloads, list):
            raise ConfigValidationError("Falta la sección 'workloads' o no contiene elementos.")

        vistos = set()
        for idx, wl in enumerate(workloads):
            if not isinstance(wl, dict):
                raise ConfigValidationError(f"El workload #{idx + 1} no es un objeto válido.")

            wl_id = wl.get("id")
            if not wl_id:
                raise ConfigValidationError(f"El workload #{idx + 1} requiere un 'id'.")
            if wl_id in vistos:
                raise ConfigValidationError(f"'workloads[].id' duplicado: '{wl_id}'.")
            vistos.add(wl_id)

            if wl.get("tipo_tarea") not in cls.ALLOWED_TAREAS:
                raise ConfigValidationError(
                    f"Workload '{wl_id}': 'tipo_tarea' inválido '{wl.get('tipo_tarea')}'. "
                    f"Permitidos: {cls.ALLOWED_TAREAS}"
                )

            if not wl.get("accelerator"):
                raise ConfigValidationError(f"Workload '{wl_id}': Requiere el campo 'accelerator'.")

            cantidad_gpus = wl.get("cantidad_gpus", 0)
            if not isinstance(cantidad_gpus, int) or cantidad_gpus <= 0:
                raise ConfigValidationError(
                    f"Workload '{wl_id}': 'cantidad_gpus' debe ser un entero positivo (> 0)."
                )

            replicas = wl.get("replicas", 1)
            if not isinstance(replicas, int) or replicas <= 0:
                raise ConfigValidationError(
                    f"Workload '{wl_id}': 'replicas' debe ser un entero positivo (> 0)."
                )

            puerto = wl.get("puerto")
            if not puerto or not isinstance(puerto, int):
                raise ConfigValidationError(f"Workload '{wl_id}': Debe especificar un 'puerto' entero.")

            capacidades = wl.get("capacidades", {})
            if capacidades:
                if not isinstance(capacidades, dict):
                    raise ConfigValidationError(f"Workload '{wl_id}': 'capacidades' debe ser un objeto.")
                for campo in ("vision", "tool_calling"):
                    if campo in capacidades and not isinstance(capacidades[campo], bool):
                        raise ConfigValidationError(
                            f"Workload '{wl_id}': 'capacidades.{campo}' debe ser booleano."
                        )
                if capacidades.get("tool_calling") and not capacidades.get("tool_call_parser"):
                    raise ConfigValidationError(
                        f"Workload '{wl_id}': 'capacidades.tool_calling: true' requiere "
                        "'capacidades.tool_call_parser' (ej. 'hermes', 'qwen')."
                    )

            cls._validate_concurrencia(wl_id, wl)

    @classmethod
    def _validate_concurrencia(cls, wl_id: str, wl: Dict[str, Any]) -> None:
        """Planificador de vLLM. Sección opcional: si falta, build_worker() aplica
        los defaults (16 / 8192)."""
        conc = wl.get("concurrencia")
        if conc is None:
            return
        if not isinstance(conc, dict):
            raise ConfigValidationError(f"Workload '{wl_id}': 'concurrencia' debe ser un objeto.")

        seqs = conc.get("max_num_seqs", DEFAULT_MAX_NUM_SEQS)
        if not isinstance(seqs, int) or isinstance(seqs, bool) or not (1 <= seqs <= cls.MAX_NUM_SEQS_LIMITE):
            raise ConfigValidationError(
                f"Workload '{wl_id}': 'concurrencia.max_num_seqs' debe ser un entero entre 1 y "
                f"{cls.MAX_NUM_SEQS_LIMITE} (recibido: {seqs!r})."
            )

        batched = conc.get("max_num_batched_tokens", DEFAULT_MAX_NUM_BATCHED_TOKENS)
        if not isinstance(batched, int) or isinstance(batched, bool) or batched < 1024:
            raise ConfigValidationError(
                f"Workload '{wl_id}': 'concurrencia.max_num_batched_tokens' debe ser un entero "
                f">= 1024 (recibido: {batched!r})."
            )

        # Con menos tokens por paso que secuencias en vuelo, al menos una secuencia
        # no puede ni decodificar un token por paso: vLLM se queda en vilo.
        if batched < seqs:
            raise ConfigValidationError(
                f"Workload '{wl_id}': 'concurrencia.max_num_batched_tokens' ({batched}) no puede "
                f"ser menor que 'concurrencia.max_num_seqs' ({seqs}): al menos una secuencia no "
                "podría decodificar ni un token por paso del planificador."
            )

    @classmethod
    def _validate_capacidad(cls, config: Dict[str, Any]) -> None:
        """Benchmark de capacidad. Sección opcional y de nivel superior: si falta,
        la fase usa los defaults de scripts/benchmark_capacity.py."""
        cap = config.get("capacidad")
        if cap is None:
            return
        if not isinstance(cap, dict):
            raise ConfigValidationError("'capacidad' debe ser un mapa.")

        if not isinstance(cap.get("habilitado", True), bool):
            raise ConfigValidationError("'capacidad.habilitado' debe ser booleano.")

        niveles = cap.get("niveles_concurrencia", DEFAULT_NIVELES_CONCURRENCIA)
        if (not isinstance(niveles, list) or not niveles
                or any(not isinstance(n, int) or isinstance(n, bool) or n <= 0 for n in niveles)
                or any(b <= a for a, b in zip(niveles, niveles[1:]))):
            raise ConfigValidationError(
                "'capacidad.niveles_concurrencia' debe ser una lista de enteros positivos "
                f"estrictamente creciente (ej. [1, 2, 4, 8, 16]). Recibido: {niveles!r}"
            )
        if max(niveles) > 256:
            raise ConfigValidationError(
                f"'capacidad.niveles_concurrencia': el nivel máximo ({max(niveles)}) supera 256. "
                "Una rampa así deja de medir la GPU y pasa a medir la cola."
            )

        enteros_positivos = {
            "segundos_por_nivel": 20,
            "prompt_tokens_objetivo": 512,
            "max_tokens": 128,
            "presupuesto_segundos": 240,
        }
        for campo, defecto in enteros_positivos.items():
            valor = cap.get(campo, defecto)
            if not isinstance(valor, int) or isinstance(valor, bool) or valor <= 0:
                raise ConfigValidationError(
                    f"'capacidad.{campo}' debe ser un entero positivo (recibido: {valor!r})."
                )

        warmup = cap.get("warmup_segundos", 10)
        if not isinstance(warmup, int) or isinstance(warmup, bool) or warmup < 0:
            raise ConfigValidationError(
                f"'capacidad.warmup_segundos' debe ser un entero >= 0 (recibido: {warmup!r})."
            )

        for campo, defecto in (("umbral_p95_degradacion", 3.0),
                               ("ganancia_minima_throughput_pct", 10.0),
                               ("factor_usuarios_por_slot", 8)):
            valor = cap.get(campo, defecto)
            if isinstance(valor, bool) or not isinstance(valor, (int, float)) or valor <= 0:
                raise ConfigValidationError(
                    f"'capacidad.{campo}' debe ser un número positivo (recibido: {valor!r})."
                )

        error_pct = cap.get("umbral_error_pct", 5.0)
        if isinstance(error_pct, bool) or not isinstance(error_pct, (int, float)) \
                or not (0 < error_pct <= 100):
            raise ConfigValidationError(
                f"'capacidad.umbral_error_pct' debe estar en (0, 100] (recibido: {error_pct!r})."
            )

        # Regla cruzada: hace que "rampa acotada" sea una GARANTÍA del contrato y
        # no una intención. Sin esto, una rampa de 10 niveles x 60s se comería
        # 10 minutos de GPU en cada despliegue sin que nadie lo notara.
        necesario = len(niveles) * cap.get("segundos_por_nivel", 20) + warmup
        presupuesto = cap.get("presupuesto_segundos", 240)
        if necesario > presupuesto:
            raise ConfigValidationError(
                f"'capacidad': la rampa configurada necesita ~{necesario}s pero "
                f"'presupuesto_segundos' es {presupuesto}s. Reduce 'niveles_concurrencia' o "
                "'segundos_por_nivel', o sube el presupuesto."
            )

        origen = cap.get("origen", "gateway")
        if origen not in cls.ALLOWED_BENCH_ORIGENES:
            raise ConfigValidationError(
                f"'capacidad.origen' inválido: '{origen}'. Permitidos: {cls.ALLOWED_BENCH_ORIGENES}"
            )


# =============================================================================
# SCRIPTS REMOTOS
# =============================================================================
GPU_SETUP_SCRIPT = """
set -euo pipefail

# A. Dependencias esenciales, driver estable y utilidad modprobe
sudo apt-get update && sudo apt-get install -y ubuntu-drivers-common build-essential \
  nvidia-driver-550-server nvidia-utils-550-server nvidia-modprobe

# B. Carga manual de módulos en caliente (evita reinicio de la instancia)
sudo modprobe nvidia
sudo modprobe nvidia-uvm
sudo nvidia-modprobe -u -c=0

# C. Docker Engine
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker ubuntu
fi

# D. NVIDIA Container Toolkit (expone la GPU a Docker)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit

# E. Runtime NVIDIA por defecto en Docker
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
sudo chmod 666 /var/run/docker.sock

# F. Verificación de salud
nvidia-smi

# G. Persistencia del cache de modelos
mkdir -p {remote_root}/docker_images/{modelo}/hf_cache
"""

WORKER_RUN_SCRIPT = """
set -euo pipefail
echo "===> [WORKER rank ${{SKYPILOT_NODE_RANK:-0}}] Desplegando vLLM ({wl_id})"
cd {remote_root}/docker_images/{modelo}

export MODEL_NAME="${{MODEL_NAME}}"
export GPU_MEMORY_UTILIZATION="${{GPU_MEMORY_UTILIZATION}}"
export MAX_MODEL_LEN="${{MAX_MODEL_LEN}}"
export ENABLE_VISION="${{ENABLE_VISION}}"
export ENABLE_TOOL_CALLING="${{ENABLE_TOOL_CALLING}}"
export TOOL_CALL_PARSER="${{TOOL_CALL_PARSER}}"
# Sin estos dos export, los envs que SkyPilot inyecta en la sesión no llegan a
# 'docker compose' y vLLM arrancaría con los defaults del entrypoint (que eran
# max_num_seqs=2: dos peticiones concurrentes por worker).
export MAX_NUM_SEQS="${{MAX_NUM_SEQS}}"
export MAX_NUM_BATCHED_TOKENS="${{MAX_NUM_BATCHED_TOKENS}}"

sudo docker compose up -d
sudo docker compose ps
echo "===> vLLM con max_num_seqs=${{MAX_NUM_SEQS}} max_num_batched_tokens=${{MAX_NUM_BATCHED_TOKENS}}"

# El worker solo escucha en la red interna de la VPC; LiteLLM en el Gateway lo consume.
SELF_IP=$(hostname -I | awk '{{print $1}}')

# Marcadores parseables por scripts/sync_endpoints.py para descubrir el pool.
echo "SOONIVERSE_WORKER_READY={wl_id}|${{SELF_IP}}|{puerto}"
if [ "${{SKYPILOT_NODE_RANK:-0}}" = "0" ]; then
    echo "SOONIVERSE_NODE_IPS=$(echo "${{SKYPILOT_NODE_IPS:-$SELF_IP}}" | tr '\\n' ',' | sed 's/,$//')"
fi
echo "===> Worker listo en ${{SELF_IP}}:{puerto}"
"""

GATEWAY_SETUP_SCRIPT = """
set -euo pipefail

# A. Dependencias base (sin GPU: el Gateway es CPU-only)
sudo apt-get update
sudo apt-get install -y curl jq python3-pip postgresql-client

# B. Docker Engine + Compose plugin
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker ubuntu
fi
sudo systemctl enable --now docker
sudo chmod 666 /var/run/docker.sock

# C. Dependencias Python del orquestador local (db_setup / render de config)
pip3 install --break-system-packages --quiet "psycopg2-binary>=2.9" "pyyaml>=6.0" || \
  pip3 install --quiet "psycopg2-binary>=2.9" "pyyaml>=6.0"

# D. El Gateway actúa como bastion SSH hacia los workers privados
sudo sed -i 's/^#*AllowTcpForwarding.*/AllowTcpForwarding yes/' /etc/ssh/sshd_config
sudo systemctl reload ssh || sudo systemctl reload sshd || true

mkdir -p {remote_root}/docker_images/gateway/data
{tls_setup}
echo "===> Gateway aprovisionado."
"""

# Certificado autofirmado (modo 'self-signed', el único implementado hoy).
# Idempotente: no regenera si ya existe. 'letsencrypt' (certbot en sidecar) y
# 'acm' (requiere ALB) quedan como hook documentado, no implementados.
TLS_SELF_SIGNED_SETUP = """
if [ ! -f {remote_root}/docker_images/gateway/nginx/certs/fullchain.pem ]; then
    echo "===> TLS self-signed: generando certificado ({tls_domain})"
    mkdir -p {remote_root}/docker_images/gateway/nginx/certs
    openssl req -x509 -nodes -days 825 -newkey rsa:2048 \\
        -keyout {remote_root}/docker_images/gateway/nginx/certs/privkey.pem \\
        -out {remote_root}/docker_images/gateway/nginx/certs/fullchain.pem \\
        -subj "/CN={tls_domain}" \\
        -addext "subjectAltName=DNS:{tls_domain}" 2>/dev/null || \\
    openssl req -x509 -nodes -days 825 -newkey rsa:2048 \\
        -keyout {remote_root}/docker_images/gateway/nginx/certs/privkey.pem \\
        -out {remote_root}/docker_images/gateway/nginx/certs/fullchain.pem \\
        -subj "/CN={tls_domain}"
else
    echo "===> TLS self-signed: certificado ya existe, se reutiliza."
fi
"""

# Certificado real (modo 'letsencrypt'). Los certs viven en /opt/sooniverse,
# FUERA de {remote_root}/docker_images/gateway -ese árbol se re-sincroniza en
# cada 'sky launch' (file_mounts) y no es el sitio para depender de que un
# rsync sin --delete no los borre.
#
# certbot corre en modo --standalone: en este punto del 'setup' Docker Compose
# todavía no arrancó (eso pasa en el 'run', después), así que el puerto 80
# está libre y certbot puede quedárselo un instante para el reto HTTP-01.
#
# Si el DNS todavía no resuelve al Gateway (el operador puede no haber creado
# el registro A todavía, ver Manual_Dominio_AWS.md), certbot falla rápido y
# cae al autofirmado de RESPALDO -nginx SIEMPRE debe poder arrancar. La fase
# 'dominio' (después de que el Gateway esté arriba) reintenta la emisión real
# vía webroot en cuanto el DNS resuelva, sin necesitar un redeploy completo.
TLS_LETSENCRYPT_SETUP = """
sudo mkdir -p /opt/sooniverse/letsencrypt /opt/sooniverse/certbot-www
sudo chown -R "$(id -u):$(id -g)" /opt/sooniverse
if [ ! -f /opt/sooniverse/letsencrypt/live/{tls_domain}/fullchain.pem ]; then
    echo "===> TLS letsencrypt: intentando emitir el certificado para {tls_domain} (modo standalone)"
    sudo docker run --rm -p 80:80 \\
        -v /opt/sooniverse/letsencrypt:/etc/letsencrypt \\
        certbot/certbot certonly --standalone --non-interactive --agree-tos \\
        -m {email_acme} -d {tls_domain} --keep-until-expiring {staging_flag} \\
        || echo "===> certbot standalone falló (¿el DNS todavía no resuelve a este Gateway?); se usará un autofirmado de respaldo."
fi
if [ ! -f /opt/sooniverse/letsencrypt/live/{tls_domain}/fullchain.pem ]; then
    echo "===> TLS letsencrypt: generando autofirmado de RESPALDO ({tls_domain}) -se reemplaza solo en cuanto el DNS resuelva (fase 'dominio')"
    mkdir -p /opt/sooniverse/letsencrypt/live/{tls_domain}
    openssl req -x509 -nodes -days 825 -newkey rsa:2048 \\
        -keyout /opt/sooniverse/letsencrypt/live/{tls_domain}/privkey.pem \\
        -out /opt/sooniverse/letsencrypt/live/{tls_domain}/fullchain.pem \\
        -subj "/CN={tls_domain}" \\
        -addext "subjectAltName=DNS:{tls_domain}" 2>/dev/null || \\
    openssl req -x509 -nodes -days 825 -newkey rsa:2048 \\
        -keyout /opt/sooniverse/letsencrypt/live/{tls_domain}/privkey.pem \\
        -out /opt/sooniverse/letsencrypt/live/{tls_domain}/fullchain.pem \\
        -subj "/CN={tls_domain}"
else
    echo "===> TLS letsencrypt: certificado ya existe (real o de respaldo), se reutiliza."
fi
"""

GATEWAY_RUN_SCRIPT = """
set -euo pipefail
cd {remote_root}

echo "===> [GATEWAY] Cliente ${{CLIENTE_ID}} (${{ENTORNO}})"

# ---------------------------------------------------------------------------
# 0. Persistir CLIENTE_ID/ENTORNO en .env (idempotente).
#    SkyPilot solo exporta 'envs:' (esta variable incluida) para ESTE script
#    (setup/run) en el momento de 'sky launch'; una invocación posterior de
#    'docker compose' vía 'sky exec' (p.ej. scripts/sync_openwebui_models.py
#    al recrear open-webui/correr el bootstrap tras la fase 'capabilities')
#    NO hereda ese entorno de shell -confirmado en despliegue real: 'sky exec
#    ... echo $CLIENTE_ID' devuelve vacío-. Sin esto, cualquier servicio que
#    lea ${{CLIENTE_ID:-default}}/${{ENTORNO:-prod}} en una corrida posterior
#    caería silenciosamente a esos defaults aunque el cliente real sea otro
#    -exactamente lo que le pasaba a 'openwebui-bootstrap': consultaba
#    sooniverse.model_capability con client_id='default' en vez de 'acme' y
#    nunca encontraba la fila real, aplicando el fallback fail-closed (todo
#    apagado) en vez de la verdad sondeada.
# ---------------------------------------------------------------------------
sed -i '/^CLIENTE_ID=/d;/^ENTORNO=/d' .env
{{ echo "CLIENTE_ID=${{CLIENTE_ID}}"; echo "ENTORNO=${{ENTORNO}}"; }} >> .env

# ---------------------------------------------------------------------------
# 0.5 Con dominio propio (TLS_ENABLED=true + TLS_DOMAIN no vacío), fija
#     ALLOWED_HOSTS/CSRF_TRUSTED_ORIGINS en .env -sin esto, con HTTPS real
#     TODOS los POST del panel Django fallan por CSRF (Django 4+ exige el
#     origen cualificado con esquema; CSRF_TRUSTED_ORIGINS viene vacío por
#     defecto, ver .env.example). Mismo patrón sed+append que CLIENTE_ID/ENTORNO.
# ---------------------------------------------------------------------------
if [ "${{TLS_ENABLED}}" = "true" ] && [ -n "${{TLS_DOMAIN}}" ]; then
    PUBLIC_IP_PRE="$(curl -s --max-time 5 ifconfig.me || true)"
    sed -i '/^ALLOWED_HOSTS=/d;/^CSRF_TRUSTED_ORIGINS=/d;/^HTTPS_ACTIVO=/d' .env
    {{
        echo "ALLOWED_HOSTS=${{TLS_DOMAIN}},${{PUBLIC_IP_PRE}},localhost"
        echo "CSRF_TRUSTED_ORIGINS=https://${{TLS_DOMAIN}}"
        echo "HTTPS_ACTIVO=true"
    }} >> .env
fi

# ---------------------------------------------------------------------------
# 1. Inicialización opcional de la base de datos (flag AUTO_INIT_DB del contrato)
# ---------------------------------------------------------------------------
if [ "${{AUTO_INIT_DB}}" = "true" ]; then
    echo "===> AUTO_INIT_DB=true -> ingestando {schema_dir}/*.sql (orden lexicográfico)"
    REFRESH_FLAG=""
    if [ "${{AUTO_REFRESH_METRICS}}" = "true" ]; then REFRESH_FLAG="--refresh"; fi
    python3 scripts/db_setup.py --env-file .env --sql-dir {schema_dir} ${{REFRESH_FLAG}}
else
    echo "===> AUTO_INIT_DB=false -> se omite la inicialización automática de la BD."
    echo "     Ejecuta manualmente: python scripts/db_setup.py"
fi

# ---------------------------------------------------------------------------
# 2. Render del config.yaml de LiteLLM con las IPs privadas de los workers
#    WORKER_ENDPOINTS es inyectado por SkyPilot (JSON). Vacío en el primer
#    arranque: `scripts/sync_endpoints.py` lo rellena al levantar los workers.
# ---------------------------------------------------------------------------
python3 scripts/render_litellm_config.py \
    --endpoints-json "${{WORKER_ENDPOINTS}}" \
    --strategy "${{LB_STRATEGY}}" \
    --output docker_images/gateway/litellm_config.yaml

# ---------------------------------------------------------------------------
# 3. Levantar el stack del Gateway
#    GATEWAY_PUBLIC_URL se calcula aquí (la instancia se conoce su propia IP
#    pública vía metadata/ifconfig.me) y se exporta para que Open WebUI genere
#    enlaces absolutos correctos sin que el generador tenga que reinyectarla
#    después de 'sky launch'. Con dominio propio, usa https://<dominio> en vez
#    de la IP -incluso si el certificado real todavía no se emitió (el setup
#    ya dejó un autofirmado de respaldo, así que https:// siempre responde).
# ---------------------------------------------------------------------------
PUBLIC_IP="$(curl -s --max-time 5 ifconfig.me || true)"
if [ "${{TLS_ENABLED}}" = "true" ] && [ -n "${{TLS_DOMAIN}}" ]; then
    export GATEWAY_PUBLIC_URL="https://${{TLS_DOMAIN}}"
else
    export GATEWAY_PUBLIC_URL="http://${{PUBLIC_IP}}"
fi

cd {remote_root}/docker_images/gateway
sudo -E docker compose --env-file {remote_root}/.env up -d --build
sudo docker compose ps

echo "===> Gateway operativo (nginx como única puerta de entrada):"
echo "     Chat / API / Panel -> ${{GATEWAY_PUBLIC_URL}}/  |  /v1/  |  /panel/"
echo "     Salud de nginx      -> ${{GATEWAY_PUBLIC_URL}}/healthz"
"""


# =============================================================================
# BUILDERS DE MANIFIESTOS SKYPILOT
# =============================================================================
class TopologyBuilder:
    """Construye las especificaciones SkyPilot del Gateway y de los Workers."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.cliente = config["cliente"]
        self.red = config["red_y_aislamiento"]
        self.gateway = config.get("gateway", {})
        self.db = config.get("base_de_datos", {})
        self.workloads = config["workloads"]
        self._network_outputs = None  # poblado por apply_network_outputs() en modo 'auto'

    def apply_network_outputs(self, outputs: Any) -> None:
        """Inyecta los IDs/nombres reales creados por AwsNetworkManager (modo
        'gestion_red: auto'), para que build_sky_gateway_config/
        build_sky_workers_config referencien la VPC/SGs recién creados en vez
        de los nombres estáticos del contrato."""
        self._network_outputs = outputs

    # -- naming ---------------------------------------------------------------
    @property
    def base_name(self) -> str:
        return f"sooniverse-{self.cliente['id']}-{self.cliente['entorno']}"

    @property
    def gateway_cluster(self) -> str:
        return f"{self.base_name}-gw"

    def worker_cluster(self, wl_id: str) -> str:
        # SkyPilot exige nombres cortos y en minúsculas con guiones.
        return f"{self.base_name}-{wl_id}".lower().replace("_", "-").replace(".", "-")

    # -- comunes --------------------------------------------------------------
    def _base_envs(self) -> Dict[str, str]:
        return {
            "CLIENTE_ID": self.cliente["id"],
            "ENTORNO": self.cliente["entorno"],
            "MODO": self.cliente["modo"],
        }

    # -- gateway --------------------------------------------------------------
    def build_gateway(self, worker_endpoints: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        gw = self.gateway
        tls_cfg = gw.get("tls", {}) or {}
        tls_enabled = bool(tls_cfg.get("habilitado", False))
        expose_direct = bool(gw.get("exponer_puertos_directos", False))

        # nginx es la única puerta de entrada por defecto: 80 (+443 con TLS).
        # 4000/8000/8080 solo se abren si el operador activa exponer_puertos_directos
        # (depuración/dev); en ese modo también se respeta 'puertos_publicos' del contrato.
        public_ports = [80]
        if tls_enabled:
            public_ports.append(443)
        if expose_direct:
            for port in gw.get("puertos_publicos", [4000, 8000, 8080]):
                if port not in public_ports:
                    public_ports.append(port)

        resources: Dict[str, Any] = {
            "cloud": "aws",
            "region": self.red["region"],
            "instance_type": gw.get("tipo_instancia", "t4g.large"),
            "disk_size": gw.get("disk_size", 100),
            "ports": public_ports,
            "labels": {**self.red.get("tags_obligatorios", {}), "rol": "gateway"},
        }

        envs = {
            **self._base_envs(),
            "ROL_NODO": "gateway",
            "AUTO_INIT_DB": str(self.db.get("AUTO_INIT_DB", True)).lower(),
            "AUTO_REFRESH_METRICS": str(self.db.get("auto_refresh_metrics", True)).lower(),
            "LB_STRATEGY": gw.get("load_balancing_strategy", "latency-based-routing"),
            "WEBUI_SIGNUP": str(gw.get("open_webui", {}).get("signup_habilitado", False)).lower(),
            "METRICS_REFRESH_INTERVAL": str(
                gw.get("django_metrics", {}).get("metrics_refresh_interval", 300)
            ),
            # Lista de endpoints vLLM en JSON; se rellena tras aprovisionar los workers.
            "WORKER_ENDPOINTS": json.dumps(worker_endpoints or []),
            # Dominio propio (derivado de gateway.dominio en load_config()): usado por
            # el 'run' script para construir GATEWAY_PUBLIC_URL con https y el dominio
            # real en vez de 'http://<IP efímera>', y para escribir ALLOWED_HOSTS /
            # CSRF_TRUSTED_ORIGINS en .env antes de levantar el stack.
            "TLS_ENABLED": str(tls_enabled).lower(),
            "TLS_DOMAIN": tls_cfg.get("dominio") or "",
        }

        file_mounts = {
            f"{REMOTE_ROOT}/docker_images/gateway": "./docker_images/gateway",
            f"{REMOTE_ROOT}/docker_images/openwebui": "./docker_images/openwebui",
            f"{REMOTE_ROOT}/database": "./database",
            f"{REMOTE_ROOT}/scripts": "./scripts",
            f"{REMOTE_ROOT}/django_metrics": "./django_metrics",
            f"{REMOTE_ROOT}/config_global.yaml": "./config_global.yaml",
            f"{REMOTE_ROOT}/.env": "./.env",
        }

        # Clave SSH que SkyPilot genera LOCALMENTE (máquina del operador/CI que
        # corre 'sky launch', nunca en el propio Gateway) para el bastion hacia
        # los workers -ver ssh_proxy_command en build_sky_workers_config(). El
        # panel Django (que SÍ vive en el Gateway) la necesita para el botón
        # "Reiniciar" de la card Pool vLLM (metrics/workers.py). Si todavía no
        # existe (primer 'sky launch' de este clúster: SkyPilot la genera como
        # efecto secundario de esa misma corrida, no antes), el botón queda
        # deshabilitado hasta el siguiente '--only gateway'.
        gateway_ssh_key = Path.home() / ".sky" / "generated" / "ssh-keys" / f"{self.gateway_cluster}.key"
        if gateway_ssh_key.exists():
            file_mounts[f"{REMOTE_ROOT}/.ssh_bastion_key"] = str(gateway_ssh_key)

        schema_dir = self.db.get("schema_dir", "database")

        tls_setup = ""
        tls_modo = tls_cfg.get("modo", "self-signed")
        if tls_enabled and tls_modo == "self-signed":
            tls_setup = TLS_SELF_SIGNED_SETUP.format(
                remote_root=REMOTE_ROOT, tls_domain=tls_cfg.get("dominio") or "sooniverse.local",
            )
        elif tls_enabled and tls_modo == "letsencrypt":
            dominio_cfg = self.gateway.get("dominio") or {}
            tls_setup = TLS_LETSENCRYPT_SETUP.format(
                remote_root=REMOTE_ROOT,
                tls_domain=tls_cfg["dominio"],
                email_acme=tls_cfg["email_acme"],
                staging_flag="--staging" if dominio_cfg.get("staging", False) else "",
            )
        elif tls_enabled:
            tls_setup = (
                f'echo "===> TLS modo \'{tls_cfg.get("modo")}\' no implementado todavía; '
                'usa self-signed, letsencrypt, o deja tls.habilitado: false."'
            )

        return {
            "name": self.gateway_cluster,
            "resources": resources,
            "num_nodes": 1,
            "file_mounts": file_mounts,
            "envs": envs,
            "setup": GATEWAY_SETUP_SCRIPT.format(remote_root=REMOTE_ROOT, tls_setup=tls_setup).strip(),
            "run": GATEWAY_RUN_SCRIPT.format(remote_root=REMOTE_ROOT, schema_dir=schema_dir).strip(),
        }

    # -- workers --------------------------------------------------------------
    def build_worker(self, wl: Dict[str, Any]) -> Dict[str, Any]:
        modelo = wl.get("modelo", wl["id"])
        frac = wl.get("asignacion_fraccional", {})

        resources: Dict[str, Any] = {
            "cloud": "aws",
            "region": self.red["region"],
            "accelerators": f"{wl['accelerator']}:{wl['cantidad_gpus']}",
            "labels": {**self.red.get("tags_obligatorios", {}), "rol": "worker", "workload": wl["id"]},
            # El puerto se declara para que SkyPilot abra la regla en el Security
            # Group; sin ella el Gateway tampoco podría alcanzar al worker DENTRO
            # de la VPC. La privacidad no la da esta lista, la da
            # `use_internal_ips: true`: el worker no recibe IP pública, así que la
            # regla solo es alcanzable desde dentro de la VPC.
            # Para restringir el origen a nivel de CIDR, define
            # `red_y_aislamiento.security_group_workers` con un SG propio.
            "ports": [wl["puerto"]],
        }

        if self.red.get("image_id"):
            resources["image_id"] = self.red["image_id"]
        if wl.get("tipo_instancia"):
            resources["instance_type"] = wl["tipo_instancia"]

        capacidades = wl.get("capacidades", {})
        conc = wl.get("concurrencia", {}) or {}
        envs = {
            **self._base_envs(),
            "ROL_NODO": "worker",
            "WORKLOAD_ID": wl["id"],
            "MODEL_NAME": wl.get("hf_repo", "cyankiwi/Qwen3.5-2B-AWQ-4bit"),
            "MODEL_PUBLIC_NAME": wl.get("nombre_publico", wl["id"]),
            "GPU_MEMORY_UTILIZATION": str(frac.get("gpu_memory_utilization", 0.95)),
            "MAX_MODEL_LEN": str(frac.get("max_model_len", 16384)),
            "VLLM_PORT": str(wl["puerto"]),
            # Planificador de vLLM (ver 'concurrencia' en config_global.yaml).
            # Determina cuántas peticiones atiende el worker A LA VEZ; es el
            # parámetro que fija el techo de capacidad real de la infraestructura.
            "MAX_NUM_SEQS": str(conc.get("max_num_seqs", DEFAULT_MAX_NUM_SEQS)),
            "MAX_NUM_BATCHED_TOKENS": str(
                conc.get("max_num_batched_tokens", DEFAULT_MAX_NUM_BATCHED_TOKENS)
            ),
            # Capacidades declaradas (ver config_global.yaml): el entrypoint solo
            # agrega --enable-auto-tool-choice/--limit-mm-per-prompt si aquí están
            # activas, para no anunciarle a un cliente (Open WebUI, LiteLLM) una
            # función que este modelo no soporta de verdad.
            "ENABLE_VISION": "1" if capacidades.get("vision", True) else "0",
            "ENABLE_TOOL_CALLING": "1" if capacidades.get("tool_calling", False) else "0",
            "TOOL_CALL_PARSER": capacidades.get("tool_call_parser") or "",
        }

        return {
            "name": self.worker_cluster(wl["id"]),
            "resources": resources,
            "num_nodes": wl.get("replicas", 1),
            "file_mounts": {
                f"{REMOTE_ROOT}/docker_images/{modelo}": f"./docker_images/{modelo}",
            },
            "envs": envs,
            "setup": GPU_SETUP_SCRIPT.format(remote_root=REMOTE_ROOT, modelo=modelo).strip(),
            "run": WORKER_RUN_SCRIPT.format(
                remote_root=REMOTE_ROOT, modelo=modelo, wl_id=wl["id"], puerto=wl["puerto"]
            ).strip(),
        }

    # -- config de cliente SkyPilot para el Nodo Gateway ------------------------
    @property
    def has_network_outputs(self) -> bool:
        return self._network_outputs is not None

    def build_sky_gateway_config(self) -> Dict[str, Any]:
        """
        Fuerza al Nodo Gateway a nacer en la misma VPC que los workers (con su
        propio Security Group reservado), para que el túnel SSH bastion y las
        rutas internas a la subred privada funcionen. Sin esto, SkyPilot puede
        elegir la VPC por defecto de la cuenta, aislando al Gateway de los
        workers aunque ambos estén "arriba".
        """
        aws_cfg: Dict[str, Any] = {}
        net = self._network_outputs

        vpc_name = net.vpc_name if net else self.red.get("vpc_name")
        sg_gateway = net.sg_gateway_name if net else self.red.get("security_group_gateway")

        if vpc_name:
            aws_cfg["vpc_name"] = vpc_name
        if sg_gateway:
            aws_cfg["security_group_name"] = sg_gateway

        return {"aws": aws_cfg} if aws_cfg else {}

    # -- config de cliente SkyPilot para los workers privados ------------------
    def build_sky_workers_config(self, gateway_ip: Optional[str] = None) -> Dict[str, Any]:
        """
        Genera la configuración de cliente de SkyPilot que fuerza a los workers a
        vivir dentro de la VPC sin IP pública, tunelizando SSH por el Gateway.
        """
        aws_cfg: Dict[str, Any] = {}
        net = self._network_outputs

        vpc_name = net.vpc_name if net else self.red.get("vpc_name")
        if vpc_name:
            aws_cfg["vpc_name"] = vpc_name

        # Security Group: en modo 'auto' es el que crea AwsNetworkManager (SG->SG
        # con el gateway); en modo 'existente' es el pre-creado por el operador.
        sg_workers = net.sg_workers_name if net else self.red.get("security_group_workers")
        if sg_workers:
            aws_cfg["security_group_name"] = sg_workers

        if self.red.get("workers_en_subred_privada", True):
            aws_cfg["use_internal_ips"] = True
            if gateway_ip:
                gateway_ssh_key = (
                    Path.home() / ".sky" / "generated" / "ssh-keys" / f"{self.gateway_cluster}.key"
                )
                if gateway_ssh_key.exists():
                    os.chmod(gateway_ssh_key, 0o600)
                aws_cfg["ssh_proxy_command"] = (
                    f"ssh -W %h:%p -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
                    f"-o ConnectTimeout=10 -i {gateway_ssh_key} ubuntu@{gateway_ip}"
                )

        return {"aws": aws_cfg} if aws_cfg else {}


# =============================================================================
# IO
# =============================================================================
HEADER = (
    "# ==============================================================================\n"
    "# ARCHIVO GENERADO AUTOMÁTICAMENTE POR SOONIVERSE INFRA GENERATOR\n"
    "# NO EDITAR MANUALMENTE. MODIFICAR config_global.yaml EN SU LUGAR.\n"
    "# ==============================================================================\n\n"
)


def _derive_tls_from_dominio(config: Dict[str, Any]) -> None:
    """Con 'gateway.dominio.habilitado: true', deriva 'gateway.tls.*' a partir del
    catálogo -así el resto del código (nginx, SG, resources.ports, el reporte
    final) sigue leyendo solo 'tls.*' sin saber nada de 'dominio'. Se llama
    DESPUÉS de ConfigValidator.validate(), que ya garantizó que el catálogo es
    válido y que 'seleccionado' existe en 'disponibles'.

    'tls.modo'/'tls.habilitado' NO se tratan como contradicción: el contrato de
    ejemplo siempre trae 'tls: {habilitado: false, modo: self-signed, dominio:
    null}' como placeholder explícito (no ausente), así que exigir borrarlo antes
    de activar 'dominio' sería frágil. Solo se falla si 'tls.dominio' apunta a un
    dominio DISTINTO del seleccionado -esa sí es una señal inequívoca de que el
    operador quiso fijarlo a mano para otra cosa."""
    gw = config.setdefault("gateway", {})
    dominio_cfg = gw.get("dominio") or {}
    if not dominio_cfg.get("habilitado", False):
        return

    seleccionado = dominio_cfg["seleccionado"]
    entrada = next(e for e in dominio_cfg["disponibles"] if e["nombre"] == seleccionado)
    email_acme = entrada["email_acme"]

    tls = gw.setdefault("tls", {})
    if tls.get("dominio") not in (None, seleccionado):
        raise ConfigValidationError(
            f"'gateway.tls.dominio' ('{tls.get('dominio')}') no coincide con "
            f"'gateway.dominio.seleccionado' ('{seleccionado}'). Con "
            "'gateway.dominio.habilitado: true' no fijes 'gateway.tls.dominio' a mano: se deriva solo."
        )

    tls["habilitado"] = True
    tls["modo"] = "letsencrypt"
    tls["dominio"] = seleccionado
    tls["email_acme"] = email_acme


def load_config(config_path: str) -> Dict[str, Any]:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"No se encontró el archivo de configuración: {config_path}")

    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    ConfigValidator.validate(config)
    _derive_tls_from_dominio(config)
    return config


def dump_yaml(data: Dict[str, Any], out_path: Path, header: bool = True) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        if header:
            f.write(HEADER)
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def build_network_spec_from_config(config: Dict[str, Any]) -> "Any":
    """Traduce `red_y_aislamiento` + `gateway` + `workloads[].puerto` del contrato
    a un `aws_network.NetworkSpec`. Solo tiene sentido en modo 'gestion_red: auto'."""
    from aws_network import NetworkSpec  # import perezoso: boto3 solo hace falta aquí

    cliente = config["cliente"]
    red = config["red_y_aislamiento"]
    gw = config.get("gateway", {})
    nat = red.get("nat_gateway") or {}
    endpoints = red.get("vpc_endpoints") or {}
    subredes = red.get("subredes") or {}
    tls = gw.get("tls") or {}
    dominio = gw.get("dominio") or {}

    worker_ports = sorted({wl["puerto"] for wl in config["workloads"]})

    return NetworkSpec(
        client_id=cliente["id"],
        environment=cliente["entorno"],
        region=red["region"],
        vpc_cidr=red.get("vpc_cidr", "10.0.0.0/16"),
        az_count=red.get("azs", 1),
        public_subnet_cidrs=subredes.get("publicas"),
        private_subnet_cidrs=subredes.get("privadas"),
        nat_mode=nat.get("modo", "single"),
        enable_s3_endpoint=bool(endpoints.get("s3", True)),
        admin_cidrs=[red.get("cidr_admin_ssh", "0.0.0.0/0")],
        public_cidrs=[red.get("cidr_permitido_gateway", "0.0.0.0/0")],
        gateway_public_ports=gw.get("puertos_publicos", [80, 4000, 8000, 8080]),
        worker_ports=worker_ports,
        expose_direct_ports=bool(gw.get("exponer_puertos_directos", False)),
        tls_enabled=bool(tls.get("habilitado", False)),
        nat_timeout_seconds=nat.get("timeout_segundos", 300),
        extra_tags=red.get("tags_obligatorios") or {},
        aws_profile=red.get("aws_profile"),
        gateway_eip=bool(dominio.get("habilitado", False)),
        gateway_eip_persistent=bool(dominio.get("eip_persistente", True)),
        gateway_domain=dominio.get("seleccionado") if dominio.get("habilitado") else None,
    )


def load_network_outputs_from_state(config: Dict[str, Any], state: Any, deployment_id: str) -> Optional["Any"]:
    """Reconstruye `aws_network.NetworkOutputs` a partir de lo YA registrado en
    PostgreSQL para `deployment_id`.

    Necesario porque `--only gateway` / `--only workers` / `--only endpoints` se
    pueden invocar como procesos SEPARADOS de `--only network` (documentado en
    docs/07_REFERENCIA_CLI.md como flujo válido). `TopologyBuilder._network_outputs`
    solo vive en memoria durante una corrida; sin esto, una invocación de
    `--only gateway` en modo 'auto' no tenía forma de saber el vpc_name/SG reales
    ya creados, y `build_sky_gateway_config()` producía `{"aws": {}}` -SkyPilot
    entonces lanzaba el gateway en la VPC por defecto de la cuenta, no en la
    nuestra (bug real encontrado en una corrida de prueba real).

    Devuelve None si el despliegue no tiene (todavía) VPC + ambos SGs registrados.
    """
    from aws_network import NetworkOutputs

    resources = state.list_resources(deployment_id)
    by_component: Dict[str, List[Dict[str, Any]]] = {}
    for res in resources:
        by_component.setdefault(res["component"], []).append(res)

    def first(component: str) -> Optional[Dict[str, Any]]:
        rows = by_component.get(component)
        return rows[0] if rows else None

    vpc_row = first("vpc")
    sg_gw_row = first("sg-gateway")
    sg_wk_row = first("sg-workers")
    if not vpc_row or not sg_gw_row or not sg_wk_row:
        return None

    cliente = config["cliente"]

    def resolved_name(row: Dict[str, Any], fallback_suffix: str) -> str:
        attrs = row.get("attributes") or {}
        return attrs.get("name") or f"sooniverse-{cliente['id']}-{cliente['entorno']}-{fallback_suffix}"

    eip_gw_row = first("eip-gateway")
    eip_gw_attrs = (eip_gw_row or {}).get("attributes") or {}

    return NetworkOutputs(
        deployment_id=deployment_id,
        vpc_id=vpc_row["aws_id"],
        vpc_name=resolved_name(vpc_row, "vpc"),
        availability_zones=sorted({
            r["availability_zone"] for r in by_component.get("subnet-public", []) if r.get("availability_zone")
        }),
        public_subnet_ids=[r["aws_id"] for r in by_component.get("subnet-public", [])],
        private_subnet_ids=[r["aws_id"] for r in by_component.get("subnet-private", [])],
        internet_gateway_id=(first("igw") or {}).get("aws_id"),
        nat_gateway_ids=[r["aws_id"] for r in by_component.get("nat", [])],
        elastic_ip_allocation_ids=[r["aws_id"] for r in by_component.get("eip", [])],
        public_route_table_id=(first("rtb-public") or {}).get("aws_id"),
        private_route_table_ids=[r["aws_id"] for r in by_component.get("rtb-private", [])],
        sg_gateway_id=sg_gw_row["aws_id"],
        sg_gateway_name=resolved_name(sg_gw_row, "gateway"),
        sg_workers_id=sg_wk_row["aws_id"],
        sg_workers_name=resolved_name(sg_wk_row, "workers"),
        managed_by_us=True,
        gateway_eip_allocation_id=(eip_gw_row or {}).get("aws_id"),
        gateway_eip_public_ip=eip_gw_attrs.get("public_ip"),
    )


def _suggest_free_cidr(existing_cidrs: List[str], prefix_len: int = 16) -> Optional[str]:
    """Primer /16 dentro de 10.0.0.0/8 que no se solapa con ninguno de `existing_cidrs`."""
    existing_nets = [ipaddress.ip_network(c, strict=False) for c in existing_cidrs]
    for candidate in ipaddress.ip_network("10.0.0.0/8").subnets(new_prefix=prefix_len):
        if not any(candidate.overlaps(net) for net in existing_nets):
            return str(candidate)
    return None


def check_cidr_isolation(config: Dict[str, Any]) -> None:
    """Aislamiento de CIDR entre clientes (Fase 6): si dos despliegues activos en
    la misma región comparten/solapan `vpc_cidr`, avisa (no aborta -no es un
    error si esas VPC nunca se van a peerear- pero impedirá el peering futuro)
    y sugiere un CIDR libre. Best-effort: si PostgreSQL no está disponible aquí,
    no bloquea el resto del flujo (la guarda real de "no crear sin poder
    registrar" ya la aplica `PostgresInfraStateStore.open_deployment`)."""
    red = config["red_y_aislamiento"]
    if red.get("gestion_red", "auto") != "auto":
        return

    cliente = config["cliente"]
    vpc_cidr = red.get("vpc_cidr", "10.0.0.0/16")
    region = red["region"]

    try:
        from db_setup import connect, resolve_db_config

        conn = connect(resolve_db_config(REPO_ROOT / ".env"))
    except Exception:
        return

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT client_id, environment, config_snapshot -> 'red_y_aislamiento' ->> 'vpc_cidr'
                FROM sooniverse.infra_deployment
                WHERE region = %s AND status NOT IN ('destroyed', 'error')
                  AND NOT (client_id = %s AND environment = %s)
                """,
                (region, cliente["id"], cliente["entorno"]),
            )
            rows = cur.fetchall()
    except Exception:
        return
    finally:
        conn.close()

    try:
        own_net = ipaddress.ip_network(vpc_cidr, strict=False)
    except ValueError:
        return

    overlaps = []
    for other_client, other_env, other_cidr in rows:
        if not other_cidr:
            continue
        try:
            other_net = ipaddress.ip_network(other_cidr, strict=False)
        except ValueError:
            continue
        if own_net.overlaps(other_net):
            overlaps.append((other_client, other_env, other_cidr))

    if not overlaps:
        return

    suggestion = _suggest_free_cidr([vpc_cidr] + [c for _, _, c in overlaps])
    for other_client, other_env, other_cidr in overlaps:
        print(
            f"[WARNING] 'vpc_cidr' {vpc_cidr} se solapa con el despliegue activo "
            f"'{other_client}/{other_env}' ({other_cidr}) en la región {region}. No es un error "
            "si esas VPC nunca se van a interconectar (peering), pero lo impedirá en el futuro."
            + (f" CIDR libre sugerido: {suggestion}." if suggestion else "")
        )


def config_hash_of(config: Dict[str, Any]) -> str:
    """sha256 determinista del contrato completo (para detectar cambios entre corridas)."""
    canonical = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# =============================================================================
# plan_changes() -- reconciliación de modificaciones en caliente (Fase 3/5.4)
# =============================================================================
NO_OP = "no-op"
IN_PLACE = "in-place"
RECREATE_CLUSTER = "recreate-cluster"
REQUIRES_DESTROY = "requires-destroy"

# Campos de workload cuyo cambio implica relanzar ese clúster worker (hardware,
# imagen o puerto distintos no se pueden aplicar sobre una instancia viva).
# 'concurrencia' entra aquí y no en IN_PLACE porque max_num_seqs/
# max_num_batched_tokens son banderas de arranque de vLLM: no se pueden aplicar
# sobre un proceso vivo, hay que relanzar el worker (que reutiliza la instancia
# y vuelve a correr setup+run; no destruye la máquina).
WORKLOAD_RECREATE_KEYS = {"accelerator", "cantidad_gpus", "tipo_instancia", "puerto", "hf_repo", "modelo", "replicas", "concurrencia"}
# Campos que solo requieren re-renderizar litellm_config.yaml + reload (sin tocar SkyPilot).
WORKLOAD_IN_PLACE_KEYS = {"nombre_publico", "peso_balanceo", "asignacion_fraccional"}


@dataclass
class FieldChange:
    field: str
    old: Any
    new: Any
    classification: str  # no-op | in-place | recreate-cluster | requires-destroy
    workload_id: Optional[str] = None

    def __str__(self) -> str:
        suffix = f" (workload={self.workload_id})" if self.workload_id else ""
        return f"[{self.classification}] {self.field}: {self.old!r} -> {self.new!r}{suffix}"


@dataclass
class ChangePlan:
    changes: List[FieldChange] = dataclass_field(default_factory=list)

    @property
    def requires_destroy(self) -> bool:
        return any(c.classification == REQUIRES_DESTROY for c in self.changes)

    @property
    def clusters_to_recreate(self) -> List[str]:
        return sorted({c.workload_id for c in self.changes if c.classification == RECREATE_CLUSTER and c.workload_id})

    @property
    def is_no_op(self) -> bool:
        return not self.changes

    def summary(self) -> str:
        if self.is_no_op:
            return "Sin cambios respecto al despliegue activo."
        return "\n".join(str(c) for c in self.changes)


def plan_changes(current_snapshot: Optional[Dict[str, Any]], new_config: Dict[str, Any]) -> ChangePlan:
    """Clasifica cada diferencia entre `current_snapshot` (config_snapshot del
    despliegue activo registrado en PostgreSQL) y `new_config` (el contrato que
    se va a aplicar) en no-op | in-place | recreate-cluster | requires-destroy.

    Es puramente informativo: no aplica nada. Quien llama (el orquestador de
    `--run`) decide qué hacer con el plan -típicamente abortar con un mensaje
    claro si `requires_destroy`, o relanzar solo `clusters_to_recreate`.
    """
    plan = ChangePlan()
    current_snapshot = current_snapshot or {}

    old_red = current_snapshot.get("red_y_aislamiento", {}) or {}
    new_red = new_config.get("red_y_aislamiento", {}) or {}

    for key in ("vpc_cidr", "azs"):
        if old_red.get(key) != new_red.get(key):
            plan.changes.append(
                FieldChange(f"red_y_aislamiento.{key}", old_red.get(key), new_red.get(key), REQUIRES_DESTROY)
            )

    old_nat_modo = (old_red.get("nat_gateway") or {}).get("modo")
    new_nat_modo = (new_red.get("nat_gateway") or {}).get("modo")
    if old_nat_modo != new_nat_modo:
        plan.changes.append(
            FieldChange("red_y_aislamiento.nat_gateway.modo", old_nat_modo, new_nat_modo, REQUIRES_DESTROY)
        )

    for key in ("cidr_permitido_gateway", "cidr_admin_ssh"):
        if old_red.get(key) != new_red.get(key):
            plan.changes.append(
                FieldChange(f"red_y_aislamiento.{key}", old_red.get(key), new_red.get(key), IN_PLACE)
            )

    old_gw = current_snapshot.get("gateway", {}) or {}
    new_gw = new_config.get("gateway", {}) or {}
    if old_gw.get("load_balancing_strategy") != new_gw.get("load_balancing_strategy"):
        plan.changes.append(
            FieldChange(
                "gateway.load_balancing_strategy",
                old_gw.get("load_balancing_strategy"), new_gw.get("load_balancing_strategy"), IN_PLACE,
            )
        )

    old_tls = old_gw.get("tls", {}) or {}
    new_tls = new_gw.get("tls", {}) or {}
    old_dominio = old_gw.get("dominio", {}) or {}
    new_dominio = new_gw.get("dominio", {}) or {}
    if old_tls != new_tls or old_dominio != new_dominio:
        # No es 'recreate-cluster' (no toca clústeres SkyPilot) ni 'requires-destroy'
        # (no toca vpc_cidr/azs/nat), pero SÍ exige re-correr la fase 'network' -el
        # diff de SG que abre/revoca el 443 vive ahí (aws_network.py::
        # _sync_ingress_cidr_rules)- y luego 'gateway'/'dominio' para que nginx y el
        # certificado reflejen el cambio. Esa guía va en el campo, no en un
        # atributo aparte: ChangePlan no tiene 'notes', y summary() solo imprime
        # FieldChange.__str__().
        plan.changes.append(
            FieldChange(
                "gateway.tls/gateway.dominio (re-correr --only network, luego gateway+dominio)",
                {"tls": old_tls, "dominio": old_dominio}, {"tls": new_tls, "dominio": new_dominio}, IN_PLACE,
            )
        )

    old_workloads = {wl["id"]: wl for wl in current_snapshot.get("workloads", []) or []}
    new_workloads = {wl["id"]: wl for wl in new_config.get("workloads", []) or []}

    for wl_id in old_workloads.keys() - new_workloads.keys():
        plan.changes.append(FieldChange("workloads[].id", wl_id, None, RECREATE_CLUSTER, workload_id=wl_id))
    for wl_id in new_workloads.keys() - old_workloads.keys():
        plan.changes.append(FieldChange("workloads[].id", None, wl_id, RECREATE_CLUSTER, workload_id=wl_id))

    for wl_id in old_workloads.keys() & new_workloads.keys():
        old_wl, new_wl = old_workloads[wl_id], new_workloads[wl_id]
        for key in WORKLOAD_RECREATE_KEYS:
            if old_wl.get(key) != new_wl.get(key):
                plan.changes.append(
                    FieldChange(f"workloads[{wl_id}].{key}", old_wl.get(key), new_wl.get(key),
                                RECREATE_CLUSTER, workload_id=wl_id)
                )
        for key in WORKLOAD_IN_PLACE_KEYS:
            if old_wl.get(key) != new_wl.get(key):
                plan.changes.append(
                    FieldChange(f"workloads[{wl_id}].{key}", old_wl.get(key), new_wl.get(key),
                                IN_PLACE, workload_id=wl_id)
                )

    return plan


def generate_manifests(config: Dict[str, Any], out_dir: Path, builder: Optional["TopologyBuilder"] = None) -> Dict[str, Any]:
    """Escribe todos los manifiestos de la topología y devuelve sus rutas."""
    builder = builder or TopologyBuilder(config)
    artefactos: Dict[str, Any] = {"gateway": None, "workers": {}, "sky_config": None}

    if builder.gateway.get("habilitado", True):
        from render_gateway_stack import render as render_gateway_stack

        render_gateway_stack(config, capabilities_dir=out_dir)

        gw_path = out_dir / GATEWAY_MANIFEST
        dump_yaml(builder.build_gateway(), gw_path)
        artefactos["gateway"] = gw_path
        print(f"[OK] Gateway     -> {gw_path.name}  (cluster: {builder.gateway_cluster})")

    for wl in config["workloads"]:
        wk_path = out_dir / WORKER_MANIFEST_FMT.format(wl_id=wl["id"])
        dump_yaml(builder.build_worker(wl), wk_path)
        artefactos["workers"][wl["id"]] = wk_path
        print(
            f"[OK] Worker '{wl['id']}' -> {wk_path.name}  "
            f"(cluster: {builder.worker_cluster(wl['id'])}, nodos: {wl.get('replicas', 1)})"
        )

    sky_cfg = builder.build_sky_workers_config()
    if sky_cfg:
        cfg_path = out_dir / SKY_WORKERS_CONFIG
        dump_yaml(sky_cfg, cfg_path)
        artefactos["sky_config"] = cfg_path
        print(f"[OK] SkyPilot cfg -> {cfg_path.name}  (VPC / IPs internas / bastion)")

    return artefactos


# =============================================================================
# ORQUESTACIÓN DE DESPLIEGUE
# =============================================================================
def _sky_binary() -> Optional[str]:
    return shutil.which("sky")


def _run_sky(args: List[str], env: Optional[Dict[str, str]] = None) -> None:
    sky = _sky_binary()
    if not sky:
        raise RuntimeError(
            "El comando 'sky' (SkyPilot) no está en el PATH. Instala con: pip install \"skypilot[aws]\""
        )
    cmd = [sky] + args
    print(f"[EXEC] {' '.join(cmd)}")
    merged_env = {**os.environ, **(env or {})}
    subprocess.run(cmd, check=True, env=merged_env)


def _gateway_public_ip(cluster: str) -> Optional[str]:
    sky = _sky_binary()
    if not sky:
        return None
    try:
        out = subprocess.run(
            [sky, "status", "--ip", cluster], check=True, capture_output=True, text=True
        )
        ip = out.stdout.strip().splitlines()[-1].strip()
        return ip or None
    except (subprocess.CalledProcessError, IndexError):
        return None


class GatewayEipAssociationError(RuntimeError):
    """Fallo asociando la Elastic IP del Gateway a la instancia recién lanzada."""


def _associate_gateway_eip(
    cluster: str, allocation_id: str, region: str, aws_profile: Optional[str] = None
) -> str:
    """Asocia la Elastic IP reservada en la fase 'network' (gateway.dominio.
    habilitado: true) a la instancia EC2 del Gateway recién lanzada, y reconcilia
    el estado local de SkyPilot -asociar una EIP le cambia la IP pública de la
    instancia, y SkyPilot sigue intentando conectarse por SSH con la IP vieja
    hasta que se reconcilia, lo que 'sky status --refresh' NO logra por sí solo
    (falla el chequeo de salud contra la IP vieja y deja el clúster en estado
    'INIT' en vez de detectar la nueva IP -comprobado empíricamente). 'sky start'
    sí reconoce y adopta la IP nueva del proveedor. Verifica con 'sky exec <gw>
    true' antes de devolver, porque TODAS las fases siguientes (endpoints,
    capabilities, capacidad, verify) dependen de 'sky exec' contra este mismo
    Gateway."""
    import boto3

    session = boto3.Session(profile_name=aws_profile) if aws_profile else boto3.Session()
    ec2 = session.client("ec2", region_name=region)

    instance_id = None
    for tag_key in ("ray-cluster-name", "skypilot-cluster-name"):
        resp = ec2.describe_instances(
            Filters=[
                # SkyPilot etiqueta la instancia como '<cluster>-<sufijo hash>'
                # (p.ej. 'sooniverse-acme-prod-gw-97e585e4'), nunca el nombre
                # exacto de clúster -de ahí el comodín.
                {"Name": f"tag:{tag_key}", "Values": [cluster, f"{cluster}-*"]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        )
        for reservation in resp.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance.get("InstanceId")
                break
            if instance_id:
                break
        if instance_id:
            break

    if not instance_id:
        raise GatewayEipAssociationError(
            f"No se encontró la instancia EC2 del clúster '{cluster}' para asociar la Elastic IP "
            f"del Gateway ({allocation_id}). El despliegue continuaría con una IP efímera."
        )

    ec2.associate_address(AllocationId=allocation_id, InstanceId=instance_id, AllowReassociation=True)

    sky = _sky_binary()
    if sky:
        restart = subprocess.run(
            [sky, "start", "-y", cluster], capture_output=True, text=True, timeout=300
        )
        if restart.returncode != 0:
            raise GatewayEipAssociationError(
                f"La Elastic IP se asoció a {instance_id}, pero 'sky start {cluster}' (para que "
                f"SkyPilot reconozca la IP nueva) falló: {restart.stderr.strip() or restart.stdout.strip()}"
            )

    new_ip = _gateway_public_ip(cluster)
    if not new_ip:
        raise GatewayEipAssociationError(
            f"La Elastic IP se asoció a {instance_id}, pero 'sky status --ip {cluster}' no devolvió "
            "ninguna IP tras reconciliar con 'sky start'."
        )

    if sky:
        # 'sky start' sincroniza sus file_mounts de un solo archivo (.env,
        # config_global.yaml, la clave SSH del bastion) escribiéndolos como
        # archivo real en vez de symlink -a diferencia de 'sky launch' sobre un
        # clúster ya arriba, que sí symlinkea-. Un 'sky launch' posterior sobre
        # este mismo clúster falla entonces con "Failed mounting because path
        # exists". Se limpian aquí los remanentes para dejar el símlink limpio
        # de cara al siguiente 'sky launch' (comprobado empíricamente).
        subprocess.run(
            [
                sky, "exec", cluster,
                "for f in config_global.yaml .ssh_bastion_key; do "
                "p=/home/ubuntu/sooniverse_infra/$f; "
                "[ -f \"$p\" ] && [ ! -L \"$p\" ] && rm -f \"$p\"; "
                "done; true",
            ],
            capture_output=True, text=True, timeout=120,
        )
        # '.env' no se deja solo borrado: fases posteriores en ESTA MISMA
        # corrida (sync_endpoints.py -> 'docker compose --env-file .env
        # restart litellm') lo necesitan presente YA, no en el próximo 'sky
        # launch'. Se reescribe con el contenido local actual -nunca con la
        # caché de un 'sky launch' anterior, que puede estar desactualizada.
        env_path = REPO_ROOT / ".env"
        if env_path.exists():
            payload = env_path.read_text(encoding="utf-8")
            remote_env = "/home/ubuntu/sooniverse_infra/.env"
            script = (
                f"rm -f {remote_env} && cat > {remote_env} <<'SOONIVERSE_ENV_EOF'\n"
                f"{payload}\nSOONIVERSE_ENV_EOF\n"
            )
            subprocess.run([sky, "exec", cluster, script], capture_output=True, text=True, timeout=120)

    sky = _sky_binary()
    if sky:
        check = subprocess.run(
            [sky, "exec", cluster, "true"], capture_output=True, text=True, timeout=120
        )
        if check.returncode != 0:
            raise GatewayEipAssociationError(
                f"La Elastic IP se asoció ({new_ip}) pero 'sky exec {cluster} true' falló tras el "
                f"refresh: {check.stderr.strip() or check.stdout.strip()}"
            )

    return new_ip


def _resolve_a_record(domain: str) -> Optional[str]:
    import socket

    try:
        infos = socket.getaddrinfo(domain, None, family=socket.AF_INET)
        return infos[0][4][0] if infos else None
    except socket.gaierror:
        return None


def run_dominio_phase(
    config: Dict[str, Any],
    gateway_cluster: str,
    gateway_ip: str,
    state: Optional[Any] = None,
    deployment_id: Optional[str] = None,
) -> None:
    """Fase 'dominio': verifica que el registro DNS A del dominio elegido resuelva
    a la IP del Gateway, y si es así, emite/renueva el certificado Let's Encrypt
    (certbot, modo webroot -nginx ya está arriba desde la fase 'gateway') y
    recarga nginx. Best-effort en TODO: nunca aborta el despliegue, solo avisa
    -igual que 'endpoints'/'capabilities'/'capacidad'/'verify'."""
    dominio_cfg = (config.get("gateway", {}).get("dominio") or {})
    dominio = dominio_cfg["seleccionado"]
    espera = int(dominio_cfg.get("esperar_dns_segundos", 300))

    resuelto = _resolve_a_record(dominio)
    t0 = time.monotonic()
    while resuelto != gateway_ip and (time.monotonic() - t0) < espera:
        time.sleep(15)
        resuelto = _resolve_a_record(dominio)

    if resuelto != gateway_ip:
        print(
            f"[WARNING] '{dominio}' resuelve a '{resuelto or '(sin resolver)'}', no a la IP del Gateway "
            f"({gateway_ip}). No se emite el certificado en esta corrida -el despliegue sigue en HTTP. "
            "Crea/corrige el registro A (ver Manual_Dominio_AWS.md) y vuelve a correr: "
            "python scripts/generate_infra.py --run --only dominio"
        )
        if state and deployment_id:
            state.log_event(deployment_id, "dominio", "verify_dns", "warning",
                             message=f"{dominio} -> {resuelto or '(sin resolver)'}, esperado {gateway_ip}")
        return

    print(f"[DOMINIO] '{dominio}' resuelve correctamente a {gateway_ip}. Emitiendo/renovando certificado...")

    tls_cfg = config.get("gateway", {}).get("tls") or {}
    email = tls_cfg.get("email_acme") or next(
        (e["email_acme"] for e in dominio_cfg.get("disponibles", []) if e["nombre"] == dominio), None
    )
    if not email:
        print(f"[WARNING] No hay 'email_acme' para '{dominio}'; se omite la emisión del certificado.")
        return

    staging_flag = "--staging" if dominio_cfg.get("staging", False) else ""
    # Si el DNS no resolvía todavía en el arranque, el setup del Gateway dejó un
    # autofirmado de RESPALDO exactamente en live/{dominio} (ver
    # TLS_LETSENCRYPT_SETUP), y a veces junto a un renewal/{dominio}.conf VACÍO
    # (0 bytes -no es un lineage válido de certbot, solo un residuo de un
    # intento fallido anterior). certbot detecta cualquiera de los dos como
    # "ya existe" y, según el caso, o se niega a emitir ("live directory
    # exists") o crea un lineage duplicado con sufijo '-0001' en vez de
    # reutilizar el nombre -comprobado empíricamente en ambos casos. 'test -s'
    # (existe Y pesa >0) distingue un renewal.conf real de uno vacío; se limpia
    # también cualquier '-0001' que haya quedado de un intento previo así.
    limpiar_respaldo = (
        f"if sudo test -d /opt/sooniverse/letsencrypt/live/{dominio} "
        f"&& ! sudo test -s /opt/sooniverse/letsencrypt/renewal/{dominio}.conf; then "
        f"sudo rm -rf /opt/sooniverse/letsencrypt/live/{dominio} "
        f"/opt/sooniverse/letsencrypt/archive/{dominio} "
        f"/opt/sooniverse/letsencrypt/renewal/{dominio}.conf; fi; "
        f"sudo rm -rf /opt/sooniverse/letsencrypt/live/{dominio}-0001 "
        f"/opt/sooniverse/letsencrypt/archive/{dominio}-0001 "
        f"/opt/sooniverse/letsencrypt/renewal/{dominio}-0001.conf"
    )
    remote_cmd = (
        f"{limpiar_respaldo} "
        "&& sudo docker run --rm "
        "-v /opt/sooniverse/letsencrypt:/etc/letsencrypt "
        "-v /opt/sooniverse/certbot-www:/var/www/certbot "
        "certbot/certbot certonly --webroot -w /var/www/certbot --non-interactive --agree-tos "
        f"--cert-name {dominio} -m {email} -d {dominio} --keep-until-expiring {staging_flag} "
        f"&& cd {REMOTE_ROOT}/docker_images/gateway "
        "&& sudo docker compose exec -T proxy nginx -s reload"
    )

    sky = _sky_binary()
    if not sky:
        print("[WARNING] 'sky' no está en el PATH; no se pudo emitir el certificado.")
        return

    try:
        result = subprocess.run(
            [sky, "exec", gateway_cluster, remote_cmd], capture_output=True, text=True, timeout=180
        )
    except subprocess.TimeoutExpired:
        print("[WARNING] 'sky exec' del certbot excedió el tiempo de espera (180s).")
        if state and deployment_id:
            state.log_event(deployment_id, "dominio", "certbot", "warning", message="timeout")
        return

    if result.returncode != 0:
        detalle = (result.stderr.strip() or result.stdout.strip())[-500:]
        print(f"[WARNING] certbot falló (código {result.returncode}): {detalle}")
        if state and deployment_id:
            state.log_event(deployment_id, "dominio", "certbot", "warning", message=detalle)
    else:
        print(f"[OK] Certificado Let's Encrypt emitido/renovado y nginx recargado para '{dominio}'.")
        if state and deployment_id:
            state.log_event(deployment_id, "dominio", "certbot", "ok", message=dominio)


# 'capacidad' va después de 'capabilities' (el modelo ya está caliente, el pool
# sincronizado y las capacidades efectivas aplicadas) y antes de 'verify'.
# 'dominio' va justo después de 'gateway' (necesita el Gateway ya levantado y
# con su Elastic IP asociada -sky exec funcionando-) y antes de 'workers' (no
# depende de ellos ni ellos de él).
PHASE_ORDER = ["network", "gateway", "dominio", "workers", "endpoints", "capabilities", "capacidad", "verify"]

# Compatibilidad con los valores antiguos de --only (antes de la Fase 3).
_ONLY_LEGACY_ALIASES = {"all": set(PHASE_ORDER), "gateway": {"gateway"}, "workers": {"workers"}}


def _phases_for(only: str) -> set:
    if only in _ONLY_LEGACY_ALIASES:
        return _ONLY_LEGACY_ALIASES[only]
    if only in PHASE_ORDER:
        return {only}
    raise ValueError(f"--only inválido: {only}")


class RequiresDestroyError(Exception):
    """plan_changes() detectó un cambio que no es modificable en caliente."""


class NetworkNotProvisionedError(Exception):
    """gestion_red='auto' pero no hay NetworkOutputs disponibles (ni en memoria ni
    en el estado) al intentar lanzar el gateway o los workers. Lanzar de todos
    modos dejaría que SkyPilot eligiera la VPC por defecto de la cuenta en
    silencio -exactamente el bug real que motivó esta guarda."""


def _open_state_store(config: Dict[str, Any]):
    """Abre (o recupera) el despliegue activo en PostgreSQL. Si la BD no es
    alcanzable, lanza ANTES de que se cree nada en AWS (guardia de la Fase 2).

    Si ya existía un despliegue activo, compara su `config_snapshot` contra
    `config` con `plan_changes()`: si el plan exige destroy (p.ej. cambió
    `vpc_cidr`), aborta con un mensaje explícito en vez de intentar aplicar un
    cambio que AwsNetworkManager no puede hacer en caliente. Si el plan es
    aplicable, actualiza el snapshot para que la próxima comparación sea
    correcta.
    """
    from infra_state import PostgresInfraStateStore

    cliente = config["cliente"]
    red = config["red_y_aislamiento"]
    store = PostgresInfraStateStore()
    store.ping()  # aborta aquí si PostgreSQL no responde

    existing = store.get_active_deployment(cliente["id"], cliente["entorno"], red["region"])
    config_hash = config_hash_of(config)

    if existing and existing.get("config_snapshot"):
        plan = plan_changes(existing["config_snapshot"], config)
        if not plan.is_no_op:
            print("[CAMBIOS] plan_changes detectó diferencias respecto al despliegue activo:")
            print("\n".join(f"  {line}" for line in plan.summary().splitlines()))
            if plan.requires_destroy:
                raise RequiresDestroyError(
                    "Uno o más cambios requieren destroy + provision (no son modificables en "
                    "caliente): " + "; ".join(
                        c.field for c in plan.changes if c.classification == REQUIRES_DESTROY
                    ) + ". Corre 'destroy_infra.py' y luego 'generate_infra.py --run' de nuevo."
                )
            if plan.clusters_to_recreate:
                print(f"[CAMBIOS] Clústeres worker a relanzar: {', '.join(plan.clusters_to_recreate)}")

    deployment_id = store.open_deployment(
        client_id=cliente["id"],
        environment=cliente["entorno"],
        region=red["region"],
        config_hash=config_hash,
        config_snapshot=config,
    )
    if existing:
        store.update_config_snapshot(deployment_id, config_hash, config)
    return store, deployment_id


def deploy(
    config: Dict[str, Any],
    artefactos: Dict[str, Any],
    only: str = "all",
    out_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    dry_run: bool = False,
) -> None:
    """
    Máquina de fases (ver docs/01_FLUJO_DESPLIEGUE.md):
      network  -> AwsNetworkManager.provision() (VPC/subredes/NAT/SGs). Se omite
                  si 'gestion_red: existente'.
      gateway  -> sky launch del Gateway en la subred pública; captura su IP.
      workers  -> regenera el bastion con esa IP y lanza los workers en la
                  subred privada.
      endpoints-> sync_endpoints.py --apply (descubre IPs, recarga LiteLLM).
      capabilities -> scripts/test_model_capabilities.py --write-db (sondea el
                  modelo YA desplegado, persiste la verdad observada en
                  sooniverse.model_capability con política fail-closed) y
                  luego scripts/sync_openwebui_models.py (aplica esa verdad a
                  Open WebUI: modelos + flags de tareas automáticas). Best-effort
                  y NUNCA aborta el despliegue: un mismatch peligroso solo se
                  reporta como [WARNING] -la infra debe quedar usable aunque un
                  modelo mienta sobre sus capacidades declaradas.
      verify   -> scripts/verify_deployment.py (best-effort, no aborta 'all').
    Cada fase es reanudable: los `ensure_*` de AwsNetworkManager y el propio
    `sky launch` son idempotentes, así que repetir una fase ya aplicada es
    seguro y rápido.
    """
    out_dir = out_dir or REPO_ROOT
    config_path = config_path or (REPO_ROOT / "config_global.yaml")
    red = config["red_y_aislamiento"]
    phases = _phases_for(only)

    print("\n" + "=" * 74)
    print(" DESPLIEGUE SOONIVERSE - MÁQUINA DE FASES (FASE 3)")
    print("=" * 74)

    builder = TopologyBuilder(config)
    gateway_ip: Optional[str] = None
    state = None
    deployment_id = None

    # --- FASE: state (siempre, si gestion_red == auto) ----------------------
    if red.get("gestion_red", "auto") == "auto":
        if dry_run:
            # --dry-run no debe escribir en PostgreSQL: solo lee un despliegue
            # activo si ya existe, nunca abre uno nuevo.
            from infra_state import PostgresInfraStateStore

            state = PostgresInfraStateStore()
            state.ping()
            existing = state.get_active_deployment(
                config["cliente"]["id"], config["cliente"]["entorno"], red["region"]
            )
            deployment_id = existing["deployment_id"] if existing else None
            print(f"[ESTADO] (dry-run, solo lectura) deployment_id={deployment_id or '(ninguno todavía)'}")
            if existing and existing.get("config_snapshot"):
                plan = plan_changes(existing["config_snapshot"], config)
                if not plan.is_no_op:
                    print("[CAMBIOS] (dry-run) plan_changes respecto al despliegue activo:")
                    print("\n".join(f"  {line}" for line in plan.summary().splitlines()))
                    if plan.requires_destroy:
                        print("[CAMBIOS] Requeriría destroy + provision (no aplicable en caliente).")
        else:
            t0 = time.monotonic()
            state, deployment_id = _open_state_store(config)
            print(f"[ESTADO] deployment_id={deployment_id} ({time.monotonic() - t0:.1f}s)")

        # Si la red ya fue aprovisionada en una corrida anterior (o en una invocación
        # separada de --only network), reconstruye vpc_name/SG reales desde el estado
        # ANTES de las fases de gateway/workers -así --only gateway / --only workers
        # invocados solos siguen apuntando a la VPC correcta, no a la que SkyPilot
        # elegiría por defecto.
        if deployment_id and state:
            loaded_outputs = load_network_outputs_from_state(config, state, deployment_id)
            if loaded_outputs:
                builder.apply_network_outputs(loaded_outputs)

    # --- FASE: network --------------------------------------------------------
    if "network" in phases:
        print("\n--- [RED] Red AWS (VPC/subredes/NAT/Security Groups) ---")
        check_cidr_isolation(config)
        if red.get("gestion_red", "auto") == "auto" and dry_run and not deployment_id:
            # Sin despliegue previo: no hay nada que leer y, para no escribir en
            # PostgreSQL durante un dry-run, no se instancia AwsNetworkManager
            # (su constructor abriría un deployment_id nuevo si no se le pasa uno).
            print("[RED] --dry-run: no existe un despliegue previo para "
                  f"{config['cliente']['id']}/{config['cliente']['entorno']}/{red['region']}. "
                  "Se crearía una VPC, subredes, NAT, route tables y Security Groups nuevos.")
        elif red.get("gestion_red", "auto") == "auto":
            from aws_network import AwsNetworkManager

            spec = build_network_spec_from_config(config)
            mgr = AwsNetworkManager(spec, state=state, deployment_id=deployment_id)
            if dry_run:
                print("[RED] --dry-run: no se ejecuta ninguna llamada mutante a AWS.")
                for item in mgr.plan_destroy():
                    print(f"       (existente) {item.component} {item.aws_id}")
            else:
                t0 = time.monotonic()
                network_outputs = mgr.provision()
                print(f"[RED] VPC={network_outputs.vpc_id} ({network_outputs.vpc_name}) "
                      f"SG-gateway={network_outputs.sg_gateway_id} SG-workers={network_outputs.sg_workers_id} "
                      f"({time.monotonic() - t0:.1f}s)")
                if network_outputs.gateway_eip_public_ip:
                    print(
                        f"[RED] Elastic IP del Gateway reservada: {network_outputs.gateway_eip_public_ip} "
                        f"({network_outputs.gateway_eip_allocation_id}) -crea el registro DNS A con esta "
                        "IP antes de continuar. Ver Manual_Dominio_AWS.md."
                    )
                builder.apply_network_outputs(network_outputs)

                # El render de manifiestos depende de los IDs reales de red: regenerarlos ahora.
                artefactos = generate_manifests(config, out_dir, builder=builder)
        else:
            print("[SKIP] 'gestion_red: existente' -> se omite AwsNetworkManager (VPC/SGs manuales).")

    # --- FASE: gateway ----------------------------------------------------------
    if "gateway" in phases and artefactos.get("gateway") and dry_run:
        print("\n--- [GATEWAY] --dry-run: se lanzaría "
              f"'sky launch -y -c {builder.gateway_cluster} {artefactos['gateway']}' ---")
    elif "gateway" in phases and artefactos.get("gateway"):
        print("\n--- [GATEWAY] Nodo Gateway (público) ---")

        if red.get("gestion_red", "auto") == "auto" and not builder.has_network_outputs:
            # Guarda dura: sin esto, build_sky_gateway_config() produce "{}" en
            # silencio y SkyPilot lanza el gateway en la VPC por defecto de la
            # cuenta -no en la nuestra- (bug real encontrado en una corrida real:
            # `--only gateway` invocado sin que `--only network` hubiera corrido
            # antes, en el mismo proceso o en uno previo con estado persistido).
            raise NetworkNotProvisionedError(
                "gestion_red='auto' pero no hay red aprovisionada (ni en memoria ni en "
                "el estado de PostgreSQL) para este cliente/entorno/región. Corre primero "
                "'generate_infra.py --run --only network' (o '--run --only all')."
            )

        gw_cfg = builder.build_sky_gateway_config()
        gateway_env: Dict[str, str] = {}
        if gw_cfg:
            gw_cfg_path = out_dir / SKY_GATEWAY_CONFIG
            dump_yaml(gw_cfg, gw_cfg_path)
            gateway_env["SKYPILOT_CONFIG"] = str(gw_cfg_path)
            print(f"[INFO] SkyPilot usará {gw_cfg_path.name} (misma VPC que los workers)")

        t0 = time.monotonic()
        _run_sky(
            ["launch", "-y", "-c", builder.gateway_cluster, str(artefactos["gateway"])],
            env=gateway_env,
        )
        if state and deployment_id:
            state.log_event(deployment_id, "gateway", "sky_launch", "ok",
                             message=builder.gateway_cluster, duration_ms=int((time.monotonic() - t0) * 1000))

        net_outputs = getattr(builder, "_network_outputs", None)
        eip_alloc_id = getattr(net_outputs, "gateway_eip_allocation_id", None)
        if eip_alloc_id:
            try:
                associated_ip = _associate_gateway_eip(
                    builder.gateway_cluster, eip_alloc_id, red["region"], red.get("aws_profile"),
                )
                print(f"[GATEWAY] Elastic IP asociada: {associated_ip}")
                if state and deployment_id:
                    state.log_event(deployment_id, "gateway", "associate_eip", "ok", message=associated_ip)
            except GatewayEipAssociationError as exc:
                if state and deployment_id:
                    state.log_event(deployment_id, "gateway", "associate_eip", "error", message=str(exc))
                raise

    if artefactos.get("gateway") and not dry_run:
        gateway_ip = _gateway_public_ip(builder.gateway_cluster)
        print(f"[INFO] IP pública del Gateway: {gateway_ip or 'no disponible'}")

    # --- FASE: dominio (DNS + certbot; best-effort, nunca aborta) ---------------
    dominio_cfg_top = (config.get("gateway", {}).get("dominio") or {})
    if "dominio" in phases and dry_run:
        if dominio_cfg_top.get("habilitado", False):
            print(f"\n--- [DOMINIO] --dry-run: se verificaría el DNS de "
                  f"'{dominio_cfg_top.get('seleccionado')}' y se emitiría/renovaría el certificado ---")
        else:
            print("\n--- [DOMINIO] --dry-run: 'gateway.dominio.habilitado: false' -> [SKIP] ---")
    elif "dominio" in phases:
        print("\n--- [DOMINIO] Dominio propio + certificado (Let's Encrypt) ---")
        if not dominio_cfg_top.get("habilitado", False):
            print("[SKIP] 'gateway.dominio.habilitado: false' -> IP efímera, HTTP.")
        elif not gateway_ip:
            print("[WARNING] Sin IP del Gateway disponible (¿corriste antes la fase 'gateway'?); se omite.")
        else:
            run_dominio_phase(config, builder.gateway_cluster, gateway_ip, state, deployment_id)

    # --- FASE: workers (regenera el bastion con la IP real del gateway) --------
    if "workers" in phases and dry_run:
        clusters = ", ".join(builder.worker_cluster(wl["id"]) for wl in config["workloads"])
        print(f"\n--- [WORKERS] --dry-run: se lanzarían: {clusters} ---")
    elif "workers" in phases:
        print("\n--- [WORKERS] Workers vLLM (subred privada) ---")

        if red.get("gestion_red", "auto") == "auto" and not builder.has_network_outputs:
            raise NetworkNotProvisionedError(
                "gestion_red='auto' pero no hay red aprovisionada (ni en memoria ni en "
                "el estado de PostgreSQL) para este cliente/entorno/región. Corre primero "
                "'generate_infra.py --run --only network' (o '--run --only all')."
            )

        sky_cfg = builder.build_sky_workers_config(gateway_ip=gateway_ip)
        worker_env: Dict[str, str] = {}
        if sky_cfg:
            cfg_path = out_dir / SKY_WORKERS_CONFIG
            dump_yaml(sky_cfg, cfg_path)
            worker_env["SKYPILOT_CONFIG"] = str(cfg_path)
            print(f"[INFO] SkyPilot usará {cfg_path.name} (use_internal_ips + bastion)")

        for wl in config["workloads"]:
            cluster = builder.worker_cluster(wl["id"])
            manifest = artefactos["workers"][wl["id"]]
            print(f"\n> Workload '{wl['id']}' ({wl.get('replicas', 1)} nodo/s)")
            t0 = time.monotonic()
            # --retry-until-up: las instancias GPU sufren InsufficientInstanceCapacity
            # con bastante frecuencia (observado dos veces con g6.xlarge en
            # us-east-1a). No es un error del despliegue, es capacidad transitoria
            # de AWS, y sin reintento tumbaba el pipeline entero dejando la red y
            # el Gateway ya creados -y facturando- a medio camino.
            # No se puede reintentar en otra AZ: con `azs: 1` la subred privada
            # solo existe en una, y SkyPilot está anclado a ella por
            # `use_internal_ips`. Si esto se vuelve crónico, la salida es subir
            # `red_y_aislamiento.azs`.
            _run_sky(["launch", "-y", "--retry-until-up", "-c", cluster, str(manifest)],
                     env=worker_env)
            if state and deployment_id:
                state.log_event(deployment_id, "workers", "sky_launch", "ok",
                                 message=cluster, duration_ms=int((time.monotonic() - t0) * 1000))

    # --- FASE: endpoints --------------------------------------------------------
    if "endpoints" in phases and dry_run:
        print("\n--- [ENDPOINTS] --dry-run: se ejecutaría sync_endpoints.py --apply ---")
    elif "endpoints" in phases:
        print("\n--- [ENDPOINTS] Sincronización de endpoints en LiteLLM ---")
        sync_script = REPO_ROOT / "scripts" / "sync_endpoints.py"
        cmd = [sys.executable, str(sync_script), "--config", str(config_path), "--apply"]
        print(f"[EXEC] {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as exc:
            print(f"[WARNING] La sincronización automática falló (código {exc.returncode}).")
            print("          Reintenta manualmente: python scripts/sync_endpoints.py --apply")

    # --- FASE: capabilities (best-effort; no aborta el resto del pipeline) -----
    if "capabilities" in phases and dry_run:
        print("\n--- [CAPABILITIES] --dry-run: se ejecutaría test_model_capabilities.py --write-db ---")
    elif "capabilities" in phases:
        print("\n--- [CAPABILITIES] Sondeo de capacidades reales + sincronización con Open WebUI ---")
        caps_script = REPO_ROOT / "scripts" / "test_model_capabilities.py"
        caps_json = out_dir / ".sooniverse_capabilities.json"
        cmd = [
            sys.executable, str(caps_script),
            "--config", str(config_path),
            "--write-db",
            "--json", str(caps_json),
        ]
        print(f"[EXEC] {' '.join(cmd)}")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            print(f"[WARNING] test_model_capabilities.py reportó un mismatch peligroso "
                  f"(código {result.returncode}); revisa la tabla impresa arriba y "
                  "sooniverse.model_capability. No se aborta el despliegue.")

        sync_owui_script = REPO_ROOT / "scripts" / "sync_openwebui_models.py"
        if sync_owui_script.exists():
            sync_cmd = [sys.executable, str(sync_owui_script), "--config", str(config_path), "--apply"]
            print(f"[EXEC] {' '.join(sync_cmd)}")
            sync_result = subprocess.run(sync_cmd)
            if sync_result.returncode != 0:
                print(f"[WARNING] sync_openwebui_models.py falló (código {sync_result.returncode}); "
                      "reintenta manualmente: python scripts/sync_openwebui_models.py --apply")
        else:
            print("[SKIP] scripts/sync_openwebui_models.py no existe todavía.")

    # --- FASE: capacidad (best-effort; no aborta el resto del pipeline) --------
    cap_cfg = config.get("capacidad") or {}
    if "capacidad" in phases and not cap_cfg.get("habilitado", True):
        print("\n[SKIP] 'capacidad.habilitado: false' -> se omite el benchmark de capacidad.")
    elif "capacidad" in phases and dry_run:
        print("\n--- [CAPACIDAD] --dry-run: se ejecutaría benchmark_capacity.py --write-db ---")
        bench_script = REPO_ROOT / "scripts" / "benchmark_capacity.py"
        if bench_script.exists():
            subprocess.run([sys.executable, str(bench_script),
                            "--config", str(config_path), "--dry-run"])
    elif "capacidad" in phases:
        print("\n--- [CAPACIDAD] Benchmark de capacidad (rampa acotada) ---")
        bench_script = REPO_ROOT / "scripts" / "benchmark_capacity.py"
        if bench_script.exists():
            bench_json = out_dir / CAPACITY_CACHE
            cmd = [
                sys.executable, str(bench_script),
                "--config", str(config_path),
                "--write-db",
                "--json", str(bench_json),
            ]
            print(f"[EXEC] {' '.join(cmd)}")
            result = subprocess.run(cmd)
            if result.returncode != 0:
                print(f"[WARNING] benchmark_capacity.py terminó con código {result.returncode}; "
                      "revisa sooniverse.capacity_benchmark. No se aborta el despliegue.")
        else:
            print("[SKIP] scripts/benchmark_capacity.py no existe todavía.")

    # --- FASE: verify (best-effort; no aborta el resto del pipeline) -----------
    if "verify" in phases and dry_run:
        print("\n--- [VERIFY] --dry-run: se ejecutaría verify_deployment.py ---")
    elif "verify" in phases:
        print("\n--- [VERIFY] Verificación de despliegue ---")
        verify_script = REPO_ROOT / "scripts" / "verify_deployment.py"
        if verify_script.exists():
            result = subprocess.run(
                [sys.executable, str(verify_script), "--config", str(config_path)]
            )
            if result.returncode != 0:
                print(f"[WARNING] verify_deployment.py reportó fallos (código {result.returncode}).")
        else:
            print("[SKIP] scripts/verify_deployment.py no existe todavía.")

    if state and deployment_id and not dry_run:
        try:
            resources = state.list_resources(deployment_id)
            healthy = all(r.get("state") in ("active", "creating") for r in resources)
            state.set_deployment_status(deployment_id, "active" if healthy else "degraded")
        except Exception as exc:  # noqa: BLE001 - no debe tumbar el reporte final por un fallo de estado
            print(f"[WARNING] No se pudo actualizar el estado final del despliegue: {exc}")

    if gateway_ip:
        gw_cfg_final = config.get("gateway", {})
        tls_cfg_final = gw_cfg_final.get("tls", {}) or {}
        scheme = "https" if tls_cfg_final.get("habilitado") else "http"
        # Con dominio propio, el host de las URLs es el dominio (el único con el
        # que el certificado es válido) -no la IP, aunque sea la misma Elastic IP.
        host = tls_cfg_final.get("dominio") if scheme == "https" else gateway_ip
        print("\n" + "=" * 74)
        print(f" Chat (Open WebUI) : {scheme}://{host}/")
        print(f" API (LiteLLM)     : {scheme}://{host}/v1")
        print(f" Panel (Django)    : {scheme}://{host}/panel/")
        print(f" Salud (nginx)     : {scheme}://{host}/healthz")
        if scheme == "https":
            print(f" Acceso directo por IP (sin certificado válido): http://{gateway_ip}/")
        if gw_cfg_final.get("exponer_puertos_directos"):
            print(" [exponer_puertos_directos=true] También alcanzables: "
                  f":4000 (LiteLLM), :8080 (Open WebUI), :8000 (Django)")
        if deployment_id:
            print(f" deployment_id: {deployment_id}")
        print("=" * 74)


# =============================================================================
# CLI
# =============================================================================
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generador y aprovisionador multi-nodo de infraestructura Sooniverse (SkyPilot)."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help="Ruta al contrato central de configuración (p.ej. clients/acme/config_global.yaml)")
    parser.add_argument(
        "--out-dir", default=None,
        help="Directorio donde se escriben los manifiestos generados. Por defecto: la raíz del repo "
             "si --config es el config_global.yaml raíz (compatibilidad), o "
             ".artifacts/<cliente.id>-<entorno>/ para cualquier otro --config (multi-cliente).",
    )
    parser.add_argument("--run", action="store_true",
                        help="Aprovisiona la topología completa en AWS tras generar los manifiestos")
    parser.add_argument(
        # Derivado de PHASE_ORDER, no repetido a mano: añadir una fase nueva solo
        # exige tocar esa lista.
        "--only", choices=["all", *PHASE_ORDER],
        default="all",
        help="Limita el aprovisionamiento a una fase de la topología",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Con --run: solo genera manifiestos e imprime lo que se haría, sin llamadas mutantes a AWS/SkyPilot",
    )
    parser.add_argument("--init-db", action="store_true",
                        help="Ejecuta scripts/db_setup.py localmente (ignora el flag AUTO_INIT_DB)")
    parser.add_argument("--no-auto-init-db", action="store_true",
                        help="Fuerza AUTO_INIT_DB=false en esta ejecución sin editar el YAML")

    args = parser.parse_args()

    print("[SOONIVERSE INFRA] Leyendo contrato central...")
    try:
        config = load_config(args.config)

        if args.no_auto_init_db:
            config.setdefault("base_de_datos", {})["AUTO_INIT_DB"] = False
            print("[INFO] Override de CLI: AUTO_INIT_DB=false")

        out_dir = Path(args.out_dir) if args.out_dir else artifacts_dir_for(Path(args.config), config)
        if out_dir != REPO_ROOT:
            print(f"[INFO] Artefactos de este cliente en: {out_dir.relative_to(REPO_ROOT)}/")
        artefactos = generate_manifests(config, out_dir)

        auto_init = config.get("base_de_datos", {}).get("AUTO_INIT_DB", True)
        print(f"[INFO] AUTO_INIT_DB = {str(auto_init).lower()} "
              f"({'la BD se inicializa en el despliegue' if auto_init else 'inicialización manual'})")

        if args.init_db:
            print("\n--- Inicialización local de la base de datos ---")
            subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "db_setup.py"), "--refresh"], check=True)

        if args.run:
            deploy(
                config, artefactos, only=args.only,
                out_dir=out_dir, config_path=Path(args.config),
                dry_run=args.dry_run,
            )
        else:
            print("\n[INFO] Para aprovisionar la topología en AWS:")
            print("       python scripts/generate_infra.py --run")
            print("       python scripts/generate_infra.py --run --dry-run          # plan, sin tocar AWS")
            print("       python scripts/generate_infra.py --run --only network     # solo la capa de red")
            print("       python scripts/generate_infra.py --run --only gateway     # solo el gateway")

    except ConfigValidationError as e:
        print(f"\n[ERROR DE CONFIGURACIÓN] {e}", file=sys.stderr)
        return 1
    except RequiresDestroyError as e:
        print(f"\n[CAMBIOS NO APLICABLES EN CALIENTE] {e}", file=sys.stderr)
        return 1
    except NetworkNotProvisionedError as e:
        print(f"\n[RED NO APROVISIONADA] {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - frontera del CLI
        print(f"\n[ERROR INESPERADO] {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
