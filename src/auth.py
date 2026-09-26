from __future__ import annotations
from fastapi import Header, HTTPException, WebSocket, status
from .config import settings

ROLE_RANK = {"readonly": 0, "dispatcher": 1, "admin": 2}

def _role_for(key: str | None) -> str | None:
    if not key:
        return None
    return settings.api_keys.get(key)

def require_role(min_role: str):
    def dep(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
        if not settings.auth_enabled:
            return "admin"
        role = _role_for(x_api_key)
        if role is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid API key")
        if ROLE_RANK.get(role, -1) < ROLE_RANK[min_role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Role '{role}' cannot perform this action (needs '{min_role}'+)")
        return role
    return dep

async def authorize_ws(websocket: WebSocket) -> bool:
    if not settings.auth_enabled:
        return True
    key = websocket.query_params.get("key")
    return _role_for(key) is not None
