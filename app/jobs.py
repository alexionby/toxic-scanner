"""Джобы построения отчёта: очередь + воркеры внутри процесса.

Ран агента занимает минуты и не должен жить внутри HTTP-запроса:
запрос создаёт джобу и мгновенно возвращает квитанцию, фоновый воркер
исполняет ран, клиент опрашивает статус. Контракт (202 + job_id +
поллинг) — обычный продакшеновский; транспорт очереди — осознанная
заглушка в памяти процесса: инстанс один, а замена asyncio.Queue на
Redis/Arq не потребует менять API.

Из этого же следуют пределы: рестарт процесса теряет джобы (клиент
получит 404 на поллинге), второй инстанс их не увидит.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from app import financial_health
from app.evidence import save_evidence_json, save_markdown_report
from app.models import CompanyCandidate
from app.telemetry import track

# Число воркеров = потолок одновременных ранов агента. Каждый ран жжёт
# деньги (Gemini + Tavily) и внешние rate-limit'ы, поэтому параллелизм
# ограничен явно: лишние джобы честно ждут в очереди, а не стартуют разом.
WORKER_COUNT = int(os.environ.get("JOB_WORKERS", "2"))
# Завершённые джобы держим, пока клиент может за ними вернуться
# (вкладка в фоне, поллинг с телефона после разрыва сети), потом чистим —
# иначе стор в памяти растёт бесконечно.
FINISHED_JOB_TTL = timedelta(hours=int(os.environ.get("JOB_TTL_HOURS", "6")))


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def finished(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED)


@dataclass
class Job:
    id: str
    company: CompanyCandidate
    # PostHog-идентити захвачен при создании: у воркера нет Request,
    # из которого его можно было бы вычислить.
    distinct_id: str
    status: JobStatus = JobStatus.QUEUED
    created_at: datetime = field(default_factory=datetime.now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Воркер кладёт сюда данные (markdown, scores, имена файлов);
    # URL-ы из имён файлов строит эндпоинт статуса — базовый адрес
    # сервера известен только на edge.
    result: dict | None = None
    error: str | None = None


_jobs: dict[str, Job] = {}
_queue: asyncio.Queue[str] = asyncio.Queue()
_workers: list[asyncio.Task] = []


def _prune_finished() -> None:
    cutoff = datetime.now() - FINISHED_JOB_TTL
    expired = [
        job_id
        for job_id, job in _jobs.items()
        if job.status.finished and job.finished_at and job.finished_at < cutoff
    ]
    for job_id in expired:
        del _jobs[job_id]


def submit_job(company: CompanyCandidate, distinct_id: str) -> Job:
    _prune_finished()
    job = Job(id=uuid.uuid4().hex, company=company, distinct_id=distinct_id)
    _jobs[job.id] = job
    _queue.put_nowait(job.id)
    return job


def get_job(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def _run_job(job: Job) -> dict:
    """Синхронное тело джобы: ран агента + сохранение артефактов.

    Выполняется в треде (run_health_check блокирующий). Возвращает
    данные результата; статусами управляет воркер.
    """
    result = financial_health.run_health_check(job.company)

    report_path = save_markdown_report(job.company.name, result.report_markdown)
    evidence_path = save_evidence_json(job.company.name, result.evidence)

    # Плоские числа стоимости рана - по ним в PostHog строится тренд
    # "сколько ест один отчёт" (токены -> прайс Gemini, поиски -> кредиты Tavily).
    stats = result.evidence.get("agent_stats", {})
    tool_calls = stats.get("tool_calls", {})
    # Кредиты Tavily = агентские web_search + детерминированные запросы
    # адаптера вакансий (успешно выполненные, они и тарифицируются).
    vacancy_searches = len(result.evidence.get("jobs", {}).get("queries", []))
    track(
        "report_built",
        distinct_id=job.distinct_id,
        krs=job.company.krs,
        company=job.company.name,
        llm_calls=stats.get("llm_calls"),
        input_tokens=stats.get("input_tokens"),
        output_tokens=stats.get("output_tokens"),
        web_searches=tool_calls.get("web_search", 0),
        vacancy_searches=vacancy_searches,
        page_reads=tool_calls.get("extract_website_text", 0),
    )

    return {
        "company": job.company.model_dump(),
        "report": result.report_markdown,
        "scores": result.scores,
        "report_file": str(report_path),
        "report_filename": report_path.name,
        "evidence_file": str(evidence_path),
        "evidence_filename": evidence_path.name,
    }


async def _worker() -> None:
    while True:
        job_id = await _queue.get()
        job = _jobs.get(job_id)
        if job is None:
            # Вычищена TTL, пока лежала в очереди, - пропускаем.
            _queue.task_done()
            continue

        job.status = JobStatus.RUNNING
        job.started_at = datetime.now()
        try:
            job.result = await asyncio.to_thread(_run_job, job)
            job.status = JobStatus.SUCCEEDED
        except Exception as e:
            # Джоба падает - воркер живёт: ошибка становится статусом
            # для клиента, а не смертью потребителя очереди.
            job.error = str(e)
            job.status = JobStatus.FAILED
        finally:
            job.finished_at = datetime.now()
            _queue.task_done()


def start_workers() -> None:
    for _ in range(WORKER_COUNT):
        _workers.append(asyncio.create_task(_worker()))


async def stop_workers() -> None:
    for task in _workers:
        task.cancel()
    await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()
