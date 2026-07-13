import asyncio
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import financial_health
from app.companies import resolve_company
from app.evidence import REPORTS_DIR, save_evidence_json, save_markdown_report
from app.models import CompanyQuery, CompanySearchResponse
from app.ratelimit import client_ip, enforce_report_quota
from app.telemetry import distinct_id_from_ip, record_waitlist_email, track

load_dotenv()

# Инициализация FastAPI
app = FastAPI(title="Toxic Scanner API")
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- ЭНДПОИНТЫ ---


@app.get("/health")
async def health():
    # Лёгкая проба живости для Cloud Run: без LLM и внешних вызовов.
    return {"status": "ok"}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/companies/search", response_model=CompanySearchResponse)
async def companies_search_endpoint(query: CompanyQuery) -> CompanySearchResponse:
    candidates = await asyncio.to_thread(resolve_company, query)
    return CompanySearchResponse(candidates=candidates)


@app.post("/companies/{krs}/health-check")
async def company_health_check_endpoint(
    krs: str, http_request: Request, _: None = Depends(enforce_report_quota)
):
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

    # Плоские числа стоимости рана - по ним в PostHog строится тренд
    # "сколько ест один отчёт" (токены -> прайс Gemini, поиски -> кредиты Tavily).
    stats = result.evidence.get("agent_stats", {})
    tool_calls = stats.get("tool_calls", {})
    track(
        "report_built",
        distinct_id=distinct_id_from_ip(client_ip(http_request)),
        krs=krs,
        company=company.name,
        llm_calls=stats.get("llm_calls"),
        input_tokens=stats.get("input_tokens"),
        output_tokens=stats.get("output_tokens"),
        web_searches=tool_calls.get("web_search", 0),
        page_reads=tool_calls.get("extract_website_text", 0),
    )

    return {
        "company": company.model_dump(),
        "status": "success",
        "report": result.report_markdown,
        "scores": result.scores,
        "report_file": str(report_path),
        "report_url": str(http_request.url_for("get_report", filename=report_path.name)),
        "evidence_file": str(evidence_path),
        "evidence_url": str(
            http_request.url_for("get_report", filename=evidence_path.name)
        ),
    }


class InterestSignal(BaseModel):
    # action: "pay_click" (нажал «оплатить») или "notify" (оставил email).
    action: str = "notify"
    email: str | None = None
    krs: str | None = None
    company: str | None = None


@app.post("/interest")
async def interest_endpoint(signal: InterestSignal, http_request: Request):
    """Фейк-дор: пользователь на пейволе нажал «оплатить» или оставил
    email. Это и есть замеритель готовности платить — пишем в событие.

    Сырой email в аналитику не идёт (только факт has_email); сам адрес —
    в выделенный вейтлист-сток.
    """
    distinct_id = distinct_id_from_ip(client_ip(http_request))
    track(
        "interest_submitted",
        distinct_id=distinct_id,
        action=signal.action,
        has_email=bool(signal.email),
        krs=signal.krs,
        company=signal.company,
    )
    if signal.email:
        record_waitlist_email(
            distinct_id=distinct_id,
            email=signal.email,
            krs=signal.krs,
            company=signal.company,
        )
    return {"ok": True}


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
