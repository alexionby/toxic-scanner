"""Company Resolver - точный путь по NIP/KRS плюс discovery по названию.

Точный идентификатор даёт одного кандидата с confidence=1.0.
Поиск по названию - полуавтомат: Tavily находит упоминания с KRS/NIP
(web_search.py), каждая находка верифицируется в официальных реестрах,
а финальный выбор всегда за пользователем, поэтому confidence < 1.0.
"""

from __future__ import annotations

import logging
import re
from difflib import SequenceMatcher

from app.models import CompanyCandidate, CompanyQuery
from app.sources import krs, vat_whitelist, web_search
from app.sources.base import RawCompanyHit

logger = logging.getLogger(__name__)

# Токены орг.-правовых форм не несут сигнала при сравнении названий:
# "MPSYSTEM SP. Z O.O." должно совпадать с запросом "mpsystem".
_LEGAL_FORM_TOKENS = {
    "sp", "z", "o", "oo", "zoo", "s", "a", "sa", "spolka", "spółka",
    "ograniczona", "ograniczoną", "odpowiedzialnoscia", "odpowiedzialnością",
    "akcyjna", "komandytowa", "komandytowo", "jawna", "cywilna", "ska", "sk",
}


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

    if query.company_name:
        return _resolve_by_name(query.company_name, query.country)

    return []


def _resolve_by_name(company_name: str, country: str) -> list[CompanyCandidate]:
    """Discovery по названию: web search -> верификация каждой находки."""
    try:
        hits = web_search.search_company_candidates(company_name, country)
    except Exception:
        logger.warning("Web search failed for %r", company_name, exc_info=True)
        return []

    candidates: list[CompanyCandidate] = []
    seen: set[str] = set()
    for hit in hits:
        if _dedup_key(hit) in seen:
            continue

        verified = _verify_hit(hit)
        if verified is None:
            continue  # в официальных реестрах не подтвердилось - отбрасываем

        key = _dedup_key(verified)
        if key in seen:
            continue
        seen.update({key, _dedup_key(hit)})

        confidence = _name_confidence(company_name, verified.name)
        candidates.append(_to_candidate(verified, confidence=confidence))

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


def _verify_hit(hit: RawCompanyHit) -> RawCompanyHit | None:
    """Подтверждает web-находку официальным реестром, иначе None."""
    if hit.krs:
        profile = krs.get_company_profile(hit.krs)
        if profile is not None:
            return profile

    if hit.nip:
        whitelist_hit = vat_whitelist.lookup_by_nip(hit.nip)
        if whitelist_hit is None:
            return None
        if whitelist_hit.krs:
            profile = krs.get_company_profile(whitelist_hit.krs)
            if profile is not None:
                return profile
        return whitelist_hit

    return None


def _dedup_key(hit: RawCompanyHit) -> str:
    return hit.krs or f"nip:{hit.nip}"


def _normalize_name(name: str) -> str:
    tokens = re.sub(r"[^\w\s]", " ", name.lower()).split()
    core = [t for t in tokens if t not in _LEGAL_FORM_TOKENS]
    return " ".join(core or tokens)


def _name_confidence(query_name: str, registry_name: str) -> float:
    """Похожесть названий как прокси уверенности.

    Всегда < 1.0: кандидата из discovery подтверждает пользователь,
    даже при дословном совпадении названий.
    """
    ratio = SequenceMatcher(
        None, _normalize_name(query_name), _normalize_name(registry_name)
    ).ratio()
    return round(min(0.95, max(0.30, ratio)), 2)


def _to_candidate(hit: RawCompanyHit, confidence: float = 1.0) -> CompanyCandidate:
    return CompanyCandidate(
        name=hit.name,
        source=hit.source,
        url=hit.url,
        krs=hit.krs,
        nip=hit.nip,
        regon=hit.regon,
        address=hit.address,
        status=hit.status,
        confidence=confidence,
        facts=hit.facts,
    )
