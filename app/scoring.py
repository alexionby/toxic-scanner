"""Звезда качества v2 - пять осей оценки работодателя.

Каждая ось: {"value": int 0-100 | None, "basis": list[str]}.
value=None - честное "нет данных" (на радаре ось серым пунктиром),
НИКОГДА не "50 из 100". Надёжность, прозрачность, финансы и динамика
считаются детерминированными формулами (одпис KRS + финансовый
адаптер); LLM-агент оценивает только "людей" (интерпретация отзывов).
Веса v1 - произвольные, важны детерминизм и объяснимость: каждое
начисление - строка в basis.
"""

from __future__ import annotations

import json
import re
from datetime import date

from app.models import CompanyCandidate
from app.sources.financials import FinancialsResult

Axis = dict  # {"value": int | None, "basis": list[str]}

LLM_AXES = ("people",)

_NO_ODPIS_BASIS = ["нет одписа KRS"]
_NO_LLM_BASIS = ["оценка не получена от агента"]

# Годовой отчёт за год Y обязан попасть в KRS примерно к 15 июля Y+1:
# до 6 месяцев на утверждение собранием + 15 дней на подачу.
_FILING_DEADLINE_MONTH_DAY = (7, 15)


def expected_statement_year(today: date) -> int:
    """За какой год отчёт уже ОБЯЗАН лежать в реестре на дату today."""
    if (today.month, today.day) >= _FILING_DEADLINE_MONTH_DAY:
        return today.year - 1
    return today.year - 2


def latest_statement_year(period: str | None) -> int | None:
    """Год из периода одписа вида "OD 01.01.2024 DO 31.12.2024"."""
    years = [int(y) for y in re.findall(r"\b(\d{4})\b", period or "")]
    return max(years) if years else None


def _no_data_axis(basis: list[str]) -> Axis:
    return {"value": None, "basis": list(basis)}


def _years_word(years: int) -> str:
    if years % 10 == 1 and years % 100 != 11:
        return "год"
    if years % 10 in (2, 3, 4) and years % 100 not in (12, 13, 14):
        return "года"
    return "лет"


def _parse_registration_date(value: str | None) -> date | None:
    """Дата из одписа приходит строкой "DD.MM.YYYY"."""
    if not value:
        return None
    try:
        day, month, year = value.strip().split(".")
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _full_years(since: date, today: date) -> int:
    years = today.year - since.year
    if (today.month, today.day) < (since.month, since.day):
        years -= 1
    return years


def _parse_capital_pln(value: str | None) -> float | None:
    """Капитал из одписа приходит строкой вида "2126050,00 PLN"."""
    if not value:
        return None
    match = re.search(r"\d[\d\s .]*(?:,\d+)?", value)
    if not match:
        return None
    number = match.group(0)
    number = re.sub(r"[\s .]", "", number).replace(",", ".")
    try:
        return float(number)
    except ValueError:
        return None


def reliability(company: CompanyCandidate, today: date | None = None) -> Axis:
    facts = company.facts
    if facts is None:
        return _no_data_axis(_NO_ODPIS_BASIS)

    today = today or date.today()
    value = 0
    basis: list[str] = []

    registered = _parse_registration_date(facts.registration_date)
    if registered is None:
        basis.append("дата регистрации неизвестна: +0")
    else:
        years = _full_years(registered, today)
        if years >= 10:
            points = 40
        elif years >= 5:
            points = 30
        elif years >= 2:
            points = 20
        else:
            points = 5
        value += points
        basis.append(
            f"{years} {_years_word(years)} в реестре "
            f"(с {facts.registration_date}): +{points}"
        )

    capital = _parse_capital_pln(facts.share_capital)
    if capital is None:
        basis.append("уставный капитал неизвестен: +0")
    elif capital >= 1_000_000:
        value += 20
        basis.append(f"капитал {facts.share_capital} (от 1 млн): +20")
    elif capital >= 100_000:
        value += 15
        basis.append(f"капитал {facts.share_capital} (от 100 тыс.): +15")
    elif capital > 5_000:
        value += 10
        basis.append(f"капитал {facts.share_capital} (выше минимального): +10")
    else:
        basis.append(f"капитал {facts.share_capital} - минимальный уставный: +0")

    if facts.arrears_flags:
        basis.append(f"dział 4 (задолженности): {', '.join(facts.arrears_flags)}: +0")
    else:
        value += 20
        basis.append("dział 4 (задолженности) пуст: +20")

    if facts.distress_flags:
        basis.append(
            f"dział 6 (ликвидация/банкротство): "
            f"{', '.join(facts.distress_flags)}: +0"
        )
    else:
        value += 20
        basis.append("dział 6 (ликвидация/банкротство) пуст: +20")

    return {"value": value, "basis": basis}


