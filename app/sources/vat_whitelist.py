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

WL_API_BASE = "https://wl-api.mf.gov.pl/api/search/nip"
REQUEST_TIMEOUT_SECONDS = 10

_NIP_WEIGHTS = (6, 5, 7, 2, 3, 4, 5, 6, 7)


def is_valid_nip(nip: str) -> bool:
    """Проверяет формат и контрольную сумму NIP без похода в сеть."""
    digits = "".join(ch for ch in nip if ch.isdigit())
    if len(digits) != 10:
        return False
    checksum = sum(int(d) * w for d, w in zip(digits, _NIP_WEIGHTS)) % 11
    return checksum != 10 and checksum == int(digits[9])


def lookup_by_nip(nip: str, as_of: date | None = None) -> RawCompanyHit | None:
    digits = "".join(ch for ch in nip if ch.isdigit())
    if not is_valid_nip(digits):
        return None

    query_date = (as_of or date.today()).isoformat()
    try:
        response = requests.get(
            f"{WL_API_BASE}/{digits}",
            params={"date": query_date},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.warning("VAT whitelist request failed for NIP %s", digits, exc_info=True)
        return None

    if response.status_code != 200:
        logger.info(
            "VAT whitelist: no result for NIP %s (%s)", digits, response.status_code
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
        url=f"https://www.podatki.gov.pl/wykaz-podatnikow-vat-wyszukiwarka?nip={digits}",
        krs=subject.get("krs") or None,
        nip=digits,
        regon=subject.get("regon"),
        address=subject.get("workingAddress") or subject.get("residenceAddress"),
        status=status,
        raw=subject,
    )
