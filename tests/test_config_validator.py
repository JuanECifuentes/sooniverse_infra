"""
Pruebas de scripts/generate_infra.py::ConfigValidator. Casos válidos e
inválidos del contrato; no requiere AWS ni PostgreSQL.
"""

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_infra import ConfigValidationError, ConfigValidator  # noqa: E402


def load_base_config():
    with (REPO_ROOT / "config_global.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clone(config):
    return yaml.safe_load(yaml.dump(config))


def test_base_config_is_valid():
    ConfigValidator.validate(load_base_config())


# -- cliente ------------------------------------------------------------------
@pytest.mark.parametrize("bad_id", ["ACME", "acme_corp", "a" * 21, "", "acme corp"])
def test_invalid_client_id_rejected(bad_id):
    cfg = clone(load_base_config())
    cfg["cliente"]["id"] = bad_id
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_invalid_entorno_rejected():
    cfg = clone(load_base_config())
    cfg["cliente"]["entorno"] = "qa"
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_invalid_modo_rejected():
    cfg = clone(load_base_config())
    cfg["cliente"]["modo"] = "on-prem"
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


# -- red_y_aislamiento ----------------------------------------------------------
def test_gestion_red_invalid_rejected():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["gestion_red"] = "manual"
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_gestion_red_existente_skips_auto_validation():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["gestion_red"] = "existente"
    del cfg["red_y_aislamiento"]["vpc_cidr"]  # no debería hacer falta en modo 'existente'
    ConfigValidator.validate(cfg)  # no debe lanzar


def test_nat_gateway_modo_invalid_rejected():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["nat_gateway"]["modo"] = "doble"
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_azs_must_be_positive():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["azs"] = 0
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_invalid_vpc_cidr_rejected():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["vpc_cidr"] = "not-a-cidr"
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_private_without_nat_and_without_s3_endpoint_rejected():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["nat_gateway"]["modo"] = "none"
    cfg["red_y_aislamiento"]["vpc_endpoints"]["s3"] = False
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_private_without_nat_but_with_s3_endpoint_is_valid():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["nat_gateway"]["modo"] = "none"
    cfg["red_y_aislamiento"]["vpc_endpoints"]["s3"] = True
    ConfigValidator.validate(cfg)  # no debe lanzar


def test_subnet_cidr_not_contained_in_vpc_cidr_rejected():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["subredes"]["publicas"] = ["192.168.1.0/24"]
    cfg["red_y_aislamiento"]["subredes"]["privadas"] = ["10.0.128.0/20"]
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_overlapping_subnet_cidrs_rejected():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["subredes"]["publicas"] = ["10.0.0.0/20"]
    cfg["red_y_aislamiento"]["subredes"]["privadas"] = ["10.0.0.0/22"]
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_non_overlapping_explicit_subnet_cidrs_valid():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["subredes"]["publicas"] = ["10.0.0.0/20"]
    cfg["red_y_aislamiento"]["subredes"]["privadas"] = ["10.0.128.0/20"]
    ConfigValidator.validate(cfg)  # no debe lanzar


# -- gateway --------------------------------------------------------------------
def test_invalid_lb_strategy_rejected():
    cfg = clone(load_base_config())
    cfg["gateway"]["load_balancing_strategy"] = "round-robin-clasico"
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_tls_enabled_without_domain_rejected():
    cfg = clone(load_base_config())
    cfg["gateway"]["tls"] = {"habilitado": True}
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_tls_enabled_with_unimplemented_mode_rejected():
    cfg = clone(load_base_config())
    cfg["gateway"]["tls"] = {"habilitado": True, "dominio": "x.example.com", "modo": "letsencrypt"}
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_tls_enabled_self_signed_with_domain_is_valid():
    cfg = clone(load_base_config())
    cfg["gateway"]["tls"] = {"habilitado": True, "dominio": "x.example.com", "modo": "self-signed"}
    ConfigValidator.validate(cfg)  # no debe lanzar


def test_tls_disabled_ignores_domain_requirement():
    cfg = clone(load_base_config())
    cfg["gateway"]["tls"] = {"habilitado": False}
    ConfigValidator.validate(cfg)  # no debe lanzar


# -- base_de_datos ----------------------------------------------------------------
def test_missing_auto_init_db_rejected():
    cfg = clone(load_base_config())
    del cfg["base_de_datos"]["AUTO_INIT_DB"]
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_nonexistent_schema_dir_rejected():
    cfg = clone(load_base_config())
    cfg["base_de_datos"]["schema_dir"] = "no/existe/este/directorio"
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


# -- workloads --------------------------------------------------------------------
def test_duplicate_workload_id_rejected():
    cfg = clone(load_base_config())
    dup = clone(cfg["workloads"][0])
    cfg["workloads"].append(dup)
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_invalid_tipo_tarea_rejected():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["tipo_tarea"] = "vision"
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_zero_gpus_rejected():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["cantidad_gpus"] = 0
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_missing_workloads_section_rejected():
    cfg = clone(load_base_config())
    cfg["workloads"] = []
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


# -- capacidades --------------------------------------------------------------
def test_capacidades_non_bool_vision_rejected():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["capacidades"]["vision"] = "si"
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_capacidades_tool_calling_without_parser_rejected():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["capacidades"]["tool_calling"] = True
    cfg["workloads"][0]["capacidades"]["tool_call_parser"] = None
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_capacidades_tool_calling_with_parser_is_valid():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["capacidades"]["tool_calling"] = True
    cfg["workloads"][0]["capacidades"]["tool_call_parser"] = "hermes"
    ConfigValidator.validate(cfg)


def test_capacidades_absent_is_valid():
    cfg = clone(load_base_config())
    del cfg["workloads"][0]["capacidades"]
    ConfigValidator.validate(cfg)


# -- concurrencia de vLLM ------------------------------------------------------
def test_concurrencia_ausente_es_valida():
    """Sección opcional: build_worker() aplica los defaults (16 / 8192)."""
    cfg = clone(load_base_config())
    del cfg["workloads"][0]["concurrencia"]
    ConfigValidator.validate(cfg)


def test_concurrencia_max_num_seqs_fuera_de_rango_rechazada():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["concurrencia"]["max_num_seqs"] = 0
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)

    cfg["workloads"][0]["concurrencia"]["max_num_seqs"] = 99999
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_concurrencia_batched_tokens_menor_que_seqs_rechazado():
    """Con menos tokens por paso que secuencias en vuelo, al menos una secuencia
    no podría decodificar ni un token por paso del planificador."""
    cfg = clone(load_base_config())
    cfg["workloads"][0]["concurrencia"] = {"max_num_seqs": 2048, "max_num_batched_tokens": 1024}
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_concurrencia_batched_tokens_minimo():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["concurrencia"]["max_num_batched_tokens"] = 512
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


