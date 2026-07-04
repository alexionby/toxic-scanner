import asyncio
import json
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_tavily import TavilySearch
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

load_dotenv()

# Инициализация FastAPI
app = FastAPI(title="Toxic Scanner API")
REPORTS_DIR = Path("reports")
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Схема входящего JSON-запроса
class CompanyRequest(BaseModel):
    company_name: str
    include_report: bool = False


def slugify_filename(value: str) -> str:
    slug_chars: list[str] = []
    for char in value.lower():
        if char.isalnum():
            slug_chars.append(char)
        elif slug_chars and slug_chars[-1] != "-":
            slug_chars.append("-")

    slug = "".join(slug_chars).strip("-")
    return (slug or "company")[:80]


def save_report(company_name: str, report: str) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = REPORTS_DIR / f"{timestamp}-{slugify_filename(company_name)}.md"
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    report_path.write_text(
        f"# Отчет по компании: {company_name}\n\n"
        f"Дата создания: {created_at}\n\n"
        f"{report}\n",
        encoding="utf-8",
    )
    return report_path


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


# --- ИНСТРУМЕНТЫ АГЕНТА ---

# 1. Поиск (Tavily)
search_tool = TavilySearch(max_results=3)


# 2. Чтение сайтов (Jina Reader)
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


tools = [search_tool, extract_website_text]

# --- НАСТРОЙКА LLM И АГЕНТА ---

# Используем Gemini Flash Lite (быстрая и дешевая модель)
llm = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite", temperature=1.5)

system_prompt = """
Ты — старший аналитик корпоративной прозрачности.
Используй поиск и чтение сайтов, чтобы собрать досье на работодателя.
Формат ответа:
- 📊 Индекс токсичности (0-100%)
- 🚩 Главные проблемы (Красные флаги)
- 🏆 Плюсы
- 🤖 Подозрительные отзывы (Вероятность накрутки HR)
"""

# Создаем агента, который умеет сам вызывать инструменты
agent_executor = create_react_agent(llm, tools, prompt=system_prompt)

# --- ЭНДПОИНТ ---


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/analyze")
async def analyze_company_endpoint(
    company_request: CompanyRequest, http_request: Request
):
    try:
        # Формируем задачу для агента
        messages = {
            "messages": [
                HumanMessage(
                    content=f"Собери отзывы о компании {company_request.company_name} (Польша)"
                )
            ]
        }

        # Агент уходит думать, искать и читать сайты
        result = await asyncio.to_thread(agent_executor.invoke, messages)

        # Забираем финальный текст ответа
        final_answer = message_content_to_text(result["messages"][-1].content)
        report_path = save_report(company_request.company_name, final_answer)
        report_url = str(http_request.url_for("get_report", filename=report_path.name))

        response = {
            "company": company_request.company_name,
            "status": "success",
            "report_file": str(report_path),
            "report_url": report_url,
            "report_preview": final_answer[:500],
        }
        if company_request.include_report:
            response["report"] = final_answer

        return response
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/reports/{filename}", name="get_report")
async def get_report(filename: str):
    reports_dir = REPORTS_DIR.resolve()
    report_path = (REPORTS_DIR / filename).resolve()

    if report_path.parent != reports_dir or not report_path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")

    return FileResponse(
        report_path,
        media_type="text/markdown",
        filename=filename,
        content_disposition_type="inline",
    )