def transparency(company: CompanyCandidate, today: date | None = None) -> Axis:
    facts = company.facts
    if facts is None:
        return _no_data_axis(_NO_ODPIS_BASIS)

    today = today or date.today()
    value = 0
    basis: list[str] = []

    if facts.annual_statements:
        value += 25
        basis.append(
            f"годовая отчётность сдаётся ({len(facts.annual_statements)} шт.): +25"
        )
    else:
        basis.append("годовые отчёты не сдавались: +0")

    last_year = latest_statement_year(facts.last_statement_period)
    if last_year is not None:
        # Свежесть меряем от законного дедлайна, а не "не старше двух
        # лет": до ~15 июля отчёт за прошлый год ещё не обязан быть сдан.
        expected = expected_statement_year(today)
        if last_year >= expected:
            value += 40
            basis.append(
                f"последний отчёт за {last_year}: требование выполнено, "
                f"срок подачи за {last_year + 1} год ещё не истёк: +40"
            )
        elif last_year == expected - 1:
            value += 15
            basis.append(
                f"отчёт за {expected} год просрочен "
                f"(срок подачи ~15.07.{expected + 1}): +15"
            )
        else:
            basis.append(
                f"последний отчёт за {last_year}, просрочен более чем на год: +0"
            )
    else:
        basis.append("период последнего отчёта неизвестен: +0")

    requisites = (("NIP", company.nip), ("REGON", company.regon), ("адрес", company.address))
    missing = [name for name, filled in requisites if not filled]
    if not missing:
        value += 15
        basis.append("NIP, REGON и адрес заполнены: +15")
    else:
        basis.append(f"не заполнено: {', '.join(missing)}: +0")

    if facts.share_capital:
        value += 10
        basis.append("уставный капитал раскрыт: +10")
    else:
        basis.append("уставный капитал не раскрыт: +0")

    if facts.registration_date:
        value += 10
        basis.append("дата регистрации раскрыта: +10")
    else:
        basis.append("дата регистрации не раскрыта: +0")

    return {"value": value, "basis": basis}


def _fmt_pln(value: float) -> str:
    return f"{value:,.0f} PLN".replace(",", " ")


def _latest_with(years: list, attr: str):
    """Самый свежий год, где заполнено поле attr."""
    for yf in sorted(years, key=lambda y: y.year, reverse=True):
        if getattr(yf, attr) is not None:
            return yf
    return None


def finances(fin: FinancialsResult) -> Axis:
    """Финансовое здоровье: прибыльность, капитал, долговая нагрузка."""
    if not fin.has_data:
        return _no_data_axis(["финансовые данные в агрегаторах не найдены"])

    value = 0
    basis: list[str] = []

    profit_year = _latest_with(fin.years, "net_profit")
    if profit_year is None:
        basis.append("чистая прибыль неизвестна: +0")
    elif profit_year.net_profit > 0:
        value += 40
        basis.append(f"прибыль в {profit_year.year} ({_fmt_pln(profit_year.net_profit)}): +40")
        if profit_year.revenue and profit_year.net_profit / profit_year.revenue >= 0.08:
            value += 10
            margin = profit_year.net_profit / profit_year.revenue
            basis.append(f"маржа {margin:.0%} (от 8%): +10")
    else:
        basis.append(f"убыток в {profit_year.year} ({_fmt_pln(profit_year.net_profit)}): +0")

    equity_year = _latest_with(fin.years, "equity")
    if equity_year is None:
        basis.append("капитал неизвестен: +0")
    elif equity_year.equity > 0:
        value += 20
        basis.append(f"положительный капитал в {equity_year.year}: +20")
        if equity_year.liabilities is not None:
            ratio = equity_year.liabilities / equity_year.equity
            if ratio < 1:
                pts = 20
            elif ratio < 2:
                pts = 12
            elif ratio < 4:
                pts = 6
            else:
                pts = 0
            value += pts
            basis.append(f"обязательства/капитал = {ratio:.1f}: +{pts}")
    else:
        basis.append(f"отрицательный капитал в {equity_year.year}: +0")

    profits = [y.net_profit for y in fin.years if y.net_profit is not None]
    if profits and all(p > 0 for p in profits):
        value += 10
        basis.append(f"все {len(profits)} отчётных лет прибыльны: +10")
    elif profits:
        losses = sum(1 for p in profits if p <= 0)
        basis.append(f"убыточных лет: {losses}: +0")

    return {"value": min(100, value), "basis": basis}


