"""
api/database.py
---------------
SQLAlchemy + SQLite setup for the FastAPI backend.

Users are persisted to data/api_users.db so accounts survive server restarts
and can be reused across sessions — no re-registration on every run.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.sql import func

# Ensure data/ exists before SQLite tries to open the file
Path("data").mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("API_DATABASE_URL", "sqlite:///./data/api_users.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # required for SQLite + FastAPI threads
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at = Column(DateTime, server_default=func.now())


# Create tables if missing — idempotent, runs on import
Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI dependency — yields a session, closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()