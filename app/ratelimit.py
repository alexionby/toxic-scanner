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


def client_ip(request: Request) -> str:
    # За прокси Cloud Run реальный IP клиента — первый в X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _is_loopback(ip: str) -> bool:
    """Локальная разработка (127.0.0.1/::1) не должна упираться в квоту.

    В проде IP берётся из X-Forwarded-For, туда loopback от внешнего
    клиента не попадёт, так что освобождение безопасно.
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

    ip = client_ip(request)
    if _is_loopback(ip):
        # Локальная разработка не тратит квоту и не блокируется.
        return

    if _global_count >= GLOBAL_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail="Дневной лимит отчётов исчерпан. Загляните завтра.",
        )

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
