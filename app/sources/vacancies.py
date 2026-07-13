"""Адаптер вакансий: активные объявления pracuj.pl через живой fetch Jina.

Tavily (поисковый индекс с лагом) отдавал архивные оферты как активные -
проверено 0/2 актуальных (см. память проекта). Заменён на Jina Reader,
который читает ЖИВУЮ страницу поиска pracuj.pl в момент запроса: только
открытые оферты, с настоящей датой публикации, работодателем и его ID
профиля. Jina - сам фетчер (до pracuj.pl идёт её IP), поэтому прод
(Cloud Run) и дев ведут себя одинаково, прод-IP-бан тут не при чём.

Разметку Jina (markdown) разбираем по якорям:
- `## [Роль](.../oferta,{offerId})` - заголовок оферты (роль + id);
- `### [Работодатель](.../company/{empId}?pid={offerId})` - работодатель
  привязан к оферте через pid, совпадающий с offerId (надёжнее позиции);
- `#### Город` и `Opublikowana: {дата}` - по ближайшей позиции.

Фильтр тёзок - по имени работодателя (pracuj.pl отдаёт его явно): keyword
-поиск `;kw` матчит слово в тексте вакансии, поэтому по «varia» приходят
73 чужие оферты - все отсекаются несовпадением работодателя.

Юридика: как okredo/aleo - вторичное чтение публичной выдачи через
лицензированный ридер (pracuj.pl отдаёт чистый 200, не бан); ToS-cleanup
до монетизации, не красная линия.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import quote

import requests

# Транслитерация польских букв - та же таблица, что строит слаги
# агрегаторов; здесь ей нормализуем имена для сверки работодателя.
from app.sources.financials import _PL_TRANSLIT

logger = logging.getLogger(__name__)

JINA_BASE = "https://r.jina.ai"
REQUEST_TIMEOUT_SECONDS = 45
_FETCH_ATTEMPTS = 2  # Jina бывает флейки - одна повторная попытка

# Организационно-правовые формы срезаем с хвоста имени перед поиском и
# сверкой (в реестре «... SPÓŁKA Z OGRANICZONĄ...», на pracuj «... Sp. z o.o.»).
_LEGAL_FORMS = re.compile(
    r"\s+(spółka z ograniczoną odpowiedzialnością"
    r"|prosta spółka akcyjna"
    r"|spółka komandytowo-akcyjna"
    r"|spółka komandytowa"
    r"|spółka akcyjna"
    r"|spółka jawna"
    r"|spółka partnerska"
    r"|spółka cywilna"
    r"|sp\.?\s*z\s*o\.?\s*o\.?"
    r"|p\.?\s*s\.?\s*a\.?"
    r"|s\.?\s*k\.?\s*a\.?"
    r"|sp\.?\s*k\.?"
    r"|sp\.?\s*j\.?"
    r"|s\.?\s*c\.?"
    r"|s\.?\s*a\.?)\s*$",
    re.IGNORECASE,
)

# Маркеры бот-защиты/капчи в теле Jina (тот же приём, что extract_website_text).
_BOT_WALL_MARKERS = ("just a moment", "captcha", "enable javascript and cookies", "access denied")

# Родительный падеж польских месяцев из «Opublikowana: 12 lipca 2026».
_PL_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
    "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}

# Категории ролей по ключевым словам (детерминированно, стемы по-польски).
_BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("produkcja", ("monter", "operator", "spawacz", "produkcj", "ślusarz", "technolog", "drukarz", "pakowacz")),
    ("magazyn", ("magazyn",)),
    ("back-office", ("księgow", "fakturzyst", "rozliczen", "administracyj", "kadr", "asystent", "płac", "plac")),
    ("it", ("programist", "developer", "devops", "analityk", "tester", "informatyk")),
    ("sprzedaż", ("handlow", "sales", "przedstawiciel", "sprzedaż", "sprzedaz", "doradca klienta")),
    ("transport", ("kierowca", "spedytor", "kurier")),
]

# Якоря разметки Jina.
_OFFER_RE = re.compile(
    r"^## \[(?P<title>[^\]]+)\]\((?P<url>https://www\.pracuj\.pl/praca/[^)]*oferta,(?P<id>\d+)[^)]*)\)",
    re.MULTILINE,
)
_EMPLOYER_RE = re.compile(
    r"### \[(?P<name>[^\]]+)\]\(https://pracodawcy\.pracuj\.pl/company/(?P<empid>\d+)\?pid=(?P<pid>\d+)"
)
_DATE_RE = re.compile(r"Opublikowana:\s*([^\n]+)")
_CITY_RE = re.compile(r"^#### (.+)$", re.MULTILINE)


@dataclass
class Vacancy:
    title: str  # роль
    city: str | None
    employer: str | None  # работодатель, как подписан на pracuj.pl
    employer_id: str | None  # ID профиля работодателя на pracuj.pl
    url: str
    published: str | None = None  # дата публикации (ISO или сырая, если не разобрать)


@dataclass
class JobsResult:
    company_name: str
    ok: bool = True
    vacancies: list[Vacancy] = field(default_factory=list)
    buckets: dict[str, int] = field(default_factory=dict)
    rejected_namesakes: int = 0  # оферты, отсеянные по несовпадению работодателя
    source_url: str | None = None  # страница поиска pracuj.pl (для evidence)
    notes: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.vacancies)


def search_name(full_name: str) -> str:
    """Имя для поиска/сверки: юрформы и реестровые кавычки долой."""
    name = re.sub(r'["„”«»]', " ", full_name)
    name = " ".join(name.split()).rstrip(" .,")
    while True:
        cut = _LEGAL_FORMS.sub("", name).rstrip(" .,")
        if cut == name or not cut:
            break
        name = cut
    return name or full_name


def _norm(text: str) -> str:
    lowered = text.lower().translate(_PL_TRANSLIT)
    return " ".join(lowered.replace(",", " ").replace(".", " ").split())


def _same_employer(employer: str | None, sname: str) -> bool:
    """Работодатель оферты - наша компания? (pracuj отдаёт полное имя).

    Двустороннее вхождение нормализованных имён; короткое имя работодателя
    (огрызок) не должно ложно входить в наше - порог длины.
    """
    if not employer:
        return False
    a, b = _norm(search_name(employer)), _norm(sname)
    if not a or not b:
        return False
    if b in a:
        return True
    return a in b and len(a) >= 4 and len(a) * 2 >= len(b)


def _bucket(role: str) -> str:
    low = role.lower()
    for name, keys in _BUCKETS:
        if any(k in low for k in keys):
            return name
    return "inne"


def _to_iso(pl_date: str) -> str:
    """«12 lipca 2026» -> «2026-07-12»; если не разобрать - сырая строка."""
    m = re.match(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", pl_date.strip().lower())
    if not m:
        return pl_date.strip()
    month = _PL_MONTHS.get(m.group(2))
    if not month:
        return pl_date.strip()
    return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}"


def _parse_offers(markdown: str) -> list[Vacancy]:
    """Все оферты из живой выдачи pracuj.pl (без фильтра тёзок)."""
    # Работодатель по offerId через pid - надёжнее позиционной привязки.
    emp_by_offer = {
        m.group("pid"): (m.group("name").strip(), m.group("empid"))
        for m in _EMPLOYER_RE.finditer(markdown)
    }
    dates = [(m.start(), m.group(1)) for m in _DATE_RE.finditer(markdown)]
    cities = [(m.start(), m.group(1).strip()) for m in _CITY_RE.finditer(markdown)]
    headings = list(_OFFER_RE.finditer(markdown))

    offers: list[Vacancy] = []
    for i, m in enumerate(headings):
        pos = m.start()
        next_pos = headings[i + 1].start() if i + 1 < len(headings) else len(markdown)
        offer_id = m.group("id")
        emp = emp_by_offer.get(offer_id)
        # дата - ближайшая ПЕРЕД заголовком (у Jina она стоит над `##`)
        before = [v for p, v in dates if p < pos]
        published = _to_iso(before[-1]) if before else None
        # город - первый `####` ПОСЛЕ заголовка в пределах блока оферты
        within = [v for p, v in cities if pos < p < next_pos]
        offers.append(
            Vacancy(
                title=m.group("title").strip(),
                city=within[0] if within else None,
                employer=emp[0] if emp else None,
                employer_id=emp[1] if emp else None,
                url=m.group("url"),
                published=published,
            )
        )
    return offers


def _fetch_pracuj(search_url: str) -> str | None:
    """Живая выдача pracuj.pl через Jina; None - сбой/блок (не пустота)."""
    for attempt in range(_FETCH_ATTEMPTS):
        try:
            response = requests.get(
                f"{JINA_BASE}/{search_url}",
                headers={"Accept": "text/markdown"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException:
            logger.warning("jina fetch failed (attempt %d)", attempt + 1, exc_info=True)
            continue
        if response.status_code != 200 or len(response.text) < 500:
            logger.warning("jina bad response: HTTP %s len=%d", response.status_code, len(response.text))
            continue
        head = response.text[:3000].lower()
        if any(marker in head for marker in _BOT_WALL_MARKERS):
            logger.warning("pracuj.pl bot-wall via jina")
            return None
        return response.text
    return None


def get_jobs(company_name: str, address: str | None = None) -> JobsResult:
    """Активные вакансии компании из живой выдачи pracuj.pl (через Jina).

    Контракт sources: наружу не бросает никогда.
    """
    result = JobsResult(company_name=company_name)
    try:
        return _collect(result, company_name, address)
    except Exception:
        logger.exception("vacancies adapter crashed for %r", company_name)
        result.ok = False
        result.notes.append("сбой адаптера вакансий (детали в логах)")
        return result


def _collect(result: JobsResult, company_name: str, address: str | None) -> JobsResult:
    sname = search_name(company_name)
    result.notes.append(f"поисковое имя: {sname}")
    search_url = f"https://www.pracuj.pl/praca/{quote(sname.lower())};kw"
    result.source_url = search_url

    markdown = _fetch_pracuj(search_url)
    if markdown is None:
        result.ok = False
        result.notes.append("живая выдача pracuj.pl недоступна (сбой Jina/бот-защита)")
        return result

    all_offers = _parse_offers(markdown)
    # Фильтр тёзок: оставляем только оферты нашего работодателя.
    for vac in all_offers:
        if _same_employer(vac.employer, sname):
            result.vacancies.append(vac)
        else:
            result.rejected_namesakes += 1

    result.buckets = dict(Counter(_bucket(v.title) for v in result.vacancies))

    # Мягкий сигнал (не фильтр): все оферты в другом городе, чем адрес -
    # возможен филиал или всё же тёзка с тем же именем.
    if result.vacancies and address:
        addr = _norm(address)
        if not any(v.city and _norm(re.sub(r"\(.*?\)", "", v.city)) in addr for v in result.vacancies):
            result.notes.append(
                "ни один город вакансий не совпал с адресом компании - "
                "возможен филиал или тёзка"
            )
    if result.rejected_namesakes:
        result.notes.append(
            f"отсеяно оферт по чужому работодателю: {result.rejected_namesakes}"
        )
    if not result.has_data:
        result.notes.append("активных вакансий на pracuj.pl не найдено")
    return result


if __name__ == "__main__":
    # Ручной прогон: uv run python -m app.sources.vacancies 0000475078
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    cli = argparse.ArgumentParser(description="Прогон адаптера вакансий по KRS")
    cli.add_argument("krs", help="KRS компании (ведущие нули можно опустить)")
    args = cli.parse_args()

    from app.companies import resolve_company
    from app.models import CompanyQuery

    candidates = resolve_company(CompanyQuery(krs=args.krs.zfill(10)))
    if not candidates:
        raise SystemExit(f"KRS {args.krs} не найден в реестре")
    company = candidates[0]
    print(f"{company.name} | {company.address}")

    jobs = get_jobs(company.name, company.address)
    print(f"ok={jobs.ok} vacancies={len(jobs.vacancies)} rejected={jobs.rejected_namesakes}")
    for vac in jobs.vacancies:
        print(f"  {vac.published or '????-??-??'} | {vac.title} | {vac.city or 'город?'} | {vac.employer}")
        print(f"       {vac.url}")
    print("buckets:", jobs.buckets)
    for note in jobs.notes:
        print("note:", note)
