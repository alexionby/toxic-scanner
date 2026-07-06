"""Общие Pydantic-модели для Company Resolver (Phase 1, см. README.md)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CompanyQuery(BaseModel):
    company_name: str | None = None
    country: str = "PL"
    city: str | None = None
    nip: str | None = None
    krs: str | None = None
    regon: str | None = None


class CompanyCandidate(BaseModel):
    name: str
    source: str  # "krs" | "web_search"
    url: str | None = None
    krs: str | None = None
    nip: str | None = None
    regon: str | None = None
    address: str | None = None
    status: str | None = None  # "active" | "removed" | "unknown"
    confidence: float = Field(ge=0.0, le=1.0)


class CompanySearchResponse(BaseModel):
    candidates: list[CompanyCandidate]
