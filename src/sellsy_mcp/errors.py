"""Public, actionable errors. Never return raw HTTP bodies or credentials."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import ValidationError


class ToolError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        self.code = code
        self.details = details
        super().__init__(message)


class SellsyAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, code: Any = None, context: Any = None):
        self.status_code = status_code
        self.code = code
        self.context = context
        super().__init__(f"Sellsy API {status_code}: {message}")


class SellsyOAuthError(PermissionError):
    def __init__(self, status_code: int, reason: str):
        self.status_code, self.reason = status_code, reason
        super().__init__(f"Sellsy OAuth failed ({status_code}): {reason}")


def error_result(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, SellsyOAuthError):
        client_failure = exc.reason == "invalid_client"
        code = "oauth_client_rejected" if client_failure else "reconnect_required"
        return {
            "status": code,
            "error": {
                "code": code,
                "http_status": exc.status_code,
                "oauth_reason": exc.reason,
                "message": (
                    "Sellsy refuse l'authentification de l'application MCP."
                    if client_failure
                    else "Le consentement Sellsy doit être renouvelé."
                ),
                "next_action": (
                    "Faire vérifier le client OAuth privé et son secret par l'administrateur du MCP."
                    if client_failure
                    else "Reconnecter l'application Sellsy avec votre compte."
                ),
            },
        }
    if isinstance(exc, ToolError):
        return {"status": exc.code, "error": {"code": exc.code, "message": str(exc), **exc.details}}
    if isinstance(exc, SellsyAPIError):
        code = {
            401: "reconnect_required",
            403: "permission_denied",
            404: "not_found",
            429: "rate_limited",
        }.get(exc.status_code, "upstream_error")
        return {
            "status": code,
            "error": {
                "code": code,
                "http_status": exc.status_code,
                "upstream_code": exc.code,
                "message": "Sellsy n'a pas pu traiter cette lecture ou cette opération.",
                "next_action": (
                    "Vérifier les droits Sellsy sur cet objet et les scopes du consentement."
                    if exc.status_code in (401, 403)
                    else "Réessayer la lecture ultérieurement."
                ),
            },
        }
    if isinstance(exc, (TimeoutError, httpx.TimeoutException, asyncio.CancelledError)):
        return {"status": "timeout", "error": {"code": "timeout", "message": "Délai de lecture dépassé."}}
    if isinstance(exc, PermissionError):
        return {
            "status": "permission_denied",
            "error": {
                "code": "permission_denied",
                "message": "Autorisation absente, expirée ou révoquée.",
                "next_action": "Vérifier les droits et reconnecter l'application.",
            },
        }
    if isinstance(exc, ValidationError):
        return {
            "status": "invalid_response",
            "error": {
                "code": "invalid_response",
                "message": "Champs de réponse non conformes.",
                "fields": [{"path": list(e["loc"]), "type": e["type"]} for e in exc.errors()],
            },
        }
    if isinstance(exc, ValueError):
        return {"status": "invalid_request", "error": {"code": "invalid_request", "message": str(exc)}}
    return {
        "status": "upstream_error",
        "error": {
            "code": type(exc).__name__,
            "message": "La réponse n'a pas pu être vérifiée. Consulter le diagnostic.",
        },
    }


async def partial_results(**operations: Any) -> dict[str, Any]:
    values = await asyncio.gather(*operations.values(), return_exceptions=True)
    result = {}
    for key, value in zip(operations, values, strict=True):
        if isinstance(value, BaseException):
            if not isinstance(value, Exception):
                raise value
            value = error_result(value)
        result[key] = value
    return result
