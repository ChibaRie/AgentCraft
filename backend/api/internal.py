from fastapi import APIRouter, HTTPException, status

router = APIRouter(tags=["internal"])


@router.post("/mcp/call")
async def call_mcp(payload: dict[str, object]) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/ui/response")
async def respond_ui(payload: dict[str, object]) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")


@router.post("/harness/check-code-style")
async def check_code_style(payload: dict[str, object]) -> dict[str, object]:
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "业务代码待填充")
