"""
api/schemas.py
--------------
Pydantic v2 request/response models for the FastAPI backend.
"""

from __future__ import annotations

from typing import Any, Dict

from pydantic import BaseModel, EmailStr


# --- Auth ---
class UserSignup(BaseModel):
    username: str
    email: EmailStr
    password: str


class UserOut(BaseModel):
    username: str
    email: EmailStr

    class Config:
        from_attributes = True


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


# --- Audit ---
class AuditRequest(BaseModel):
    url: str
    max_pages: int = 10


class AuditResponse(BaseModel):
    audit_id: str
    url: str
    seo: Dict[str, Any]
    aeo: Dict[str, Any]
    recommendations: Dict[str, Any]


# --- Chat ---
class ChatRequest(BaseModel):
    audit_id: str
    question: str


class ChatResponse(BaseModel):
    answer: str