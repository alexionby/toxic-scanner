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


class FiledStatement(BaseModel):
    filed_at: str = ""
    period: str = ""


class CompanyFacts(BaseModel):
    """Жёсткие факты из одписа KRS - первоисточник, не интерпретация."""

    registration_date: str | None = None
    legal_form: str | None = None
    share_capital: str | None = None
    main_activity: str | None = None  # dzial3: ведущий PKD с описанием
    annual_statements: list[FiledStatement] = []
    last_statement_period: str | None = None
    arrears_flags: list[str] = []  # dzial4: задолженности/взыскания
    distress_flags: list[str] = []  # dzial6: ликвидация/банкротство


class CompanyCandidate(BaseModel):
    name: str
    source: str  # "krs" | "vat_whitelist"
    url: str | None = None
    krs: str | None = None
    nip: str | None = None
    regon: str | None = None
    address: str | None = None
    status: str | None = None  # "active" | "removed" | "unknown"
    confidence: float = Field(ge=0.0, le=1.0)
    facts: CompanyFacts | None = None


class CompanySearchResponse(BaseModel):
    candidates: list[CompanyCandidate]
