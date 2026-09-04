from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from config import tenant_id
from database import fetchone, get_db
from dependencies import get_current_user
from schemas import LoginRequest
from security import issue_token, verify_password


router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user["display_name"],
        "role": user["role"],
    }


@router.post("/login")
def login(payload: LoginRequest, db: Any = Depends(get_db)):
    user = fetchone(
        db,
        "SELECT id, tenant_id, username, display_name, role, password_hash, active "
        "FROM users WHERE tenant_id = ? AND username = ?",
        (tenant_id(), payload.username),
        "SELECT id, tenant_id, username, display_name, role, password_hash, active "
        "FROM users WHERE tenant_id = %s AND username = %s",
    )
    if (
        user is None
        or not bool(user["active"])
        or not verify_password(payload.password, user["password_hash"])
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    return {"token": issue_token(user["id"]), "user": public_user(user)}


@router.get("/me")
def me(user: dict[str, Any] = Depends(get_current_user)):
    return {"user": public_user(user)}
