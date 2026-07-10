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


class OrganMember(BaseModel):
    """Член органа компании из dział 2 KRS.

    ВАЖНО: имена приходят от реестра ЗАМАСКИРОВАННЫМИ звёздочками
    ("S***** B******"); мы их так и храним. Полные имена там не
    отдаются (см. BACKLOG), а имена реальных владельцев берём из CRBR.
    """

    name: str  # замаскированное имя как отдаёт реестр
    role: str | None = None  # функция в органе или тип прокуры


class Management(BaseModel):
    """Структура управления из dział 2 KRS (без полных имён)."""

    representation_body: str | None = None  # nazwaOrganu (напр. ZARZĄD)
    representation_mode: str | None = None  # sposobReprezentacji
    board: list[OrganMember] = []  # reprezentacja.sklad (zarząd)
    supervisory_board: list[OrganMember] = []  # organNadzoru (rada nadzorcza)
    proxies: list[OrganMember] = []  # prokurenci


class Reorganization(BaseModel):
    """Слияние/разделение/преобразование из dział 6 KRS.

    В отличие от distress_flags это не бедствие, а смена периметра
    компании (поглощение, разделение, смена формы): для контрагента
    существенно - меняются активы и обязательства. Имена сторон сделки
    реестр отдаёт полностью (в отличие от замаскированного dział 2).
    """

    circumstance: str | None = None  # okreslenieOkolicznosci: тип события
    description: str | None = None  # opis...: юридические детали и даты
    parties: list[str] = []  # затронутые компании (название + KRS)


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
    reorganizations: list[Reorganization] = []  # dzial6: слияния/разделения
    management: Management | None = None  # dzial2: правление/надзор/прокура


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
