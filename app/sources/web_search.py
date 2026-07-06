"""Discovery-адаптер: находит кандидатов юр. лиц по названию через Tavily.

Официальные реестры не дают full-text поиск по названию - только
lookup по точному KRS/NIP/REGON. Поэтому discovery идёт через web
search, а верификацию каждой находки делает krs.py.
"""

from __future__ import annotations

import re

from langchain_tavily import TavilySearch

from app.sources.base import RawCompanyHit

_KRS_PATTERN = re.compile(r"\bKRS[:\s]*([0-9]{6,10})\b", re.IGNORECASE)
_NIP_PATTERN = re.compile(
    r"\bNIP[:\s]*([0-9]{3}-?[0-9]{3}-?[0-9]{2}-?[0-9]{2}|[0-9]{10})\b", re.IGNORECASE
)


def _extract_identifiers(text: str) -> tuple[str | None, str | None]:
    krs_match = _KRS_PATTERN.search(text)
    nip_match = _NIP_PATTERN.search(text)
    krs = krs_match.group(1).zfill(10) if krs_match else None
    nip = re.sub(r"-", "", nip_match.group(1)) if nip_match else None
    return krs, nip


def search_company_candidates(
    company_name: str, country: str = "PL", max_results: int = 5
) -> list[RawCompanyHit]:
    """Ищет упоминания компании вместе с её официальными реквизитами.

    Запрос таргетирован на реестровые упоминания, чтобы в сниппетах
    чаще попадались KRS/NIP, а не общий шум по названию.
    """
    query = f'"{company_name}" KRS NIP REGON spółka {country}'
    # Инстанс создаётся здесь, а не на уровне модуля, чтобы не зависеть
    # от порядка load_dotenv() при импорте (см. main.py).
    search_tool = TavilySearch(max_results=max_results)
    raw_results = search_tool.invoke({"query": query})

    hits: list[RawCompanyHit] = []
    for item in raw_results.get("results", []):
        content = f"{item.get('title', '')} {item.get('content', '')}"
        krs, nip = _extract_identifiers(content)
        if not krs and not nip:
            continue  # без идентификатора кандидата нечем верифицировать

        hits.append(
            RawCompanyHit(
                name=item.get("title", company_name),
                source="web_search",
                url=item.get("url"),
                krs=krs,
                nip=nip,
                raw=item,
            )
        )
    return hits
