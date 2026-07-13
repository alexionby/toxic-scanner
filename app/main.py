import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import jobs
from app.companies import resolve_company
from app.evidence import REPORTS_DIR
from app.models import CompanyQuery, CompanySearchResponse
from app.ratelimit import client_ip, enforce_report_quota
from app.telemetry import distinct_id_from_ip, record_waitlist_email, track

load_dotenv()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Воркеры джобов живут вместе с процессом: стартуют до первого
    # запроса, при остановке отменяются (in-flight раны обрываются -
    # стор в памяти всё равно не переживает рестарт).
    jobs.start_workers()
    yield
    await jobs.stop_workers()


# Инициализация FastAPI
app = FastAPI(title="Toxic Scanner API", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- ЭНДПОИНТЫ ---


@app.get("/healthz")
async def healthz():
    # Лёгкая проба живости для Cloud Run: без LLM и внешних вызовов.
    return {"status": "ok"}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/companies/search", response_model=CompanySearchResponse)
async def companies_search_endpoint(query: CompanyQuery) -> CompanySearchResponse:
    candidates = await asyncio.to_thread(resolve_company, query)
    return CompanySearchResponse(candidates=candidates)


@app.post("/companies/{krs}/health-check", status_code=202)
async def company_health_check_endpoint(
    krs: str, http_request: Request, _: None = Depends(enforce_report_quota)
):
    """Принять заявку на отчёт: вернуть квитанцию, не результат.

    Ран агента занимает минуты - в запросе делаем только быстрое
    (резолв компании, чтобы 404 по несуществующему KRS отдать сразу),
    сам ран уходит в очередь. 202 + Location - стандартный REST-ответ
    "принял, статус вон там".
    """
    candidates = await asyncio.to_thread(resolve_company, CompanyQuery(krs=krs))
    if not candidates:
        raise HTTPException(
            status_code=404, detail=f"Компания с KRS {krs} не найдена в реестре"
        )

    job = jobs.submit_job(
        candidates[0], distinct_id_from_ip(client_ip(http_request))
    )
    status_url = str(http_request.url_for("get_job", job_id=job.id))
    return JSONResponse(
        status_code=202,
        content={"job_id": job.id, "status": job.status, "status_url": status_url},
        headers={"Location": status_url},
    )


@app.get("/jobs/{job_id}", name="get_job")
async def get_job_endpoint(job_id: str, http_request: Request):
    job = jobs.get_job(job_id)
    if job is None:
        # Либо опечатка в id, либо процесс перезапустился и стор в
        # памяти опустел - для клиента это одно и то же "джобы нет".
        raise HTTPException(status_code=404, detail="Джоба не найдена")

    body: dict = {
        "job_id": job.id,
        "status": job.status,
        "company": job.company.name,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if job.status is jobs.JobStatus.FAILED:
        body["error"] = job.error
    if job.status is jobs.JobStatus.SUCCEEDED and job.result:
        # Воркер сохранил данные и имена файлов; URL-ы строим здесь -
        # базовый адрес сервера известен только в контексте запроса.
        body["result"] = {
            **{k: v for k, v in job.result.items() if not k.endswith("_filename")},
            "report_url": str(
                http_request.url_for(
                    "get_report", filename=job.result["report_filename"]
                )
            ),
            "evidence_url": str(
                http_request.url_for(
                    "get_report", filename=job.result["evidence_filename"]
                )
            ),
        }
    return body


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
