"""Адаптер Białej listy podatników VAT (wl-api.mf.gov.pl).

Бесплатно, без ключа, без регистрации. По NIP отдаёт REGON и KRS -
структура ответа подтверждена реальным вызовом (CD Projekt S.A.,
NIP 7342867148 -> вернул krs=0000006865, regon=492707333).
"""

from __future__ import annotations

import logging
from datetime import date

import requests

from app.sources.base import RawCompanyHit

logger = logging.getLogger(__name__)

WL_API_BASE = "https://wl-api.mf.gov.pl/api/search"
REQUEST_TIMEOUT_SECONDS = 10

_NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)
_REGON_WEIGHTS = (8, 9, 2, 3, 4, 5, 6, 7)


def is_valid_nip(nip: str) -> bool:
    """Проверяет формат и контрольную сумму NIP без похода в сеть."""
    digits = "".join(ch for ch in nip if ch.isdigit())
    if len(digits) != 10:
        return False
    checksum = sum(int(d) * w for d, w in zip(digits, _NIP_WEIGHTS)) % 11
    return checksum != 10 and checksum == int(digits[9])


def is_valid_regon(regon: str) -> bool:
    """Контрольная сумма 9-значного (базового) REGON. Сумма mod 11 == 10
    по спецификации GUS означает контрольную цифру 0 (в отличие от NIP,
    где 10 делает номер невалидным)."""
    digits = "".join(ch for ch in regon if ch.isdigit())
    if len(digits) != 9:
        return False
    checksum = sum(int(d) * w for d, w in zip(digits, _REGON_WEIGHTS)) % 11
    return checksum % 10 == int(digits[8])


def lookup_by_nip(nip: str, as_of: date | None = None) -> RawCompanyHit | None:
    digits = "".join(ch for ch in nip if ch.isdigit())
    if not is_valid_nip(digits):
        return None
    return _fetch_subject("nip", digits, as_of)


def lookup_by_regon(regon: str, as_of: date | None = None) -> RawCompanyHit | None:
    digits = "".join(ch for ch in regon if ch.isdigit())
    # 14-значный REGON (локальная единица) содержит базовый 9-значный номер
    # юр. лица в первых девяти позициях — ищем всегда по нему. Собственную
    # контрольную цифру 14-значной формы не проверяем: реестры (включая KRS
    # API) дописывают нули до 14 знаков без её пересчёта.
    if len(digits) == 14:
        digits = digits[:9]
    if not is_valid_regon(digits):
        return None
    return _fetch_subject("regon", digits, as_of)


def _fetch_subject(
    kind: str, number: str, as_of: date | None
) -> RawCompanyHit | None:
    query_date = (as_of or date.today()).isoformat()
    try:
        response = requests.get(
            f"{WL_API_BASE}/{kind}/{number}",
            params={"date": query_date},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.warning(
            "VAT whitelist request failed for %s %s", kind, number, exc_info=True
        )
        return None

    if response.status_code != 200:
        logger.info(
            "VAT whitelist: no result for %s %s (%s)",
            kind,
            number,
            response.status_code,
        )
        return None

    subject = response.json().get("result", {}).get("subject")
    if not subject:
        return None

    status_vat = (subject.get("statusVat") or "").lower()
    status = (
        "removed"
        if subject.get("removalDate") or "wykreśl" in status_vat
        else "active"
    )

    return RawCompanyHit(
        name=subject.get("name", ""),
        source="vat_whitelist",
        url=f"https://www.podatki.gov.pl/wykaz-podatnikow-vat-wyszukiwarka?{kind}={number}",
        krs=subject.get("krs") or None,
        nip=subject.get("nip") or (number if kind == "nip" else None),
        regon=subject.get("regon") or (number if kind == "regon" else None),
        address=subject.get("workingAddress") or subject.get("residenceAddress"),
        status=status,
        raw=subject,
    )
