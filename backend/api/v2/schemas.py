"""V2 API schema。Task 4 先放空基类；各端点模型由后续任务在此补充。"""

from pydantic import BaseModel


class V2BaseModel(BaseModel):
    """V2 schema 统一基类（预留 model_config / 统一字段挂载点）。"""
