"""
==============================================================================
Cliente HTTP del LiteLLM Proxy (gestión de API Keys y salud del pool)
==============================================================================
Las API Keys son propiedad de LiteLLM (él las emite y las valida en cada
petición). Este cliente delega la emisión al proxy y el panel guarda únicamente
el hash + metadatos en `sooniverse.api_key_registry`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class LiteLLMError(Exception):
    """Fallo comunicándose con el LiteLLM Proxy."""


class LiteLLMClient:
    def __init__(self, base_url: Optional[str] = None, master_key: Optional[str] = None):
        self.base_url = (base_url or settings.LITELLM_BASE_URL).rstrip("/")
        self.master_key = master_key or settings.LITELLM_MASTER_KEY
        self.timeout = settings.LITELLM_TIMEOUT

    # -- infraestructura -------------------------------------------------------
    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.master_key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> Any:
        if not self.master_key:
            raise LiteLLMError("LITELLM_MASTER_KEY no está configurada; no se puede operar sobre el proxy.")

        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method, url, headers=self._headers, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as exc:
            raise LiteLLMError(f"LiteLLM inalcanzable en {url}: {exc}") from exc

        if resp.status_code >= 400:
            detalle = resp.text[:400]
            raise LiteLLMError(f"LiteLLM respondió {resp.status_code} en {path}: {detalle}")

        try:
            return resp.json()
        except ValueError:
            return {}

    # -- API Keys --------------------------------------------------------------
    def generate_key(
        self,
        alias: str,
        models: Optional[List[str]] = None,
        max_budget: Optional[float] = None,
        rpm_limit: Optional[int] = None,
        tpm_limit: Optional[int] = None,
        duration: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Emite una nueva API Key. Devuelve `{'key': 'sk-...', 'token': '<hash>', ...}`.
        La key en claro solo se muestra una vez: nunca se persiste en nuestra BD.
        """
        payload: Dict[str, Any] = {
            "key_alias": alias,
            "models": models or [],
            "metadata": {
                "gestionado_por": "sooniverse",
                "cliente_id": settings.CLIENTE_ID,
                "entorno": settings.ENTORNO,
                **(metadata or {}),
            },
        }
        if max_budget is not None:
            payload["max_budget"] = float(max_budget)
        if rpm_limit:
            payload["rpm_limit"] = int(rpm_limit)
        if tpm_limit:
            payload["tpm_limit"] = int(tpm_limit)
        if duration:
            payload["duration"] = duration

        return self._request("POST", "/key/generate", json=payload)

    def update_key(self, key_or_token: str, **fields) -> Dict[str, Any]:
        return self._request("POST", "/key/update", json={"key": key_or_token, **fields})

    def block_key(self, key_or_token: str) -> Dict[str, Any]:
        """Desactiva la key sin borrarla (conserva el histórico de consumo)."""
        return self._request("POST", "/key/block", json={"key": key_or_token})

    def unblock_key(self, key_or_token: str) -> Dict[str, Any]:
        return self._request("POST", "/key/unblock", json={"key": key_or_token})

    def delete_keys(self, keys: List[str]) -> Dict[str, Any]:
        return self._request("POST", "/key/delete", json={"keys": keys})

    def key_info(self, key_or_token: str) -> Dict[str, Any]:
        return self._request("GET", "/key/info", params={"key": key_or_token})

    # -- observabilidad --------------------------------------------------------
    def models(self) -> List[str]:
        data = self._request("GET", "/v1/models")
        return [m.get("id") for m in data.get("data", []) if m.get("id")]

    def health(self) -> Dict[str, Any]:
        """Estado de cada deployment (worker vLLM) del pool balanceado."""
        return self._request("GET", "/health")

    def is_reachable(self) -> bool:
        try:
            requests.get(f"{self.base_url}/health/liveliness", timeout=5).raise_for_status()
            return True
        except requests.RequestException:
            return False
