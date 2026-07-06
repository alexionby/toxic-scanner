"""Health check v0.2: финансы + отзывы сотрудников.

Официальные реквизиты приходят из Company Resolver и попадают в отчёт
как проверенные факты. Финансовые показатели LLM-агент собирает с
публичных агрегаторов (aleo.com, rejestr.io), отзывы - с GoWork и
Reddit; всё это вторичные источники, и отчёт обязан помечать их как
таковые. Первоисточник финансов (Repozytorium Dokumentów Finansowych,
XML-парсинг) - в беклоге.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import requests
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_tavily import TavilySearch
from langgraph.prebuilt import create_react_agent

from app.models import CompanyCandidate


def message_content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text is not None:
                    parts.append(str(text))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False, indent=2))
            else:
                parts.append(str(item))
        return "\n\n".join(parts)

    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        if text is not None:
            return str(text)
        return json.dumps(content, ensure_ascii=False, indent=2)

    return str(content)


@tool
def extract_website_text(url: str) -> str:
    """Используй это, чтобы прочитать полный текст веб-страницы."""
    headers = {"Accept": "text/markdown"}
    try:
        # Jina помогает обойти защиты и возвращает чистый текст
        response = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=15)
        if response.status_code == 200:
            return response.text[:15000]  # Защита от переполнения контекста
    except Exception as e:
        return f"Ошибка при доступе к {url}: {str(e)}"
    return f"Ошибка при чтении сайта: {response.status_code}"


FINANCIAL_SYSTEM_PROMPT = """\
Ты - аналитик, который строит health check польской компании для
человека, решающего, подписывать ли с ней контракт или оффер.

Правила:
- Юрлицо уже подтверждено по государственным реестрам, его реквизиты
  даны в задаче. Не меняй их и не "уточняй".
- В задаче есть блок "Жёсткие факты из одписа KRS" - это первоисточник
  (государственный реестр). Строй выводы в первую очередь на нём.
  Польские компании обязаны сдавать годовой отчёт ежегодно: если
  последний сданный отчёт старше двух лет - это серьёзный красный флаг.
  Молодая компания с минимальным капиталом (5 000 PLN) без сданной
  отчётности - признак потенциальной однодневки, скажи об этом прямо.
- Каждый факт сопровождай ссылкой на источник, из которого ты его взял.
- Финансовые цифры ищи на публичных агрегаторах (aleo.com, rejestr.io) -
  это вторичные источники: прямо помечай их в отчёте как
  "по данным агрегаторов, не первоисточник".
- Не выдумывай цифры. Если данных нет - пиши "данных нет" и учитывай
  это как отдельный риск-флаг, а не как нейтральный факт.
- Отзывы сотрудников ищи на gowork.pl (профиль компании) и reddit.com
  (публичные упоминания). Это слабый неофициальный сигнал: передавай
  повторяющиеся темы, а не отдельные крайние мнения, и не выдавай
  отзывы за факты о компании. Если отзывов нет - так и напиши (для
  маленьких компаний это нормально, а не риск).
- Отделяй факты от предположений.

Структура отчёта (Markdown, на русском):
# Health Check: <название компании>
- **Health score**: 0-100 с одним предложением обоснования
- **Идентификация**: KRS, NIP, REGON, адрес, статус (из задачи)
- **Факты из реестра KRS**: дата регистрации, капитал, сдача годовой
  отчётности, задолженности, ликвидация/банкротство - как есть из задачи
- **Краткий вывод**: 2-4 предложения
- **Финансовые показатели по годам**: таблица (выручка, прибыль/убыток,
  активы, капитал) со ссылками на источники; если данных нет - явно
  скажи об этом
- **Отзывы сотрудников**: повторяющиеся жалобы и плюсы с GoWork/Reddit
  со ссылками, с пометкой "неофициальный сигнал"; если отзывов не
  нашлось - явно скажи об этом
