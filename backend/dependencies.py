from typing import Any, Callable

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from database import fetchone, get_db
from security import verify_token


bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    db: Any = Depends(get_db),
) -> dict[str, Any]:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = verify_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The session is invalid or has expired.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = fetchone(
        db,
        "SELECT id, tenant_id, username, display_name, role, active "
        "FROM users WHERE id = ?",
        (payload["sub"],),
        "SELECT id, tenant_id, username, display_name, role, active "
        "FROM users WHERE id = %s",
    )
    if user is None or not bool(user["active"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="The user account is unavailable.",
        )
    return user


def require_roles(*allowed_roles: str) -> Callable[..., dict[str, Any]]:
    def dependency(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if user["role"] not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This action requires a different role.",
            )
        return user

    return dependency


require_admin = require_roles("admin")
require_operator = require_roles("operator", "admin")
