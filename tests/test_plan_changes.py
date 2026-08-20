"""
Pruebas de scripts/generate_infra.py::plan_changes() -- clasifica cada
diferencia entre un config_snapshot activo y el nuevo contrato en
no-op | in-place | recreate-cluster | requires-destroy. No requiere AWS ni BD.
"""

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_infra import (  # noqa: E402
    IN_PLACE,
    NO_OP,
    RECREATE_CLUSTER,
    REQUIRES_DESTROY,
    plan_changes,
)


def load_base_config():
    with (REPO_ROOT / "config_global.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clone(config):
    return yaml.safe_load(yaml.dump(config))


def test_identical_config_is_no_op():
    cfg = load_base_config()
    plan = plan_changes(clone(cfg), clone(cfg))
    assert plan.is_no_op
    assert plan.changes == []


def test_vpc_cidr_change_requires_destroy():
    old = load_base_config()
    new = clone(old)
    new["red_y_aislamiento"]["vpc_cidr"] = "10.9.0.0/16"

    plan = plan_changes(old, new)
    assert not plan.is_no_op
    assert plan.requires_destroy
    assert any(c.classification == REQUIRES_DESTROY and c.field == "red_y_aislamiento.vpc_cidr" for c in plan.changes)


def test_azs_change_requires_destroy():
    old = load_base_config()
    new = clone(old)
    new["red_y_aislamiento"]["azs"] = 2

    plan = plan_changes(old, new)
    assert plan.requires_destroy


def test_nat_mode_change_requires_destroy():
    old = load_base_config()
    new = clone(old)
    new["red_y_aislamiento"]["nat_gateway"]["modo"] = "per-az"

    plan = plan_changes(old, new)
    assert plan.requires_destroy
    assert any(c.field == "red_y_aislamiento.nat_gateway.modo" for c in plan.changes)


def test_cidr_permitido_gateway_change_is_in_place():
    old = load_base_config()
    new = clone(old)
    new["red_y_aislamiento"]["cidr_permitido_gateway"] = "1.2.3.4/32"

    plan = plan_changes(old, new)
    assert not plan.requires_destroy
    assert not plan.clusters_to_recreate
    assert all(c.classification == IN_PLACE for c in plan.changes)


def test_cidr_admin_ssh_change_is_in_place():
    old = load_base_config()
    new = clone(old)
    new["red_y_aislamiento"]["cidr_admin_ssh"] = "5.6.7.8/32"

    plan = plan_changes(old, new)
    assert not plan.requires_destroy
    assert all(c.classification == IN_PLACE for c in plan.changes)


def test_load_balancing_strategy_change_is_in_place():
    old = load_base_config()
    new = clone(old)
    new["gateway"]["load_balancing_strategy"] = "simple-shuffle"

    plan = plan_changes(old, new)
    assert not plan.is_no_op
    assert all(c.classification == IN_PLACE for c in plan.changes)


def test_replicas_change_recreates_that_cluster():
    old = load_base_config()
    new = clone(old)
    new["workloads"][0]["replicas"] = 2

    plan = plan_changes(old, new)
    assert plan.clusters_to_recreate == [old["workloads"][0]["id"]]
    assert not plan.requires_destroy


def test_accelerator_change_recreates_cluster():
    old = load_base_config()
    new = clone(old)
    new["workloads"][0]["accelerator"] = "A100"

    plan = plan_changes(old, new)
    assert plan.clusters_to_recreate == [old["workloads"][0]["id"]]


def test_nombre_publico_change_is_in_place_not_recreate():
    old = load_base_config()
    new = clone(old)
    new["workloads"][0]["nombre_publico"] = "otro-nombre"

    plan = plan_changes(old, new)
    assert not plan.clusters_to_recreate
    assert all(c.classification == IN_PLACE for c in plan.changes)


def test_added_workload_is_recreate_cluster():
    old = load_base_config()
    new = clone(old)
    extra = clone(old["workloads"][0])
    extra["id"] = "segundo-modelo"
    new["workloads"].append(extra)

    plan = plan_changes(old, new)
    assert "segundo-modelo" in plan.clusters_to_recreate


def test_removed_workload_is_recreate_cluster():
    old = load_base_config()
    new = clone(old)
    removed_id = new["workloads"].pop()["id"]

    plan = plan_changes(old, new)
    assert removed_id in plan.clusters_to_recreate


def test_no_previous_snapshot_treated_as_all_new():
    new = load_base_config()
    plan = plan_changes(None, new)
    # Sin snapshot previo, cada workload existente aparece como "añadido".
    assert set(plan.clusters_to_recreate) == {wl["id"] for wl in new["workloads"]}


def test_mixed_changes_classified_independently():
    old = load_base_config()
    new = clone(old)
    new["gateway"]["load_balancing_strategy"] = "least-busy"
    new["workloads"][0]["replicas"] = 3
    new["red_y_aislamiento"]["vpc_cidr"] = "10.20.0.0/16"

    plan = plan_changes(old, new)
    classifications = {c.classification for c in plan.changes}
    assert classifications == {IN_PLACE, RECREATE_CLUSTER, REQUIRES_DESTROY}


def test_cambiar_concurrencia_exige_relanzar_el_worker():
    """max_num_seqs es una bandera de arranque de vLLM: no se puede aplicar
    sobre un proceso vivo, pero tampoco exige destruir la infraestructura."""
    base = load_base_config()
    nuevo = clone(base)
    nuevo["workloads"][0]["concurrencia"]["max_num_seqs"] = 32

    plan = plan_changes(base, nuevo)
    assert plan.requires_destroy is False
    assert plan.clusters_to_recreate == [base["workloads"][0]["id"]]
    assert any(c.classification == RECREATE_CLUSTER and "concurrencia" in c.field
               for c in plan.changes)


def test_seccion_capacidad_no_afecta_al_plan_de_infraestructura():
    """El benchmark es una fase de medición: cambiar sus parámetros no debe
    tocar ni un recurso de AWS."""
    base = load_base_config()
    nuevo = clone(base)
    nuevo["capacidad"]["segundos_por_nivel"] = 5
    assert plan_changes(base, nuevo).is_no_op