def dynamics(fin: FinancialsResult) -> Axis:
    """Динамика: тренд выручки и прибыли год-к-году."""
    rev_years = sorted((y for y in fin.years if y.revenue is not None), key=lambda y: y.year)
    if len(rev_years) < 2:
        return _no_data_axis(["нужны данные о выручке минимум за 2 года"])

    value = 50
    basis: list[str] = []

    prev, last = rev_years[-2], rev_years[-1]
    change = (last.revenue - prev.revenue) / prev.revenue
    points = max(-30, min(30, round(change * 150)))
    value += points
    basis.append(
        f"выручка {prev.year}->{last.year}: {change:+.0%} "
        f"({_fmt_pln(prev.revenue)} -> {_fmt_pln(last.revenue)}): {points:+d}"
    )

    prof_years = sorted((y for y in fin.years if y.net_profit is not None), key=lambda y: y.year)
    if len(prof_years) >= 2:
        if prof_years[-1].net_profit > prof_years[-2].net_profit:
            value += 10
            basis.append(f"чистая прибыль растёт ({prof_years[-1].year}): +10")
        elif prof_years[-1].net_profit < prof_years[-2].net_profit:
            value -= 10
            basis.append(f"чистая прибыль падает ({prof_years[-1].year}): -10")

    return {"value": max(0, min(100, value)), "basis": basis}


def _normalize_llm_axis(raw: object) -> Axis:
    """Приводит ось из ответа агента к контракту, мусор превращает в null."""
    if not isinstance(raw, dict):
        return _no_data_axis(_NO_LLM_BASIS)

    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = None
    else:
        value = max(0, min(100, int(value)))

    basis_raw = raw.get("basis")
    basis = (
        [str(item) for item in basis_raw if str(item).strip()]
        if isinstance(basis_raw, list)
        else []
    )
    if not basis:
        basis = ["агент не привёл обоснования"] if value is not None else list(_NO_LLM_BASIS)

    return {"value": value, "basis": basis}


def split_llm_scores(report: str) -> tuple[str, dict[str, Axis]]:
    """Отделяет JSON-блок оценок агента от текста отчёта.

    Возвращает (отчёт без блока, {"finances": Axis, "people": Axis}).
    Блок ищется с конца отчёта; если его нет или он не парсится -
    отчёт возвращается как есть, обе оси null (не падаем).
    """
    fallback = {axis: _no_data_axis(_NO_LLM_BASIS) for axis in LLM_AXES}

    tail_start = max(0, len(report) - 4000)
    tail = report[tail_start:]
    decoder = json.JSONDecoder()

    for pos in range(len(tail) - 1, -1, -1):
        if tail[pos] != "{":
            continue
        try:
            parsed, end = decoder.raw_decode(tail[pos:])
        except ValueError:
            continue
        if not isinstance(parsed, dict) or not all(k in parsed for k in LLM_AXES):
            continue

        before = report[: tail_start + pos]
        after = tail[pos + end :]
        # блок может быть обёрнут в код-фенс вопреки инструкции
        before = re.sub(r"```(?:json)?\s*$", "", before.rstrip()).rstrip()
        after = re.sub(r"^\s*```\s*", "", after.lstrip())
        clean_report = (before + ("\n\n" + after.strip() if after.strip() else "")).strip()

        axes = {axis: _normalize_llm_axis(parsed.get(axis)) for axis in LLM_AXES}
        return clean_report, axes

    return report, fallback


def build_scores(
    company: CompanyCandidate,
    fin: FinancialsResult,
    llm_axes: dict[str, Axis] | None = None,
    today: date | None = None,
) -> dict[str, Axis]:
    """Пять осей в порядке отрисовки радара."""
    llm_axes = llm_axes or {}
    return {
        "reliability": reliability(company, today),
        "finances": finances(fin),
        "dynamics": dynamics(fin),
        "people": llm_axes.get("people", _no_data_axis(_NO_LLM_BASIS)),
        "transparency": transparency(company, today),
    }
