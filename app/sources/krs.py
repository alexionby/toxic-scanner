"""Адаптер официального KRS Open API (api-krs.ms.gov.pl).

ВАЖНО: этот API отдаёт данные ТОЛЬКО по точному номеру KRS — поиска
по названию в нём нет (проверено). Discovery (название -> номер KRS)
делает web_search.py, этот модуль только верифицирует/обогащает.
"""

from __future__ import annotations

import logging

import requests

from app.sources.base import RawCompanyHit

logger = logging.getLogger(__name__)

KRS_API_BASE = "https://api-krs.ms.gov.pl/api/krs"
REQUEST_TIMEOUT_SECONDS = 10


def _normalize_krs(krs: str) -> str:
    """Номер KRS в реестре — 10 цифр с ведущими нулями."""
    digits = "".join(ch for ch in krs if ch.isdigit())
    return digits.zfill(10)


def get_company_profile(krs: str, rejestr: str = "P") -> RawCompanyHit | None:
    """Тянет актуальный одпис по номеру KRS.

    rejestr="P" - rejestr przedsiębiorców (нужен для financial health
    check), "S" - stowarzyszenia, не используем.
    """
    krs_number = _normalize_krs(krs)
    url = f"{KRS_API_BASE}/OdpisAktualny/{krs_number}"

    try:
        response = requests.get(
            url,
            params={"rejestr": rejestr, "format": "json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.warning("KRS API request failed for %s", krs_number, exc_info=True)
        return None

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        logger.warning("KRS API returned %s for %s", response.status_code, krs_number)
        return None

    return _parse_odpis(krs_number, response.json())


def _parse_odpis(krs_number: str, payload: dict) -> RawCompanyHit:
    """Достаёт нужные поля из одписа KRS.

    Пути полей сверены с реальным ответом API (KRS 0000006865,
    CD Projekt S.A.). Для wykreślonych подмиотов OdpisAktualny
    возвращает 404, поэтому успешный ответ означает активную запись.
    """
    dane = payload.get("odpis", {}).get("dane", {})
    dzial1 = dane.get("dzial1", {})
    dane_podmiotu = dzial1.get("danePodmiotu", {})

    name = dane_podmiotu.get("nazwa") or ""
    status = "active"

    identyfikatory = dane_podmiotu.get("identyfikatory", {})
    nip = identyfikatory.get("nip")
    regon = identyfikatory.get("regon")

    adres = dzial1.get("siedzibaIAdres", {}).get("adres", {})
    address = ", ".join(
        part
        for part in (
            adres.get("ulica"),
            adres.get("nrDomu"),
            adres.get("miejscowosc"),
            adres.get("kodPocztowy"),
        )
        if part
    ) or None

    return RawCompanyHit(
        name=name,
        source="krs",
        url=f"https://wyszukiwarka-krs.ms.gov.pl/dane-szczegolowe-podmiotu;numerKRS={krs_number}",
        krs=krs_number,
        nip=nip,
        regon=regon,
        address=address,
        status=status,
        facts=_extract_facts(payload),
        raw=payload,
    )


def _extract_facts(payload: dict) -> dict:
    """Жёсткие факты из одписа: возраст, капитал, отчётность, флаги.

    Структура сверена с живыми ответами API (CD Projekt 0000006865,
    MPSYSTEM 0000475078, 2026-07-06).
    """
    odpis = payload.get("odpis", {})
    naglowek = odpis.get("naglowekA", {})
    dane = odpis.get("dane", {})
    dzial1 = dane.get("dzial1", {})

    kapital = dzial1.get("kapital", {}).get("wysokoscKapitaluZakladowego", {})
    share_capital = None
    if kapital.get("wartosc"):
        share_capital = f"{kapital['wartosc']} {kapital.get('waluta', '')}".strip()

    wzmianki = (
        dane.get("dzial3", {})
        .get("wzmiankiOZlozonychDokumentach", {})
        .get("wzmiankaOZlozeniuRocznegoSprawozdaniaFinansowego", [])
    )
    statements = [
        {"filed_at": w.get("dataZlozenia", ""), "period": w.get("zaOkresOdDo", "")}
        for w in wzmianki
        if isinstance(w, dict)
    ]

    # dzial6 содержит и безобидные записи (слияния/преобразования),
    # поэтому флагуем только ключи про ликвидацию/банкротство/роспуск.
    distress_patterns = ("likwid", "upadl", "rozwiaz", "wykresl", "zawiesz", "restruktur")
    distress_flags = [
        key
        for key, value in dane.get("dzial6", {}).items()
        if value and any(p in key.lower() for p in distress_patterns)
    ]

    arrears_flags = [key for key, value in dane.get("dzial4", {}).items() if value]

    return {
        "registration_date": naglowek.get("dataRejestracjiWKRS"),
        "legal_form": dzial1.get("danePodmiotu", {}).get("formaPrawna"),
        "share_capital": share_capital,
        "annual_statements": statements,
        "last_statement_period": statements[-1]["period"] if statements else None,
        "arrears_flags": arrears_flags,
        "distress_flags": distress_flags,
    }
