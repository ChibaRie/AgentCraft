"""User system request/response schemas (Engineering Spec §6.2, PRD §4.1.3)."""

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    username: str = Field(min_length=2, max_length=30)
    email: EmailStr = Field(max_length=100)
    # 上限 72 字节：bcrypt 只处理前 72 字节，超长部分静默失效
    password: str = Field(min_length=6, max_length=72)

    # PRD §4.1.3：用户名/邮箱去首尾空格后再计长与校验；密码不做剥离
    @field_validator("username", "email", mode="before")
    @classmethod
    def strip_surrounding_whitespace(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("password")
    @classmethod
    def reject_null_bytes(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("密码包含不支持的字符")
        return value


class LoginRequest(BaseModel):
    login: str = Field(min_length=1)
    password: str = Field(min_length=1, max_length=72)

    @field_validator("login", mode="before")
    @classmethod
    def strip_login(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class AuthResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str
    token: str


class UserMeResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str
    created_at: datetime


class ExpertApplyResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str
