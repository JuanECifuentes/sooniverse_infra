"""
Cubre un bug real encontrado en una prueba de despliegue real con dominio:
check_domain_certificate_valid() leía ctx.config["gateway"]["tls"], que viene
de un yaml.safe_load() plano (ver verify_deployment.py::main) SIN pasar por la
derivación dominio.habilitado -> tls.* de generate_infra.py::load_config. Con
el contrato tal cual lo rellena el operador (tls.habilitado se deja en el
default de fábrica, false; el dominio real vive en gateway.dominio), la check
reportaba "N/A: sin dominio propio configurado" incluso con un dominio real
desplegado y su certificado de Let's Encrypt sirviendo tráfico de verdad.
"""

import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_deployment as vd


def _ctx(gateway_cfg):
    return vd.VerificationContext(config={"gateway": gateway_cfg})


def test_reporta_na_sin_bloque_dominio():
    ctx = _ctx({"tls": {"habilitado": False, "modo": "self-signed", "dominio": None}})
    result = vd.check_domain_certificate_valid(ctx)
    assert result.status == "N/A"
    assert result.critical is False


def test_reporta_na_con_dominio_deshabilitado_aunque_tls_diga_lo_contrario():
    ctx = _ctx({
        "dominio": {"habilitado": False, "seleccionado": None},
        "tls": {"habilitado": True, "dominio": "ia.sooniverse.co"},
    })
    result = vd.check_domain_certificate_valid(ctx)
    assert result.status == "N/A"


def test_no_reporta_na_con_dominio_real_configurado_aunque_tls_este_en_default(monkeypatch):
    """El caso real que falló: el contrato tal cual lo deja el operador -tls.*
    en su default de fábrica, la verdad vive en gateway.dominio-."""
    ctx = _ctx({
        "dominio": {"habilitado": True, "seleccionado": "ia.sooniverse.co"},
        "tls": {"habilitado": False, "modo": "self-signed", "dominio": None},
    })

    calls = {}

    def fake_create_connection(addr, timeout=10):
        calls["addr"] = addr
        raise OSError("sin red en el test -solo importa que no sea N/A")

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)

    result = vd.check_domain_certificate_valid(ctx)

    assert result.status != "N/A"
    assert calls["addr"] == ("ia.sooniverse.co", 443)
