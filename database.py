"""
PostgreSQL database layer using SQLAlchemy.

Tables:
  - projects: stores project metadata (id, name, created_at)
  - test_cases: stores test cases linked to projects via foreign key

Connection string is read from the DATABASE_URL environment variable.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Generator

from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    relationship,
    sessionmaker,
)

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:admin@localhost:5432/AI_tester_db",
)

engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# ---------------------------------------------------------------------------
# ORM models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id = Column(String(8), primary_key=True)
    name = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    test_cases = relationship(
        "TestCaseDB",
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "test_cases": [tc.to_dict() for tc in self.test_cases],
        }


class TestCaseDB(Base):
    __tablename__ = "test_cases"

    id = Column(String(36), primary_key=True)           # uuid
    project_id = Column(
        String(8),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    test_id = Column(String(20), nullable=False)         # e.g. TC-001
    test_name = Column(String(500), nullable=False)
    description = Column(Text, nullable=False, default="")
    expected_result = Column(Text, nullable=False, default="")
    priority = Column(String(20), nullable=False, default="Medium")
    category = Column(String(50), nullable=False, default="Functional")

    project = relationship("Project", back_populates="test_cases")

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "test_name": self.test_name,
            "description": self.description,
            "expected_result": self.expected_result,
            "priority": self.priority,
            "category": self.category,
        }


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

def get_db() -> Generator[Session, None, None]:
    """Yield a SQLAlchemy session, closing it when done."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables if they don't already exist."""
    Base.metadata.create_all(bind=engine)
