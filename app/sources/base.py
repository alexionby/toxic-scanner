"""Базовые контракты для source-адаптеров (см. README.md 'Source Adapters')."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class RawCompanyHit:
    """Необработанная находка до верификации и скоринга.

    Discovery-адаптеры (web_search) производят RawCompanyHit.
    Адаптеры официальных реестров (krs, позже regon) верифицируют
    их и превращают в CompanyCandidate.
    """

    name: str
    source: str
    url: str | None = None
    krs: str | None = None
    nip: str | None = None
    regon: str | None = None
    address: str | None = None
    status: str | None = None
    raw: dict = field(default_factory=dict)


class OfficialRegistryAdapter(Protocol):
    """Контракт для адаптеров официальных реестров (KRS, GUS REGON).

    В отличие от discovery-адаптеров, эти детерминированно возвращают
    профиль по точному идентификатору, а не гадают по названию.
    """

    def get_company_profile(self, identifier: str) -> RawCompanyHit | None: ...
