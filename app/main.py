import asyncio
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_tavily import TavilySearch
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

from app import financial_health
from app.companies import resolve_company
from app.evidence import REPORTS_DIR, save_evidence_json, save_markdown_report
from app.financial_health import extract_website_text, message_content_to_text
from app.models import CompanyQuery, CompanySearchResponse

load_dotenv()

# Инициализация FastAPI
app = FastAPI(title="Toxic Scanner API")
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Схема входящего JSON-запроса
class CompanyRequest(BaseModel):
    company_name: str
    include_report: bool = False


# --- СТАРЫЙ ОБЩИЙ АНАЛИЗАТОР (/analyze) ---
# Прототип: агент сам гуляет по интернету по одному названию. Постепенно
# заменяется цепочкой resolver -> health-check; пока остаётся как есть.

search_tool = TavilySearch(max_results=3)
tools = [search_tool, extract_website_text]

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

# --- ЭНДПОИНТЫ ---


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
        report_path = save_markdown_report(company_request.company_name, final_answer)
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


@app.post("/companies/search", response_model=CompanySearchResponse)
async def companies_search_endpoint(query: CompanyQuery) -> CompanySearchResponse:
    candidates = await asyncio.to_thread(resolve_company, query)
    return CompanySearchResponse(candidates=candidates)


@app.post("/companies/{krs}/health-check")
async def company_health_check_endpoint(krs: str, http_request: Request):
    candidates = await asyncio.to_thread(resolve_company, CompanyQuery(krs=krs))
    if not candidates:
        raise HTTPException(
            status_code=404, detail=f"Компания с KRS {krs} не найдена в реестре"
        )
    company = candidates[0]

    try:
        result = await asyncio.to_thread(financial_health.run_health_check, company)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    report_path = save_markdown_report(company.name, result.report_markdown)
    evidence_path = save_evidence_json(company.name, result.evidence)

    return {
        "company": company.model_dump(),
        "status": "success",
        "report": result.report_markdown,
        "report_file": str(report_path),
        "report_url": str(http_request.url_for("get_report", filename=report_path.name)),
        "evidence_file": str(evidence_path),
        "evidence_url": str(
            http_request.url_for("get_report", filename=evidence_path.name)
        ),
    }


@app.get("/reports/{filename}", name="get_report")
async def get_report(filename: str):
    reports_dir = REPORTS_DIR.resolve()
    report_path = (REPORTS_DIR / filename).resolve()

    if report_path.parent != reports_dir or not report_path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")

    media_type = (
        "application/json" if report_path.suffix == ".json" else "text/markdown"
    )
    return FileResponse(
        report_path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline",
    )
