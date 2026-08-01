"""
Pruebas de scripts/infra_state.py::PostgresInfraStateStore contra una base
PostgreSQL real (la de .env). Usa un client_id/environment/region de prueba
aislado ("pytest-infra-state"/"test"/"us-test-1") que nunca colisiona con
despliegues reales, y limpia sus propias filas al terminar.

Si la BD no es alcanzable, el módulo entero se salta (no rompe `pytest` sin
credenciales/red, como exige el criterio de aceptación del proyecto).
"""

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from db_setup import DbSetupError, connect, resolve_db_config  # noqa: E402
from infra_state import PostgresInfraStateStore  # noqa: E402

ENV_PATH = REPO_ROOT / ".env"
TEST_CLIENT = "pytest-infra-state"
TEST_ENV = "test"
TEST_REGION = "us-test-1"


def _db_reachable() -> bool:
    try:
        conn = connect(resolve_db_config(ENV_PATH))
        conn.close()
        return True
    except DbSetupError:
        return False


pytestmark = pytest.mark.skipif(not _db_reachable(), reason="PostgreSQL de .env no alcanzable")


@pytest.fixture
def store():
    s = PostgresInfraStateStore(env_path=ENV_PATH, mirror_dir=REPO_ROOT / "tests" / "_tmp_mirrors")
    (REPO_ROOT / "tests" / "_tmp_mirrors").mkdir(parents=True, exist_ok=True)
    yield s

    # Limpieza: borra cualquier despliegue de prueba y su cascada de recursos/eventos.
    conn = s._connect()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM sooniverse.infra_event WHERE deployment_id IN "
                "(SELECT deployment_id FROM sooniverse.infra_deployment WHERE client_id = %s)",
                (TEST_CLIENT,),
            )
            cur.execute("DELETE FROM sooniverse.infra_deployment WHERE client_id = %s", (TEST_CLIENT,))
    s.close()


def test_open_deployment_creates_row_and_is_idempotent(store):
    dep1 = store.open_deployment(TEST_CLIENT, TEST_ENV, TEST_REGION)
    dep2 = store.open_deployment(TEST_CLIENT, TEST_ENV, TEST_REGION)
    assert dep1 == dep2
    uuid.UUID(dep1)  # no lanza si es un UUID válido


def test_config_snapshot_strips_secrets(store):
    snapshot = {
        "cliente": {"id": TEST_CLIENT},
        "gateway": {"litellm": {"master_key_env": "LITELLM_MASTER_KEY"}},
        "secrets": {"DB_PASSWORD": "hunter2", "AWS_SECRET_ACCESS_KEY": "abc123"},
        "nested": [{"SECRET_KEY": "no-deberia-quedar", "region": "us-east-1"}],
    }
    deployment_id = store.open_deployment(TEST_CLIENT, TEST_ENV, TEST_REGION, config_snapshot=snapshot)

    conn = store._connect()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT config_snapshot FROM sooniverse.infra_deployment WHERE deployment_id = %s",
            (deployment_id,),
        )
        stored = cur.fetchone()[0]

    assert "DB_PASSWORD" not in stored.get("secrets", {})
    assert "AWS_SECRET_ACCESS_KEY" not in stored.get("secrets", {})
    assert stored["nested"][0].get("SECRET_KEY") is None
    assert stored["nested"][0]["region"] == "us-east-1"
    assert stored["cliente"]["id"] == TEST_CLIENT


def test_record_and_list_resources_delete_order(store):
    deployment_id = store.open_deployment(TEST_CLIENT, TEST_ENV, TEST_REGION)

    store.record_resource(
        deployment_id, resource_type="vpc", component="vpc", aws_id="vpc-test123",
        region=TEST_REGION, delete_order=80, managed_by_us=True, state="active",
    )
    store.record_resource(
        deployment_id, resource_type="sg-workers", component="sg-workers", aws_id="sg-test123",
        region=TEST_REGION, delete_order=10, managed_by_us=True, state="active",
    )

    ordered = store.resources_in_delete_order(deployment_id)
    assert [r["component"] for r in ordered] == ["sg-workers", "vpc"]


def test_record_resource_upsert_does_not_duplicate(store):
    deployment_id = store.open_deployment(TEST_CLIENT, TEST_ENV, TEST_REGION)
    for _ in range(2):
        store.record_resource(
            deployment_id, resource_type="vpc", component="vpc", aws_id="vpc-dup123",
            region=TEST_REGION, delete_order=80, managed_by_us=True, state="active",
        )
    resources = store.list_resources(deployment_id)
    assert len([r for r in resources if r["aws_id"] == "vpc-dup123"]) == 1


def test_mark_resource_state_and_only_active_filter(store):
    deployment_id = store.open_deployment(TEST_CLIENT, TEST_ENV, TEST_REGION)
    store.record_resource(
        deployment_id, resource_type="nat", component="nat", aws_id="nat-test123",
        region=TEST_REGION, delete_order=30, managed_by_us=True, state="active",
    )
    store.mark_resource_state(deployment_id, "nat-test123", "deleted")

    active_only = store.list_resources(deployment_id, only_active=True)
    all_resources = store.list_resources(deployment_id, only_active=False)
    assert "nat-test123" not in {r["aws_id"] for r in active_only}
    assert "nat-test123" in {r["aws_id"] for r in all_resources}


def test_close_deployment_sets_destroyed_and_get_active_returns_none(store):
    deployment_id = store.open_deployment(TEST_CLIENT, TEST_ENV, TEST_REGION)
    store.close_deployment(deployment_id)
    assert store.get_active_deployment(TEST_CLIENT, TEST_ENV, TEST_REGION) is None


def test_local_mirror_file_written(store):
    deployment_id = store.open_deployment(TEST_CLIENT, TEST_ENV, TEST_REGION)
    mirror_path = store.mirror_dir / f".sooniverse_state.{TEST_CLIENT}-{TEST_ENV}.json"
    assert mirror_path.exists()
    assert deployment_id in mirror_path.read_text(encoding="utf-8")
    mirror_path.unlink()
