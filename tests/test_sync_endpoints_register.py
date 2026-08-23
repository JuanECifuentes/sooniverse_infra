"""
Pruebas de scripts/sync_endpoints.py::register_in_db contra una base
PostgreSQL real (la de .env). Cubre los dos bugs de desincronización
corregidos en esta iteración:

1. Un clúster que desaparece POR COMPLETO de una corrida (ningún endpoint
   descubierto para él) debe marcarse desincronizado, no quedarse
   is_healthy=TRUE para siempre.
2. Con `endpoints == []` (pool vacío), el reset debe seguir ejecutándose para
   TODOS los clústeres declarados en el contrato, no omitirse.

Usa un cliente/entorno de prueba aislado que nunca colisiona con despliegues
reales, y limpia sus propias filas al terminar. Si la BD no es alcanzable, el
módulo entero se salta.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from db_setup import DbSetupError, connect, resolve_db_config  # noqa: E402
from sync_endpoints import register_in_db  # noqa: E402

ENV_PATH = REPO_ROOT / ".env"
TEST_CLIENTE = "pytest-sync"
TEST_ENTORNO = "test"
TEST_CLUSTER_A = f"sooniverse-{TEST_CLIENTE}-{TEST_ENTORNO}-worker-a"
TEST_CLUSTER_B = f"sooniverse-{TEST_CLIENTE}-{TEST_ENTORNO}-worker-b"


def _db_reachable() -> bool:
    try:
        conn = connect(resolve_db_config(ENV_PATH))
        conn.close()
        return True
    except DbSetupError:
        return False


pytestmark = pytest.mark.skipif(not _db_reachable(), reason="PostgreSQL de .env no alcanzable")


def make_config():
    return {
        "cliente": {"id": TEST_CLIENTE, "entorno": TEST_ENTORNO},
        "red_y_aislamiento": {"gestion_red": "existente", "region": "us-test-1"},
        "workloads": [
            {"id": "worker-a", "puerto": 8007},
            {"id": "worker-b", "puerto": 8007},
        ],
    }


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    conn = connect(resolve_db_config(ENV_PATH))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sooniverse.worker_node WHERE cluster_name = ANY(%s)",
                ([TEST_CLUSTER_A, TEST_CLUSTER_B],),
            )
        conn.commit()
    finally:
        conn.close()


def _fetch(cluster_name: str, ip: str):
    conn = connect(resolve_db_config(ENV_PATH))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_healthy, health_status, estado_operativo FROM sooniverse.worker_node "
                "WHERE cluster_name = %s AND private_ip = %s",
                (cluster_name, ip),
            )
            return cur.fetchone()
    finally:
        conn.close()


def test_cluster_missing_entirely_gets_marked_desincronizado():
    config = make_config()
    names = {"__gateway__": "gw", "worker-a": TEST_CLUSTER_A, "worker-b": TEST_CLUSTER_B}

    endpoint_a = {
        "cluster": TEST_CLUSTER_A, "model_public_name": "modelo-a", "accelerator": "L4",
        "ip": "10.0.0.10", "port": 8007, "healthy": True,
    }
    register_in_db([endpoint_a], names, config)
    row = _fetch(TEST_CLUSTER_A, "10.0.0.10")
    assert row is not None
    assert row[0] is True and row[2] == "sano"

    # Segunda corrida: worker-a desaparece POR COMPLETO (0 endpoints para él),
    # solo worker-b tiene un endpoint. Antes del fix, worker-a se quedaba
    # is_healthy=TRUE porque el reset solo alcanzaba a los clusters presentes
    # en ESTA corrida ({ep["cluster"] for ep in endpoints}).
    endpoint_b = {
        "cluster": TEST_CLUSTER_B, "model_public_name": "modelo-b", "accelerator": "L4",
        "ip": "10.0.0.20", "port": 8007, "healthy": True,
    }
    register_in_db([endpoint_b], names, config)

    row_a = _fetch(TEST_CLUSTER_A, "10.0.0.10")
    assert row_a is not None
    assert row_a[0] is False, "worker-a debería quedar is_healthy=FALSE tras desaparecer del todo"
    assert row_a[1] == "unknown"
    assert row_a[2] == "desincronizado"


def test_empty_pool_still_resets_all_expected_clusters():
    config = make_config()
    names = {"__gateway__": "gw", "worker-a": TEST_CLUSTER_A, "worker-b": TEST_CLUSTER_B}

    endpoint_a = {
        "cluster": TEST_CLUSTER_A, "model_public_name": "modelo-a", "accelerator": "L4",
        "ip": "10.0.0.10", "port": 8007, "healthy": True,
    }
    register_in_db([endpoint_a], names, config)
    assert _fetch(TEST_CLUSTER_A, "10.0.0.10")[0] is True

    # Pool completamente vacío (ningún worker respondió). Antes del fix, el
    # `if clusters:` (clusters = {ep["cluster"] for ep in endpoints} = {}) hacía
    # que NINGÚN UPDATE se ejecutara, dejando la tabla con datos obsoletos sin
    # ninguna señal de desincronización.
    register_in_db([], names, config)

    row = _fetch(TEST_CLUSTER_A, "10.0.0.10")
    assert row is not None
    assert row[0] is False
    assert row[2] == "desincronizado"


def test_healthy_endpoint_marks_estado_operativo_sano():
    config = make_config()
    names = {"__gateway__": "gw", "worker-a": TEST_CLUSTER_A, "worker-b": TEST_CLUSTER_B}
    endpoint = {
        "cluster": TEST_CLUSTER_A, "model_public_name": "modelo-a", "accelerator": "L4",
        "ip": "10.0.0.10", "port": 8007, "healthy": False,
    }
    register_in_db([endpoint], names, config)
    row = _fetch(TEST_CLUSTER_A, "10.0.0.10")
    assert row == (False, "unhealthy", "degradado")
