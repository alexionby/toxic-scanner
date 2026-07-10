"""Предохранители расходов на построение отчёта.

Отчёт стоит реальных денег (Gemini + Tavily + чтение okredo/aleo через
Jina), а эндпоинт публичный. Здесь два дневных потолка:

- глобальный на весь сервис — жёсткая граница максимального счёта в день;
- на один IP — чтобы зацикленный скрипт с одного адреса не выел квоту.

Счётчики держим в памяти: инстанс один (Cloud Run min-instances=0), этого
достаточно как предохранителя. При холодном старте они сбрасываются — для
суточного ограничения расходов это приемлемо.
"""

from __future__ import annotations

import ipaddress
import os
from datetime import date

from fastapi import HTTPException, Request

from app.telemetry import distinct_id_from_ip, track

GLOBAL_PER_DAY = int(os.environ.get("RATE_LIMIT_GLOBAL_PER_DAY", "50"))
IP_PER_DAY = int(os.environ.get("RATE_LIMIT_IP_PER_DAY", "10"))
# Сколько доверенных инфраструктурных хопов дописано СПРАВА от реального IP
# клиента в X-Forwarded-For. Прямой Cloud Run (*.run.app, наш деплой): Google
# Front End вписывает реальный IP последним → 0 (берём самый правый). За
# внешним HTTPS Load Balancer / CDN добавляется ещё хоп справа ("<клиент>,
# <IP LB>") → поднять через env. Значение зависит от топологии: при сомнении
# замерить реальный X-Forwarded-For на проде.
TRUSTED_PROXY_HOPS = int(os.environ.get("TRUSTED_PROXY_HOPS", "0"))
# Оффер пейвола: пакет из PACK_SIZE отчётов за PRICE_PLN злотых.
REPORT_PRICE_PLN = int(os.environ.get("REPORT_PRICE_PLN", "20"))
REPORT_PACK_SIZE = int(os.environ.get("REPORT_PACK_SIZE", "10"))

_current_day: date | None = None
_global_count = 0
_ip_counts: dict[str, int] = {}


def _roll_day() -> None:
    """Сбросить счётчики на смене календарного дня."""
    global _current_day, _global_count, _ip_counts
    today = date.today()
    if _current_day != today:
        _current_day = today
        _global_count = 0
        _ip_counts = {}


def _is_valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def client_ip(request: Request) -> str:
    # X-Forwarded-For заполняется слева направо от НЕдоверенного к доверенному:
    # левые значения подставляет сам клиент, правые дописывает инфраструктура.
    # Поэтому реальный IP берём ОТСЧЁТОМ СПРАВА, пропустив TRUSTED_PROXY_HOPS
    # инфра-хопов, а НЕ первым слева (тот подделывается → обход per-IP лимита).
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        idx = len(parts) - 1 - TRUSTED_PROXY_HOPS
        if idx >= 0 and _is_valid_ip(parts[idx]):
            return parts[idx]
    # XFF нет / короче ожидаемого / мусор → неподделываемый адрес сокета.
    return request.client.host if request.client else "unknown"


def _is_loopback(ip: str) -> bool:
    """True для 127.0.0.1/::1.

    ВАЖНО: подавать сюда адрес сокета (request.client.host), а НЕ client_ip():
    последний читает X-Forwarded-For, который внешний клиент может выставить
    в "127.0.0.1" и притвориться локальным. Адрес сокета подделать нельзя.
    """
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def enforce_report_quota(request: Request) -> None:
    """Проверить и списать квоту перед построением отчёта.

    Зависимость FastAPI: срабатывает до тела эндпоинта. Слот расходуется
    на попытку (вход = намерение потратить деньги на внешние API).

    Два разных потолка:
    - глобальный дневной — сухой 429 «приходите завтра» (бюджет сервиса);
    - per-IP — это пейвол: исчерпал бесплатные отчёты → 402 с фейк-дором
      «следующий за N zł». Понижение IP_PER_DAY превращает его в реальный
      замеритель готовности платить.
    """
    global _global_count
    _roll_day()

    # Освобождаем локальную разработку по НЕПОДДЕЛЫВАЕМОМУ адресу сокета.
    # client_ip() читает X-Forwarded-For (клиент может подставить туда
    # "127.0.0.1" и обойти оба потолка), поэтому для решения об освобождении
    # берём реальный пир: за Cloud Run это инфраструктура Google, не loopback.
    peer = request.client.host if request.client else ""
    if _is_loopback(peer):
        return

    if _global_count >= GLOBAL_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail="Дневной лимит отчётов исчерпан. Загляните завтра.",
        )

    ip = client_ip(request)
    used = _ip_counts.get(ip, 0)
    if used >= IP_PER_DAY:
        track(
            "paywall_shown",
            distinct_id=distinct_id_from_ip(ip),
            free_used=used,
            price_pln=REPORT_PRICE_PLN,
            pack_size=REPORT_PACK_SIZE,
        )
        raise HTTPException(
            status_code=402,
            detail={
                "paywall": True,
                "price_pln": REPORT_PRICE_PLN,
                "pack_size": REPORT_PACK_SIZE,
                "free_used": used,
            },
        )

    _global_count += 1
    _ip_counts[ip] = used + 1
