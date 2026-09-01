from fastapi import HTTPException, status


def get_current_user_id() -> int:
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED, "Auth scaffolding only; business code pending"
    )