# -- benchmark de capacidad ----------------------------------------------------
def test_capacidad_ausente_es_valida():
    """Compatibilidad hacia atrás: un contrato anterior a esta sección sigue valiendo."""
    cfg = clone(load_base_config())
    del cfg["capacidad"]
    ConfigValidator.validate(cfg)


def test_capacidad_niveles_no_crecientes_rechazados():
    cfg = clone(load_base_config())
    cfg["capacidad"]["niveles_concurrencia"] = [1, 4, 2, 8]
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_capacidad_niveles_con_repetidos_rechazados():
    cfg = clone(load_base_config())
    cfg["capacidad"]["niveles_concurrencia"] = [1, 2, 2, 4]
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_capacidad_niveles_vacios_rechazados():
    cfg = clone(load_base_config())
    cfg["capacidad"]["niveles_concurrencia"] = []
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_capacidad_presupuesto_insuficiente_rechazado():
    """La regla que convierte 'rampa acotada' en una garantía del contrato y no
    en una intención: sin ella, 10 niveles x 60s se comerían 10 min de GPU en
    cada despliegue sin que nadie lo notara."""
    cfg = clone(load_base_config())
    cfg["capacidad"]["niveles_concurrencia"] = [1, 2, 4, 8, 16, 32]
    cfg["capacidad"]["segundos_por_nivel"] = 60
    cfg["capacidad"]["presupuesto_segundos"] = 120
    with pytest.raises(ConfigValidationError) as exc:
        ConfigValidator.validate(cfg)
    assert "presupuesto_segundos" in str(exc.value)


def test_capacidad_origen_invalido_rechazado():
    cfg = clone(load_base_config())
    cfg["capacidad"]["origen"] = "portatil"
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_capacidad_umbral_error_fuera_de_rango_rechazado():
    cfg = clone(load_base_config())
    cfg["capacidad"]["umbral_error_pct"] = 0
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)

    cfg["capacidad"]["umbral_error_pct"] = 150
    with pytest.raises(ConfigValidationError):
        ConfigValidator.validate(cfg)


def test_capacidad_deshabilitada_es_valida():
    cfg = clone(load_base_config())
    cfg["capacidad"]["habilitado"] = False
    ConfigValidator.validate(cfg)
