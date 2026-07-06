"""
Jarvis OS — Pydantic Schemas
Request/response shapes for all API endpoints.
Kept separate from ORM models to enforce the API/DB boundary.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from enum import Enum

from pydantic import BaseModel, Field


# ============================================================
# Shared
# ============================================================
class HealthStatus(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    ERROR = "error"


# ============================================================
# Chat
# ============================================================
class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|assistant|system)$")
    content: str = Field(..., min_length=1)


class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., min_length=1)
    session_id: Optional[str] = None
    stream: bool = True
    provider: Optional[str] = None  # Override default provider for this request
    model: Optional[str] = None     # Override default model for this request


class ChatResponse(BaseModel):
    content: str
    model: str
    provider: str
    session_id: str
    tokens_used: Optional[int] = None


class StreamChunk(BaseModel):
    """Single chunk in a streaming response."""
    delta: str
    done: bool = False
    session_id: Optional[str] = None
    model: Optional[str] = None
    provider: Optional[str] = None


# ============================================================
# Memory
# ============================================================
class SemanticMemoryCreate(BaseModel):
    content: str = Field(..., min_length=1)
    category: Optional[str] = None
    source: str = "explicit"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class SemanticMemoryResponse(BaseModel):
    id: str
    content: str
    category: Optional[str]
    source: str
    confidence: float
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class MemorySearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    category: Optional[str] = None


class MemorySearchResult(BaseModel):
    memories: List[SemanticMemoryResponse]
    total: int


# ============================================================
# Contacts
# ============================================================
class ContactCreate(BaseModel):
    name: str = Field(..., min_length=1)
    email: Optional[str] = None
    phone: Optional[str] = None
    organization: Optional[str] = None
    relationship_type: Optional[str] = None
    notes: Optional[str] = None


class ContactResponse(BaseModel):
    id: str
    name: str
    email: Optional[str]
    phone: Optional[str]
    organization: Optional[str]
    relationship_type: Optional[str]
    notes: Optional[str]
    interaction_count: int
    last_interaction: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


# ============================================================
# Health
# ============================================================
class ComponentHealth(BaseModel):
    status: HealthStatus
    detail: Optional[str] = None


class HealthResponse(BaseModel):
    status: HealthStatus
    version: str
    provider: str
    model: str
    components: Dict[str, ComponentHealth]
    timestamp: datetime = Field(default_factory=datetime.utcnow)
