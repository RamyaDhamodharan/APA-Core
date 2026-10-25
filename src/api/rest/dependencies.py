import os
import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from src.db.session_db import get_db
from src.models.user_models import User

bearer_scheme = HTTPBearer()


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    token = credentials.credentials

    secret = os.getenv("JWT_SECRET_KEY")
    algorithm = os.getenv("JWT_ALGORITHM", "HS256")

    if not secret or secret == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET":
        raise RuntimeError("JWT_SECRET_KEY is not configured correctly")

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
        )

        subject = payload.get("sub")
        if payload.get("type") != "access" or not subject:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid access token",
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            user_id = uuid.UUID(str(subject))
            payload["sub"] = str(user_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authenticated user ID is invalid",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        if payload.get("must_change_password") is True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Change your temporary password before using the workspace",
            )

        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
            )
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Account is inactive"
            )
        if user.must_change_password:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Change your temporary password before using the workspace",
            )

        payload["role"] = user.role

        return payload

    except InvalidTokenError as e:
        print("JWT VALIDATION ERROR:", repr(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


def require_role(role: str):
    def role_dependency(current_user: dict[str, object] = Depends(get_current_user)):
        if current_user.get("role") != role:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return current_user

    return role_dependency
