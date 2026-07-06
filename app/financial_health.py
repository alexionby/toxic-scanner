"""Financial health check v0.1 (см. README 'Financial Health Check').

Официальные реквизиты приходят из Company Resolver и попадают в отчёт
как проверенные факты. Финансовые показатели LLM-агент собирает с
публичных агрегаторов (aleo.com, rejestr.io) - это вторичный источник,
и отчёт обязан помечать его как таковой. Первоисточник (Repozytorium
Dokumentów Finansowych, XML-парсинг) - в беклоге.
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
Ты - финансовый аналитик. Ты строишь financial health check польской
компании для человека, который решает, подписывать ли с ней контракт.

Правила:
- Юрлицо уже подтверждено по государственным реестрам, его реквизиты
  даны в задаче. Не меняй их и не "уточняй".
- Каждый факт сопровождай ссылкой на источник, из которого ты его взял.
- Финансовые цифры ищи на публичных агрегаторах (aleo.com, rejestr.io) -
  это вторичные источники: прямо помечай их в отчёте как
  "по данным агрегаторов, не первоисточник".
- Не выдумывай цифры. Если данных нет - пиши "данных нет" и учитывай
  это как отдельный риск-флаг, а не как нейтральный факт.
- Отделяй факты от предположений.

Структура отчёта (Markdown, на русском):
# Financial Health Check: <название компании>
- **Health score**: 0-100 с одним предложением обоснования
- **Идентификация**: KRS, NIP, REGON, адрес, статус (из задачи)
- **Краткий вывод**: 2-4 предложения
- **Финансовые показатели по годам**: таблица (выручка, прибыль/убыток,
  активы, капитал) со ссылками на источники; если данных нет - явно
  скажи об этом
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
        "Ищи финансовые данные по официальным реквизитам, а не только по "
        f"названию: например запросами 'aleo.com KRS {company.krs}', "
        f"'rejestr.io {company.krs}', '{company.name} wyniki finansowe "
        "przychody'. Найденные страницы агрегаторов читай целиком."
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
