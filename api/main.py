"""
api/main.py
-----------
FastAPI backend for the SEO & AEO Audit Agent.

Runs ALONGSIDE the existing Streamlit app — it does NOT replace it.
Streamlit is untouched. GitHub Actions are untouched.

Endpoints:
    POST /auth/signup         — create user (persistent, SQLite)
    POST /auth/login          — OAuth2 password flow → JWT
    GET  /auth/me             — current user (JWT required)
    POST /audits              — run an audit (JWT required)
    GET  /audits/{id}         — get audit result (owner only)
    POST /chat                — ask question about an audit (owner only)
    GET  /health              — public liveness

Run:
    uvicorn api.main:app --reload --port 8080

Docs:
    http://localhost:8080/docs
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Dict

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from api import schemas
from sqlalchemy.orm import Session

from api import schemas
from api import security
from api.database import User, get_db
# ─────────────────────────────────────────────────────────────
# Imports matched to your actual project files
# ─────────────────────────────────────────────────────────────
from chat_agent import ChatAgent
from crawler import discover_sitemap_urls, crawl_pages
from seo import audit_site
from aeo import audit_site_aeo
from recommendation_engine import RecommendationEngine

app = FastAPI(
    title="SEO & AEO Audit API",
    version="1.0",
    description="REST API for triggering audits and chatting with findings.",
)


# --- In-memory state ---
# For a demo, audits and chat agents live in process memory. Restart loses
# them. For production you'd persist audits to the same SQLite DB and rebuild
# chat agents lazily. Keeping it in-memory here because the focus is on the
# auth/REST demo.
_audits: Dict[str, Dict[str, Any]] = {}
_chat_agents: Dict[str, ChatAgent] = {}


# ============================================================
# Auth routes
# ============================================================
@app.post("/auth/signup", response_model=schemas.UserOut, status_code=201)
def signup(payload: schemas.UserSignup, db: Session = Depends(get_db)):
    exists = (
        db.query(User)
        .filter((User.username == payload.username) | (User.email == payload.email))
        .first()
    )
    if exists:
        raise HTTPException(
            status_code=400,
            detail="Username or email already registered. Log in instead.",
        )
    user = User(
        username=payload.username,
        email=payload.email,
        hashed_password=security.hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.post("/auth/login", response_model=schemas.TokenOut)
def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == form.username).first()
    if not user or not security.verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = security.create_access_token({"sub": user.username})
    return {"access_token": token, "token_type": "bearer"}


@app.get("/auth/me", response_model=schemas.UserOut)
def me(current_user: User = Depends(security.get_current_user)):
    return current_user


# ============================================================
# Audit routes
# ============================================================
@app.post("/audits", response_model=schemas.AuditResponse)
def trigger_audit(
    req: schemas.AuditRequest,
    current_user: User = Depends(security.get_current_user),
):
    # Run the same pipeline the Streamlit app runs
    urls = discover_sitemap_urls(req.url)
    if not urls:
        urls = [req.url]  # Fallback if no sitemap is found
        
    pages = crawl_pages(urls, max_pages=req.max_pages)
    seo_results = audit_site(pages)
    aeo_results = audit_site_aeo(pages, api_key=os.getenv("GROQ_API_KEY"))
    rec_engine = RecommendationEngine()
    recommendations = rec_engine.generate_recommendations(seo_results, aeo_results)
    audit_id = str(uuid.uuid4())

    _audits[audit_id] = {
        "owner": current_user.username,
        "url": req.url,
        "seo": seo_results,
        "aeo": aeo_results,
        "recommendations": recommendations,
    }

    # Cache a ChatAgent bound to this audit so /chat doesn't re-index every call
    agent = ChatAgent(api_key=os.getenv("GROQ_API_KEY", ""))
    agent.index_audit(seo_results, aeo_results, recommendations)
    _chat_agents[audit_id] = agent

    return schemas.AuditResponse(
        audit_id=audit_id,
        url=req.url,
        seo=seo_results,
        aeo=aeo_results,
        recommendations=recommendations,
    )


@app.get("/audits/{audit_id}", response_model=schemas.AuditResponse)
def get_audit(
    audit_id: str,
    current_user: User = Depends(security.get_current_user),
):
    if audit_id not in _audits:
        raise HTTPException(404, "Audit not found")
    audit = _audits[audit_id]
    if audit["owner"] != current_user.username:
        raise HTTPException(403, "Not your audit")
    return schemas.AuditResponse(
        audit_id=audit_id,
        url=audit["url"],
        seo=audit["seo"],
        aeo=audit["aeo"],
        recommendations=audit["recommendations"],
    )


# ============================================================
# Chat route
# ============================================================
@app.post("/chat", response_model=schemas.ChatResponse)
def chat(
    req: schemas.ChatRequest,
    current_user: User = Depends(security.get_current_user),
):
    if req.audit_id not in _audits:
        raise HTTPException(404, "Audit not found")
    if _audits[req.audit_id]["owner"] != current_user.username:
        raise HTTPException(403, "Not your audit")

    agent = _chat_agents.get(req.audit_id)
    if agent is None:
        raise HTTPException(500, "Chat agent unavailable for this audit")

    answer = agent.ask(req.question)
    return schemas.ChatResponse(answer=answer)


# ============================================================
# Health
# ============================================================
@app.get("/health")
def health():
    return {"status": "ok"}