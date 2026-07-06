"""Финансовый адаптер: годовые показатели компании из агрегаторов.

Первоисточник (Repozytorium Dokumentów Finansowych) закрыт bot-защитой
Imperva и требует обхода, что запрещено принципами проекта. Поэтому
берём те же цифры у агрегаторов, которые уже легально переварили RDF,
через уже используемый в проекте Jina Reader (без браузера).

- okredo.com - основной: отдаёт полную таблицу за несколько лет
  (выручка, прибыль, капитал, обязательства, активы).
- aleo.com - fallback и кросс-валидация выручки.

Оба - ВТОРИЧНЫЕ источники: помечать в отчёте соответственно. Структура
подтверждена живыми вызовами (MPSYSTEM KRS 0000475078, CD Projekt
0000006865, 2026-07-06).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

JINA_BASE = "https://r.jina.ai"
REQUEST_TIMEOUT_SECONDS = 45

# Транслитерация польских букв для построения slug agregatora.
_PL_TRANSLIT = str.maketrans(
    {
        "ą": "a", "ć": "c", "ę": "e", "ł": "l", "ń": "n",
        "ó": "o", "ś": "s", "ź": "z", "ż": "z",
    }
)


@dataclass
class YearFinancials:
    year: int
    revenue: float | None = None
    net_profit: float | None = None
    equity: float | None = None
    liabilities: float | None = None
    non_current_assets: float | None = None


@dataclass
class FinancialsResult:
    krs: str
    years: list[YearFinancials] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    source_urls: dict[str, str] = field(default_factory=dict)
    cross_check: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return any(
            y.revenue is not None or y.net_profit is not None for y in self.years
        )


def company_slug(name: str) -> str:
    """slug агрегатора: транслит названия, орг.-формы пишутся словами.

    'MPSYSTEM SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ' ->
    'mpsystem-spolka-z-ograniczona-odpowiedzialnoscia'
    """
    lowered = name.lower().translate(_PL_TRANSLIT)
    tokens = re.sub(r"[^a-z0-9]+", " ", lowered).split()
    return "-".join(tokens)


def _fetch_markdown(url: str) -> str | None:
    try:
        response = requests.get(
            f"{JINA_BASE}/{url}",
            headers={"Accept": "text/markdown"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.warning("Jina fetch failed for %s", url, exc_info=True)
        return None
    if response.status_code != 200 or len(response.text) < 800:
        # Короткий ответ = заглушка/защита, а не контент.
        return None
    return response.text


def _parse_number(raw: str) -> float | None:
    cleaned = raw.strip()
    if not cleaned or cleaned in {"-", "—", "–"}:
        return None
    # Убираем разделители тысяч (пробелы, запятые) и валюту.
    cleaned = re.sub(r"[^\d.\-]", "", cleaned.replace(",", "").replace(" ", ""))
    if cleaned in {"", "-", "."}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# Jina разворачивает финансовую таблицу okredo в плоский поток
# одноколоночных ячеек: строка года, затем значения показателей
# в фиксированном порядке. Позиции сверены с живым ответом okredo
# en-pl (2026-07-06):
#   0 Turnover, 1 Profit before tax, 2 Net Profit, 3 Working capital %,
#   4 Liquidity, 5 Net profitability %, 6 Equity,
#   7 Current liabilities, 8 Non-current liabilities, 9 Non-current Assets.
_OKREDO_POSITIONS = {
    "revenue": 0,
    "net_profit": 2,
    "equity": 6,
    "non_current_assets": 9,
}
_OKREDO_CURRENT_LIAB = 7
_OKREDO_NONCURRENT_LIAB = 8


def _single_cells(markdown: str) -> list[str]:
    """Содержимое одноколоночных строк '| x |' (двухколоночные пропускаем)."""
    cells = []
    for line in markdown.splitlines():
        m = re.fullmatch(r"\|\s*([^|]+?)\s*\|", line.strip())
        if m:
            cells.append(m.group(1).strip())
    return cells


def _completeness(yf: YearFinancials) -> int:
    return sum(
        v is not None
        for v in (yf.revenue, yf.net_profit, yf.equity, yf.liabilities, yf.non_current_assets)
    )


def _parse_okredo(markdown: str) -> list[YearFinancials]:
    cells = _single_cells(markdown)
    results: dict[int, YearFinancials] = {}

    i = 0
    while i < len(cells):
        year_match = re.fullmatch(r"20[12][0-9]", cells[i])
        if not year_match:
            i += 1
            continue
        year = int(cells[i])
        # Собираем значения до следующего года / нечисловой ячейки.
        values: list[float | None] = []
        j = i + 1
        while j < len(cells) and not re.fullmatch(r"20[12][0-9]", cells[j]):
            cell = cells[j]
            # markdown-разделитель заголовка ('---') - пропускаем молча.
            if re.fullmatch(r"[-—–]{2,}", cell):
                j += 1
                continue
            num = _parse_number(cell)
            # Одиночный дефис = пустое значение; иной текст -> конец блока.
            if num is None and not re.fullmatch(r"[-—–]", cell):
                break
            values.append(num)
            j += 1

        yf = YearFinancials(year=year)
        for field_name, pos in _OKREDO_POSITIONS.items():
            if pos < len(values):
                setattr(yf, field_name, values[pos])
        parts = [
            values[p]
            for p in (_OKREDO_CURRENT_LIAB, _OKREDO_NONCURRENT_LIAB)
            if p < len(values) and values[p] is not None
        ]
        if parts:
            yf.liabilities = sum(parts)

        # okredo дублирует таблицу - оставляем более полную запись.
        if year not in results or _completeness(yf) > _completeness(results[year]):
            results[year] = yf
        i = j

    return [results[y] for y in sorted(results)]


def _parse_aleo_revenue(markdown: str) -> dict[int, float]:
    """Best-effort: выручка по годам из aleo (fallback/сверка).

    aleo отдаёт данные до paywall неполно; берём только пары
    'год + число рядом с Przychody', чего достаточно для сверки.
    """
    idx = markdown.lower().find("przychody netto ze sprzeda")
    if idx < 0:
        return {}
    window = markdown[idx : idx + 4000]
    revenue: dict[int, float] = {}
    # Значение выручки должно содержать разделитель тысяч и быть заметно
    # больше года, иначе regex ловит сам год как «число».
    for m in re.finditer(r"(20[12][0-9])[^\d]{1,40}?(\d{1,3}[ .,]\d{3}[ .,\d]*)", window):
        year = int(m.group(1))
        value = _parse_number(m.group(2))
        if value and value >= 100_000 and year not in revenue:
            revenue[year] = value
    return revenue


def get_financials(company_name: str, krs: str) -> FinancialsResult:
    """Годовые финансовые показатели: okredo основной, aleo fallback+сверка."""
    slug = company_slug(company_name)
    result = FinancialsResult(krs=krs)

    okredo_url = f"https://okredo.com/en-pl/company/{slug}-krs-{krs}"
    okredo_md = _fetch_markdown(okredo_url)
    if okredo_md:
        years = _parse_okredo(okredo_md)
        if years:
            result.years = years
            result.sources.append("okredo")
            result.source_urls["okredo"] = okredo_url

    aleo_url = f"https://aleo.com/pl/firma/{slug}"
    aleo_md = _fetch_markdown(aleo_url)
    aleo_revenue = _parse_aleo_revenue(aleo_md) if aleo_md else {}
    if aleo_md:
        result.source_urls["aleo"] = aleo_url

    # Fallback: okredo пуст, но aleo дал выручку.
    if not result.has_data and aleo_revenue:
        result.years = [
            YearFinancials(year=y, revenue=v) for y, v in sorted(aleo_revenue.items())
        ]
        result.sources.append("aleo")
        result.notes.append("okredo без данных - выручка взята с aleo (менее полно)")
    # Кросс-валидация: где okredo и aleo дают выручку за общий год.
    elif result.has_data and aleo_revenue:
        for yf in result.years:
            aleo_val = aleo_revenue.get(yf.year)
            if aleo_val is None or yf.revenue is None:
                continue
            diff = abs(aleo_val - yf.revenue) / max(yf.revenue, 1)
            if diff <= 0.02:
                result.cross_check.append(f"{yf.year}: выручка совпала с aleo (±2%)")
            else:
                result.cross_check.append(
                    f"{yf.year}: РАСХОЖДЕНИЕ okredo {yf.revenue:,.0f} vs "
                    f"aleo {aleo_val:,.0f} ({diff:.0%})"
                )
        if "aleo" not in result.sources:
            result.sources.append("aleo (сверка)")

    if not result.has_data:
        result.notes.append("финансовые данные в агрегаторах не найдены")

    return result
