"""Адаптер CRBR - Centralny Rejestr Beneficjentów Rzeczywistych.

Отдаёт реальных владельцев (бенефициаров) компании по NIP: имя,
гражданство, характер и размер доли. Это ПЕРВОИСТОЧНИК (публичный
госреестр, jawny по закону), в отличие от финансовых агрегаторов.

Ключевое отличие от dział 2 KRS: правление там - наёмная фигура и с
замаскированными именами, а CRBR отдаёт ПОЛНЫЕ имена тех, кто реально
владеет/контролирует. Проверено 2026-07-09: владельцы ≠ директор
(MPSYSTEM, GOWORK MED).

Эндпоинт работает обычным POST (без браузера/Imperva/auth), одиночные
и мелкие батчи проходят с reCaptchaToken="0" без throttling.

PII: CRBR публично отдаёт и PESEL/дату рождения бенефициаров, но мы их
СОЗНАТЕЛЬНО НЕ храним и не показываем (минимизация PII, аудитория ЕС).
Для сигнала «кто владеет» хватает имени, гражданства и доли.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

import requests

from app.sources.vat_whitelist import is_valid_nip

logger = logging.getLogger(__name__)

CRBR_API = "https://crbr.podatki.gov.pl/adcrbr/api/wyszukajSpolke"
SOURCE_URL = "https://crbr.podatki.gov.pl/adcrbr/"
REQUEST_TIMEOUT_SECONDS = 15

# Кап на портфель одного владельца: серийный держатель (десятки фирм) -
# сам по себе сигнал, но тянуть весь список незачем. Усечение помечаем.
MAX_LINKED_COMPANIES = 20

# Дата запуска CRBR: форвардный запрос делаем диапазоном от неё до сегодня,
# чтобы одним вызовом получить и текущий состав, и историю владения.
CRBR_START_DATE = "2019-10-13"


@dataclass
class LinkedCompany:
    """Другая фирма того же владельца (обратный поиск CRBR по PESEL).

    Всё берётся из ответа reverse-поиска, без похода в KRS (1-й уровень
    без фан-аута). PESEL, по которому нашли, здесь НЕ хранится.
    """

    krs: str | None
    name: str
    legal_form: str | None = None
    address: str | None = None
    role: str | None = None  # роль этого владельца в той фирме
    in_proceedings: bool = False  # spolka.postepowanie не пусто


@dataclass
class Beneficiary:
    name: str
    citizenship: list[str] = field(default_factory=list)
    ownership: list[str] = field(default_factory=list)  # человекочитаемые доли
    # Сеть 1-го уровня (заполняется при with_network=True). PESEL как
    # ключ поиска живёт только в области функции и сюда НЕ попадает.
    linked_companies: list[LinkedCompany] = field(default_factory=list)
    network_status: str = "not_checked"  # not_checked|no_pesel|ok|error
    linked_truncated: bool = False


@dataclass
class BeneficiariesResult:
    nip: str
    ok: bool = False  # запрос к CRBR удался (иначе - не трактовать как флаг)
    found: bool = False  # в CRBR есть запись о бенефициарах
    beneficiaries: list[Beneficiary] = field(default_factory=list)
    subject_address: str | None = None  # адрес самой фирмы для сверки сети
    owners_since: str | None = None  # с какой даты действует текущий состав
    ownership_changed: bool = False  # менялся ли состав за историю CRBR
    discrepancy: bool = False  # подано расхождение (listaInformacjiORozbieznosciach)
    source_url: str = SOURCE_URL
    note: str | None = None


def _normalize_nip(nip: str) -> str:
    return "".join(ch for ch in nip if ch.isdigit())


def _base_payload(date_from: str | None = None) -> dict:
    today = date.today().isoformat()
    # reCaptchaToken="0" принимается на одиночных запросах (проверено); при
    # массовом прогоне здесь понадобится реальный токен (см. BACKLOG).
    return {
        "dataOd": date_from or today,
        "dataDo": today,
        "reCaptchaToken": "0",
        "czasPobraniaDanych": int(time.time() * 1000),
    }


def _call(payload: dict, ctx: str) -> tuple[dict | None, str | None]:
    """POST к CRBR; возвращает (json, note_об_ошибке)."""
    try:
        response = requests.post(
            CRBR_API,
            json=payload,
            headers={"content-type": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.warning("CRBR request failed (%s)", ctx, exc_info=True)
        return None, "CRBR недоступен"

    if response.status_code != 200:
        logger.warning("CRBR returned %s (%s)", response.status_code, ctx)
        return None, f"CRBR ответил {response.status_code}"

    return response.json(), None


def _format_address(adres: dict | None) -> str | None:
    if not adres:
        return None
    street = " ".join(
        p for p in (adres.get("ulica"), adres.get("nrDomu")) if p
    )
    tail = " ".join(
        p for p in (adres.get("kodPocztowy"), adres.get("miejscowosc")) if p
    )
    return ", ".join(p for p in (street, tail) if p) or None


def _ownership_descriptions(beneficiary: dict) -> list[str]:
    out: list[str] = []
    for u in beneficiary.get("informacjeOUdzialeLubUprawnieniach") or []:
        desc = u.get("rodzajWlasnosciOpis") or ""
        amount = u.get("ilosc")
        unit = u.get("jednostkaMiaryUdzialuOpis") or ""
        if amount:
            desc = f"{desc} ({amount} {unit})".strip()
        if desc:
            out.append(desc)
    return out


def get_beneficiaries(nip: str, with_network: bool = False) -> BeneficiariesResult:
    """Тянет актуальных бенефициаров компании по NIP.

    with_network=True дополнительно делает обратный поиск по PESEL каждого
    владельца - «сеть 1-го уровня»: другие фирмы того же человека прямо из
    ответа CRBR, без похода в KRS. PESEL нигде не сохраняется.
    """
    nip_digits = _normalize_nip(nip)
    # is_valid_nip проверяет и длину, и контрольную сумму - отсекаем битые
    # NIP до сетевого POST (в отличие от прежней проверки только длины).
    if not is_valid_nip(nip_digits):
        return BeneficiariesResult(nip=nip_digits, ok=False, note="некорректный NIP")

    # Диапазон от запуска CRBR: одним вызовом получаем и текущий состав, и
    # историю владения (смена собственника - отдельный сигнал).
    payload = {
        "kontekstWyszukania": 1,
        "nip": nip_digits,
        **_base_payload(date_from=CRBR_START_DATE),
    }
    data, err = _call(payload, f"NIP {nip_digits}")
    if data is None:
        return BeneficiariesResult(nip=nip_digits, ok=False, note=err)

    return _parse_response(nip_digits, data, with_network)


def _display_name(beneficiary: dict) -> str:
    return " ".join(
        p
        for p in (
            beneficiary.get("imiePierwsze"),
            beneficiary.get("imieDrugieINastepne"),
            beneficiary.get("nazwisko"),
        )
        if p
    ) or beneficiary.get("nazwaBeneficjentaGrupowego") or "—"


def _ownership_history(records: list[dict]) -> tuple[str | None, bool]:
    """(дата начала текущего состава владельцев, менялся ли состав).

    records отсортированы по периоду. Идём с конца, пока набор имён совпадает
    с текущим - его начало и есть «владельцы с такой-то даты».
    """
    sets = [
        (
            r.get("dataPoczatkuPrezentacji"),
            frozenset(_display_name(b) for b in (r.get("listaBeneficjentow") or [])),
        )
        for r in records
    ]
    current_set = sets[-1][1]
    changed = len({s for _, s in sets}) > 1
    owners_since = None
    for start, s in reversed(sets):
        if s == current_set:
            owners_since = start
        else:
            break
    return owners_since, changed


def _parse_response(nip: str, payload: dict, with_network: bool) -> BeneficiariesResult:
    records = payload.get("informacjeOSpolkachIBeneficjentach") or []
    if not records:
        # Запрос удался, но записи нет: интерпретация (норма/красный флаг/
        # освобождение) - на стороне отчёта, зависит от даты регистрации.
        return BeneficiariesResult(
            nip=nip, ok=True, found=False, note="в CRBR нет записи о бенефициарах"
        )

    # Диапазонный ответ идёт записями по периодам; свежий период = текущий состав.
    records = sorted(records, key=lambda r: r.get("dataPoczatkuPrezentacji") or "")
    current = records[-1]
    spolka = current.get("spolka", {}) or {}
    subject_address = _format_address(spolka.get("adresSiedziby"))
    discrepancy = bool(spolka.get("listaInformacjiORozbieznosciach"))
    owners_since, ownership_changed = _ownership_history(records)

    beneficiaries: list[Beneficiary] = []
    for b in current.get("listaBeneficjentow") or []:
        citizenship = [
            o.get("nazwa") for o in (b.get("obywatelstwo") or []) if o.get("nazwa")
        ]
        ben = Beneficiary(
            name=_display_name(b),
            citizenship=citizenship,
            ownership=_ownership_descriptions(b),
        )
        if with_network:
            _attach_network(ben, b.get("pesel"), subject_nip=nip)
        beneficiaries.append(ben)

    return BeneficiariesResult(
        nip=nip,
        ok=True,
        found=True,
        beneficiaries=beneficiaries,
        subject_address=subject_address,
        owners_since=owners_since,
        ownership_changed=ownership_changed,
        discrepancy=discrepancy,
    )


def _attach_network(ben: Beneficiary, pesel: str | None, subject_nip: str) -> None:
    """Обратный поиск по PESEL: другие фирмы владельца. PESEL не сохраняем."""
    if not pesel:
        # Иностранец без PESEL: reverse по имени+дате рождения - в беклоге.
        ben.network_status = "no_pesel"
        return

    payload = {"kontekstWyszukania": 2, "pesel": pesel, **_base_payload()}
    data, err = _call(payload, "reverse")
    if data is None:
        ben.network_status = "error"
        return

    linked: list[LinkedCompany] = []
    for record in data.get("informacjeOSpolkachIBeneficjentach") or []:
        spolka = record.get("spolka", {}) or {}
        if _normalize_nip(spolka.get("nip") or "") == subject_nip:
            continue  # сама проверяемая фирма - не связь
        linked.append(
            LinkedCompany(
                krs=spolka.get("krs"),
                name=spolka.get("pelnaNazwa") or "—",
                legal_form=spolka.get("formaOrganizacyjnaOpis"),
                address=_format_address(spolka.get("adresSiedziby")),
                role="; ".join(_role_in_company(record, pesel)) or None,
                in_proceedings=bool(spolka.get("postepowanie")),
            )
        )

    ben.network_status = "ok"
    if len(linked) > MAX_LINKED_COMPANIES:
        ben.linked_truncated = True
        linked = linked[:MAX_LINKED_COMPANIES]
    ben.linked_companies = linked


def _role_in_company(record: dict, pesel: str) -> list[str]:
    """Роль владельца в связанной фирме; чужие PESEL при этом не сохраняем."""
    for b in record.get("listaBeneficjentow") or []:
        if b.get("pesel") == pesel:
            return _ownership_descriptions(b)
    return []
