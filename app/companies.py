"""Company Resolver v0 - только гарантированный путь по NIP/KRS.

Discovery по названию сознательно не реализуем: в польских реестрах
нет бесплатного государственного full-text поиска по имени.
"""

from __future__ import annotations

from app.models import CompanyCandidate, CompanyQuery
from app.sources import krs, vat_whitelist
from app.sources.base import RawCompanyHit


def resolve_company(query: CompanyQuery) -> list[CompanyCandidate]:
    if query.krs:
        hit = krs.get_company_profile(query.krs)
        return [_to_candidate(hit)] if hit else []

    if query.nip:
        whitelist_hit = vat_whitelist.lookup_by_nip(query.nip)
        if whitelist_hit is None:
            return []

        if whitelist_hit.krs:
            full_profile = krs.get_company_profile(whitelist_hit.krs)
            if full_profile is not None:
                return [_to_candidate(full_profile)]

        return [_to_candidate(whitelist_hit)]

    return []  # только название - не поддерживается в v0


def _to_candidate(hit: RawCompanyHit) -> CompanyCandidate:
    return CompanyCandidate(
        name=hit.name,
        source=hit.source,
        url=hit.url,
        krs=hit.krs,
        nip=hit.nip,
        regon=hit.regon,
        address=hit.address,
        status=hit.status,
        confidence=1.0,
    )
