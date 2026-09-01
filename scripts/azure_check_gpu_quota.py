#!/usr/bin/env python3
"""
==============================================================================
Sooniverse Infra - Utilidad de cuotas de GPU en Azure
==============================================================================
No forma parte del pipeline de despliegue (`generate_infra.py`/`destroy_infra.py`)
-es una herramienta de diagnóstico para responder, antes de tocar
`config_global.yaml`, tres preguntas sobre una suscripción Azure real:

  1. ¿Qué SKUs de VM con GPU están REALMENTE visibles para esta suscripción,
     en qué regiones? (--skus)
  2. ¿Cuál es la cuota de vCPU ACTUAL para cada familia GPU en una región?
     (--quota)
  3. Pedir un aumento de cuota para una familia+región (--request-increase).
     Usa el mismo mecanismo (Microsoft.Quota) que la página "My quotas" del
     Portal -no hay CLI de az preinstalada garantizada, así que esto habla
     directo con el SDK de Azure (azure-mgmt-quota) con las credenciales del
     Service Principal en .env (ver .env.example, AZURE_*).

IMPORTANTE (ver docs/README.md §7.1): NO existe hoy un tamaño de VM Azure con
GPU L4 o L40S en el catálogo público de Azure (verificado contra
`resource_skus.list()`; ambas solo aparecen en Azure Local/Stack HCI, no en
regiones públicas) -el mapeo cloud-agnóstico de `accelerator` en
config_global.yaml no puede pedir "L4" en Azure. Las alternativas reales más
cercanas son T4 (`Standard NCASv3_T4 Family`) y A10 (`StandardNVADSA10v5Family`).

Uso:
    python scripts/azure_check_gpu_quota.py --skus
    python scripts/azure_check_gpu_quota.py --quota southcentralus
    python scripts/azure_check_gpu_quota.py --request-increase southcentralus "StandardNVADSA10v5Family" 16
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from azure.core.exceptions import HttpResponseError
    from azure.identity import ClientSecretCredential
    from azure.mgmt.compute import ComputeManagementClient
    from azure.mgmt.quota import QuotaMgmtClient
    from azure.mgmt.quota.models import CurrentQuotaLimitBase, LimitObject, QuotaProperties, ResourceName
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Falta el SDK de Azure. Instala con: pip install azure-identity azure-mgmt-compute azure-mgmt-quota"
    ) from exc

GPU_NAME_RE = re.compile(r"^Standard_(NC|ND|NV)", re.IGNORECASE)


def _load_env(env_path: Path) -> None:
    """Carga AZURE_* de .env en os.environ si no están ya seteadas (mismo
    mecanismo/prioridad conceptual que db_setup.resolve_db_config: variables ya
    presentes en el proceso ganan sobre el archivo)."""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("AZURE_") and value and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _credential_and_subscription() -> tuple[ClientSecretCredential, str]:
    tenant_id = os.environ.get("AZURE_TENANT_ID")
    client_id = os.environ.get("AZURE_CLIENT_ID")
    client_secret = os.environ.get("AZURE_CLIENT_SECRET")
    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID")
    if not (tenant_id and client_id and client_secret and subscription_id):
        print(
            "[ERROR] Faltan AZURE_TENANT_ID/AZURE_CLIENT_ID/AZURE_CLIENT_SECRET/AZURE_SUBSCRIPTION_ID "
            "en .env (ver .env.example).",
            file=sys.stderr,
        )
        sys.exit(1)
    cred = ClientSecretCredential(tenant_id=tenant_id, client_id=client_id, client_secret=client_secret)
    return cred, subscription_id


def cmd_skus(regions: Iterable[str] | None) -> None:
    """Lista, para TODA la suscripción (o para las regiones dadas si se pasan),
    qué tamaños de VM con GPU (NC/ND/NV) están realmente visibles y en qué
    regiones -incluye las restricciones de subscripción/zona que Azure ya trae
    (p.ej. 'NOT_AVAILABLE_FOR_SUBSCRIPTION' en una región concreta)."""
    cred, subscription_id = _credential_and_subscription()
    compute = ComputeManagementClient(cred, subscription_id)

    region_filter = set(regions) if regions else None
    families: dict[tuple[str, str], dict] = {}
    for sku in compute.resource_skus.list():
        if sku.resource_type != "virtualMachines":
            continue
        name = sku.name or ""
        if not GPU_NAME_RE.match(name):
            continue
        locs = set(sku.locations or [])
        if region_filter and not (locs & {r.lower() for r in region_filter} | locs & region_filter):
            continue
        restricted_locs = {
            loc
            for r in (sku.restrictions or [])
            if r.reason_code and "NOT_AVAILABLE" in str(r.reason_code)
            for loc in (r.restriction_info.locations if r.restriction_info else [])
        }
        key = (sku.family or "", name)
        entry = families.setdefault(key, {"locations": set(), "restricted": set()})
        entry["locations"].update(locs)
        entry["restricted"].update(restricted_locs)

    if not families:
        print("(ninguna SKU con GPU visible para esta suscripción)")
        return

    for (family, name), info in sorted(families.items()):
        clean = sorted(info["locations"] - info["restricted"])
        print(f"family={family:<28} size={name:<28}")
        print(f"    orderable sin restricción en: {clean}")
        if info["restricted"]:
            print(f"    NOT_AVAILABLE_FOR_SUBSCRIPTION en: {sorted(info['restricted'])}")


def cmd_quota(region: str) -> None:
    """Cuota actual (uso/límite) de cada familia GPU en una región."""
    cred, subscription_id = _credential_and_subscription()
    compute = ComputeManagementClient(cred, subscription_id)

    print(f"=== Cuota de vCPU por familia GPU en {region} ===")
    for u in compute.usage.list(region):
        name = (u.name.value or "") if u.name else ""
        if not any(tag in name for tag in ("NC", "ND", "NV")) or "Family" not in name:
            continue
        localized = (u.name.localized_value or "") if u.name else ""
        print(f"  {name:<35} ({localized:<40}) actual={u.current_value:<4} limite={u.limit}")


def cmd_request_increase(region: str, resource_name: str, new_limit: int) -> None:
    """Pide el aumento de cuota vía Microsoft.Quota (mismo backend que 'My quotas'
    en el Portal). Puede fallar con 'ResourceNotAvailableForOffer' si el TIPO DE
    SUSCRIPCIÓN no admite GPU (ej. Free Trial/Azure for Students) -en ese caso
    hace falta pasar la suscripción a Pay-As-You-Go ANTES de reintentar; no hay
    forma de resolverlo por API/CLI, es un cambio de facturación en el Portal."""
    cred, subscription_id = _credential_and_subscription()
    qc = QuotaMgmtClient(cred, subscription_id)
    scope = f"subscriptions/{subscription_id}/providers/Microsoft.Compute/locations/{region}"

    body = CurrentQuotaLimitBase(
        properties=QuotaProperties(
            limit=LimitObject(value=new_limit, limit_type="Independent"),
            name=ResourceName(value=resource_name),
            resource_type="Family",
        )
    )
    print(f"=== Solicitando {resource_name!r} en {region} -> {new_limit} vCPUs ===")
    try:
        poller = qc.quota.begin_create_or_update(resource_name, scope, body)
        result = poller.result()
        print(f"[OK] {result.as_dict() if hasattr(result, 'as_dict') else result}")
    except HttpResponseError as exc:
        code = getattr(exc.error, "code", None)
        print(f"[FALLÓ] status={exc.status_code} code={code}")
        if code == "ResourceNotAvailableForOffer":
            print(
                "  -> El tipo de suscripción actual no admite cuota de GPU (frecuente en "
                "'Free Trial'/'Azure for Students'). Verifica con "
                "`python scripts/azure_check_gpu_quota.py --sub-info` y, si aplica, pasa la "
                "suscripción a Pay-As-You-Go desde el Portal (Cost Management + Billing) "
                "antes de reintentar -no es algo que se resuelva por API."
            )
        sys.exit(2)


def cmd_sub_info() -> None:
    """Tipo de oferta de la suscripción (quota_id/spending_limit): la causa más
    común de 'ResourceNotAvailableForOffer' al pedir cuota de GPU."""
    try:
        from azure.mgmt.subscription import SubscriptionClient
    except ImportError as exc:
        raise ImportError("Falta azure-mgmt-subscription. Instala con: pip install azure-mgmt-subscription") from exc

    cred, subscription_id = _credential_and_subscription()
    sub = SubscriptionClient(cred).subscriptions.get(subscription_id)
    print(f"display_name: {sub.display_name}")
    print(f"state: {sub.state}")
    policies = sub.subscription_policies.as_dict() if sub.subscription_policies else {}
    print(f"quota_id: {policies.get('quota_id')}")
    print(f"spending_limit: {policies.get('spending_limit')}")
    if policies.get("quota_id", "").startswith("FreeTrial") or policies.get("spending_limit") == "On":
        print(
            "[AVISO] Suscripción Free Trial (o con límite de gasto activo): Azure bloquea "
            "TODA cuota de GPU en este tipo de oferta, sin importar región/familia. "
            "Pasar a Pay-As-You-Go en el Portal antes de pedir cuota de GPU."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=str(REPO_ROOT / ".env"), help="Ruta a .env (default: .env en la raíz)")
    parser.add_argument("--skus", action="store_true", help="Lista SKUs de VM con GPU visibles para la suscripción")
    parser.add_argument("--region", action="append", dest="regions", help="Filtra --skus a estas regiones (repetible)")
    parser.add_argument("--quota", metavar="REGION", help="Cuota actual de vCPU por familia GPU en una región")
    parser.add_argument(
        "--request-increase", nargs=3, metavar=("REGION", "FAMILY", "LIMIT"),
        help="Pide aumento de cuota, ej. --request-increase southcentralus StandardNVADSA10v5Family 16",
    )
    parser.add_argument("--sub-info", action="store_true", help="Tipo de oferta de la suscripción (Free Trial, etc.)")
    args = parser.parse_args()

    _load_env(Path(args.env))

    if args.skus:
        cmd_skus(args.regions)
    elif args.quota:
        cmd_quota(args.quota)
    elif args.request_increase:
        region, family, limit = args.request_increase
        cmd_request_increase(region, family, int(limit))
    elif args.sub_info:
        cmd_sub_info()
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