- **Риск-флаги**: список
- **Качество данных**: какие источники нашлись, чего не хватает
- **Источники**: список URL
- **Что проверить перед подписанием контракта**: список
"""

_agent = None


def _get_agent():
    """Ленивая инициализация: ключи из .env к этому моменту уже загружены."""
    global _agent
    if _agent is None:
        llm = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite", temperature=0.3)
        tools = [TavilySearch(max_results=5), extract_website_text]
        _agent = create_react_agent(llm, tools, prompt=FINANCIAL_SYSTEM_PROMPT)
    return _agent


def _facts_block(company: CompanyCandidate) -> str:
    facts = company.facts
    if facts is None:
        return "Жёсткие факты из одписа KRS: недоступны.\n\n"

    statements = facts.annual_statements
    if statements:
        recent = ", ".join(s.period for s in statements[-3:])
        statements_line = (
            f"сдано {len(statements)} шт., последние периоды: {recent}"
        )
    else:
        statements_line = "не сдавались (ни одной записи в реестре)"

    return (
        "Жёсткие факты из одписа KRS (первоисточник, приоритет над "
        "агрегаторами):\n"
        f"- Дата регистрации в KRS: {facts.registration_date or 'нет данных'}\n"
        f"- Правовая форма: {facts.legal_form or 'нет данных'}\n"
        f"- Уставный капитал: {facts.share_capital or 'нет данных'}\n"
        f"- Годовые отчёты: {statements_line}\n"
        f"- Задолженности/взыскания (dział 4): "
        f"{', '.join(facts.arrears_flags) if facts.arrears_flags else 'записей нет'}\n"
        f"- Ликвидация/банкротство (dział 6): "
        f"{', '.join(facts.distress_flags) if facts.distress_flags else 'записей нет'}\n\n"
    )


def _task_prompt(company: CompanyCandidate) -> str:
    return (
        "Построй financial health check компании.\n\n"
        "Официальные данные (подтверждены государственными реестрами, "
        "используй их как есть):\n"
        f"- Название: {company.name}\n"
        f"- KRS: {company.krs or 'нет'}\n"
        f"- NIP: {company.nip or 'нет'}\n"
        f"- REGON: {company.regon or 'нет'}\n"
        f"- Адрес: {company.address or 'нет'}\n"
        f"- Статус в реестре: {company.status or 'неизвестен'}\n\n"
        + _facts_block(company)
        +
        "Ищи финансовые данные по официальным реквизитам, а не только по "
        f"названию: например запросами 'aleo.com KRS {company.krs}', "
        f"'rejestr.io {company.krs}', '{company.name} wyniki finansowe "
        "przychody'. Найденные страницы агрегаторов читай целиком.\n\n"
        "Затем собери отзывы сотрудников: найди профиль компании на "
        f"gowork.pl (например запросом 'gowork.pl {company.name} opinie') "
        f"и публичные упоминания на Reddit (например '{company.name} "
        "reddit praca opinie'). Учитывай адрес компании, чтобы не "
        "перепутать её с тёзками. Найденные страницы отзывов читай "
        "целиком."
    )


@dataclass
class HealthCheckResult:
    report_markdown: str
    evidence: dict


def run_health_check(company: CompanyCandidate) -> HealthCheckResult:
    agent = _get_agent()
    result = agent.invoke({"messages": [HumanMessage(content=_task_prompt(company))]})
    messages = result["messages"]
    report = message_content_to_text(messages[-1].content)

    evidence = {
        "schema": "toxic-scanner/health-check-evidence/v0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "company": company.model_dump(),
        "data_confidence": "secondary_sources",
        "agent_trace": _messages_to_trace(messages),
    }
    return HealthCheckResult(report_markdown=report, evidence=evidence)


def _messages_to_trace(messages: list) -> list[dict]:
    """Полный след работы агента: какие запросы делал и что получил."""
    trace: list[dict] = []
    for message in messages:
        entry: dict = {
            "type": getattr(message, "type", message.__class__.__name__),
            "content": message_content_to_text(message.content),
        }
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            entry["tool_calls"] = [
                {"name": call.get("name"), "args": call.get("args")}
                for call in tool_calls
            ]
        if getattr(message, "name", None):
            entry["tool_name"] = message.name
        trace.append(entry)
    return trace
