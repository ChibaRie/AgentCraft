from fastapi import HTTPException, status


def require_expert_role(_user_id: int) -> int:
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED, "Permission scaffolding only; business code pending"
    )
