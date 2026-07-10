"""Единая точка отправки продуктовых событий.

Два стока сразу: (1) лог — на Cloud Run stdout уходит в Cloud Logging,
переживает холодные старты; (2) PostHog Cloud EU — серверный capture,
если задан POSTHOG_API_KEY. Оба получают уже отредактированные props.

distinct_id — хэш IP (аудитория под адблоками, клиентский JS занизит).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys

import requests

logger = logging.getLogger("toxic_scanner.events")

# PII вейтлиста держим в отдельном стоке, а не в продуктовой телеметрии.
waitlist_logger = logging.getLogger("toxic_scanner.waitlist")


def _ensure_stdout_handler(target: logging.Logger) -> None:
    """Под uvicorn у наших логгеров нет своего хендлера, и события молча
    теряются. Вешаем вывод в stdout (на Cloud Run → Cloud Logging; stdout,
    не stderr — иначе всё метится severity=ERROR). Идемпотентно."""
    target.setLevel(logging.INFO)
    target.propagate = False
    if not any(getattr(h, "_toxic_scanner", False) for h in target.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        handler._toxic_scanner = True  # type: ignore[attr-defined]
        target.addHandler(handler)


_ensure_stdout_handler(logger)
_ensure_stdout_handler(waitlist_logger)

_SALT = os.environ.get("TELEMETRY_SALT", "toxic-scanner")

# Defense-in-depth: в общий поток событий PII не должен попадать даже по
# ошибке — редактируем значения ключей, похожих на секреты/контакты.
_SENSITIVE_KEY = re.compile(r"email|token|secret|password", re.IGNORECASE)

# PostHog Cloud EU: серверный capture. Ключ phc_ публичный (на запись),
# но держим в env, не в коде. Без ключа отправка — no-op (локально).
_POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY", "")
_POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://eu.i.posthog.com")
_POSTHOG_TIMEOUT = float(os.environ.get("POSTHOG_TIMEOUT_SECONDS", "2"))


def distinct_id_from_ip(ip: str) -> str:
    return hashlib.sha256(f"{_SALT}:{ip}".encode()).hexdigest()[:16]


def _send_to_posthog(event: str, distinct_id: str | None, props: dict) -> None:
    """Синхронный capture в PostHog. Короткий таймаут + проглатывание
    ошибок: телеметрия никогда не должна ронять или подвешивать запрос."""
    if not _POSTHOG_API_KEY:
        return
    try:
        requests.post(
            f"{_POSTHOG_HOST}/capture/",
            json={
                "api_key": _POSTHOG_API_KEY,
                "event": event,
                "distinct_id": distinct_id or "anonymous",
                "properties": props,
            },
            timeout=_POSTHOG_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — телеметрия не критична к сбою
        logger.warning("posthog capture failed: %s", exc)


def track(event: str, distinct_id: str | None = None, **props: object) -> None:
    """Записать продуктовое событие. Формат строки стабилен — по префиксу
    EVENT его легко выбрать в Cloud Logging до появления PostHog.

    Чувствительные значения редактируются: аналитика не место для PII.
    """
    # Редактируем только строковые значения: сырой PII — это строки, а
    # булев флаг вроде has_email или число — сам по себе не секрет.
    safe = {
        key: (
            "[redacted]"
            if isinstance(value, str) and _SENSITIVE_KEY.search(key)
            else value
        )
        for key, value in props.items()
    }
    payload = {"event": event, "distinct_id": distinct_id, "props": safe}
    logger.info("EVENT %s", json.dumps(payload, ensure_ascii=False))
    _send_to_posthog(event, distinct_id, safe)


def record_waitlist_email(
    distinct_id: str | None, email: str, **props: object
) -> None:
    """Сохранить email из фейк-дора в выделенный сток.

    ВРЕМЕННО в логах: БД пока нет (min-instances=0). До монетизации
    перенести в access-controlled хранилище — см. BACKLOG.
    """
    payload = {"distinct_id": distinct_id, "email": email, **props}
    waitlist_logger.info("WAITLIST %s", json.dumps(payload, ensure_ascii=False))
