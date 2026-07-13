"""Адаптер вакансий: активные объявления компании из поисковой выдачи.

Вакансии - ключевой сигнал секции «Текущие инициативы», но трейсы
показали, что LLM-агент делает один поиск, не открывает найденные
списки и теряет свежие объявления (детали - PLAN-jobs-source.md).
Поэтому собираем детерминированно ДО агента, как financials.py и
crbr.py, и отдаём готовым блоком фактов.

Источник - выдача Tavily (лицензированный поисковый API), сайты
вакансий не скрейпим: карточки pracuj.pl под ботозащитой (HTTP 403),
но заголовки выдачи стабильного формата «Oferta pracy {роль},
{компания}, {город}» отдают всё нужное без открытия страницы
(проверено руками, 07.2026). Tavily обрезает длинные заголовки
многоточием - разбор явно учитывает, какой хвост потерян.

Свежесть: дат публикации в выдаче нет, вместо них два маркера:
- живость pracuj.pl - закрытые объявления снимаются и выпадают из
  индекса, карточка в выдаче почти наверняка открыта сейчас;
- запрос с time_range="month" - результат индексирован за последний
  месяц.
Оба дают fresh=True; даты не выдумываем (published=None, если не видна).

Каналы ошибок langchain_tavily неочевидны (сверено с исходниками):
пустая выдача возвращается СТРОКОЙ (ToolException, проглоченный
BaseTool), а реальные сбои API - словарём {"error": ...} без
исключения. _tavily_hits разводит их явно: строка = легитимная
пустота, error-словарь = сбой источника.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse

from langchain_tavily import TavilySearch

# Транслитерация польских букв - та же таблица, что строит слаги
# агрегаторов; здесь ей сверяем города из слагов/выдачи с адресом KRS.
from app.sources.financials import _PL_TRANSLIT

logger = logging.getLogger(__name__)

_MAX_RESULTS_PER_QUERY = 10
# requests внутри langchain_tavily идёт БЕЗ таймаута - ограничиваем
# ожидание сами, иначе один зависший сокет вешает весь отчёт.
_SEARCH_TIMEOUT_SECONDS = 25
# Домен, чьему формату заголовков доверяем и чей индекс чистится от
# закрытых объявлений (живость = свежесть). Поддомены (it.pracuj.pl)
# считаются тем же доменом - см. _is_trusted_host.
_TRUSTED_DOMAIN = "pracuj.pl"

# В реестре имя юридическое полное, а вакансии работодатель подписывает
# коротко ('MPSystem Sp. z o.o.' или просто 'MPSYSTEM') - точная фраза
# с полной формой не находит ничего. Формы срезаем с хвоста итеративно:
# бывают составные ('... SP. Z O.O. SP.K.'). Родственный, но другой по
# задаче список - companies._LEGAL_FORM_TOKENS (фильтрация токенов для
# fuzzy-сравнения); при третьем потребителе выносить в общий модуль.
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

# Маркер юрлица ВНУТРИ сегмента заголовка: если обрезанный заголовок
# кончается компанией («..., MPSystem Sp. z o.o. ...»), город съеден.
_COMPANY_MARKER = re.compile(
    r"spółka|sp\.?\s*z\s*o|sp\.?\s*[jk]\b|s\.?\s*a\.?\s*$", re.IGNORECASE
)

# Хвостовое многоточие - след обрезки заголовка Tavily.
_ELLIPSIS_TAIL = re.compile(r"(?:\.\.\.|…)\s*$")

# Категории ролей по ключевым словам (детерминированно, стемы по-польски).
_BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("produkcja", ("monter", "operator", "spawacz", "produkcj", "ślusarz", "technolog", "drukarz", "pakowacz")),
    ("magazyn", ("magazyn",)),
    ("back-office", ("księgow", "fakturzyst", "rozliczen", "administracyj", "kadr", "asystent")),
    ("it", ("programist", "developer", "devops", "analityk", "tester", "informatyk")),
    ("sprzedaż", ("handlow", "sales", "przedstawiciel", "sprzedaż", "sprzedaz", "doradca klienta")),
    ("transport", ("kierowca", "spedytor", "kurier")),
]


@dataclass
class Vacancy:
    title: str  # роль
    city: str | None
    company: str  # работодатель, как подписан в заголовке выдачи
    source: str  # домен источника
    url: str
    published: str | None = None
    fresh: bool = False


@dataclass
class JobsResult:
    company_name: str
    ok: bool = True
    vacancies: list[Vacancy] = field(default_factory=list)
    # Имя работодателя наше, но город либо не совпал с адресом компании,
    # либо неизвестен (заголовок обрезан): филиал или тёзка - для агента
    # «под вопросом», не подтверждённый факт и не мусор.
    unconfirmed: list[Vacancy] = field(default_factory=list)
    buckets: dict[str, int] = field(default_factory=dict)
    rejected_namesakes: int = 0
    queries: list[str] = field(default_factory=list)  # прозрачность в evidence
    notes: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.vacancies or self.unconfirmed)


def search_name(full_name: str) -> str:
    """Имя для поисковых запросов: юрформы и реестровые кавычки долой.

    Кавычки ('PPH "MARTEX" SP. Z O.O.') сломали бы и фразовые запросы
    f'"{name}"', и сверку с подписью работодателя в заголовке.
    """
    name = re.sub(r'["„”«»]', " ", full_name)
    name = " ".join(name.split()).rstrip(" .,")
    while True:
        cut = _LEGAL_FORMS.sub("", name).rstrip(" .,")
        if cut == name or not cut:
            break
        name = cut
    return name or full_name


def _norm(text: str) -> str:
    """Нормализация для сверок: как _norm_address в financial_health, плюс
    транслит - выдача пишет города и по-польски, и слагами без диакритики
    ('OSTRÓWEK' vs 'ostrowek')."""
    lowered = text.lower().translate(_PL_TRANSLIT)
    return " ".join(lowered.replace(",", " ").replace(".", " ").split())


def _is_trusted_host(host: str) -> bool:
    return host == _TRUSTED_DOMAIN or host.endswith("." + _TRUSTED_DOMAIN)


def _clean_city(raw: str) -> str:
    """Город из заголовка выдачи, который Tavily обрезает многоточием.

    'Ostrówek (pow. węgrowski)' / 'Ostrówek ... - Pracuj.pl' /
    'Warszawa - Pracuj…' / 'Ostrówek (pow ...' -> чистое имя города.
    """
    city = raw.replace("…", " ").replace("...", " ")
    # брендовый трейлер выдачи, в т.ч. оборванный ('- Pracuj')
    city = re.sub(r"\s*[-|]\s*Pracuj(\.pl)?\s*$", "", city, flags=re.IGNORECASE)
    city = re.sub(r"\(.*$", "", city)  # скобка, в т.ч. оборванная обрезкой
    return " ".join(city.split()).strip(" ,.")


def _city_status(city: str | None, address: str | None) -> str:
    """matched | remote | mismatch | unknown - сверка города с адресом."""
    if not city:
        return "unknown"
    if "zdaln" in city.lower():  # praca zdalna - не тёзка по определению
        return "remote"
    if not address:
        return "unknown"
    base = _norm(city)
    return "matched" if base and base in _norm(address) else "mismatch"


def _city_from_url(url: str, address: str | None) -> str | None:
    """Город из слага pracuj.pl - только как ПОДТВЕРЖДЕНИЕ адресного.

    Слаг роли и города слиты дефисами ('...-placowych-warszawa,oferta,'),
    надёжно отделить город нельзя - поэтому сверяем хвостовые сегменты
    слага с адресом компании и возвращаем город только при совпадении.
    Отклонять («другой город») по слагу нельзя - хвост может быть ролью.
    """
    if not address:
        return None
    m = re.search(r"/praca/([a-z0-9ąćęłńóśźż-]+),oferta", url.lower())
    if not m:
        return None
    tail_segments = m.group(1).rsplit("-", 3)[-3:]
    addr = _norm(address)
    for n in (3, 2, 1):  # 'nowy dwor mazowiecki', 'zielona gora', 'warszawa'
        tail = " ".join(tail_segments[-n:])
        if tail and tail in addr:
            return tail.title()
    return None


def _same_company(hit_company: str, sname: str) -> bool:
    """Грубый матч работодателя из заголовка с нашим поисковым именем.

    Ловит тёзок с ДРУГИМ именем; тёзку с тем же именем в другом городе
    ловит _city_status, а с тем же именем и скрытым городом - карантин
    unconfirmed.
    """
    a, b = _norm(hit_company), _norm(sname)
    if not a or not b:
        return False
    if b in a:  # наше имя внутри подписи работодателя - надёжно
        return True
    # Обратное вхождение (подпись внутри нашего имени) - только если
    # подпись не огрызок: обрезка Tavily оставляет 'TK' от 'TK MAXX',
    # и он ложно входит в 'TK FOOD'.
    return a in b and len(a) >= 4 and len(a) * 2 >= len(b)


def _bucket(role: str) -> str:
    low = role.lower()
    for name, keys in _BUCKETS:
        if any(k in low for k in keys):
            return name
    return "inne"


def _parse_hit(title: str, url: str, sname: str) -> Vacancy | None:
    """Вакансия из заголовка выдачи; None - профиль работодателя/шум.

    Обрезка Tavily делает последний сегмент ненадёжным, поэтому маркер
    обрезки выбирает разбор, а не служит fallback'ом: если обрезанный
    заголовок кончается компанией (юрформа или наше имя в сегменте) -
    города нет; иначе последний сегмент - оборванный город.
    """
    stripped = title.strip()
    low = stripped.lower()
    if not low.startswith("oferta pracy"):
        return None
    domain = urlparse(url).netloc.removeprefix("www.").lower()
    truncated = bool(_ELLIPSIS_TAIL.search(stripped))
    body = _ELLIPSIS_TAIL.sub("", stripped)[len("Oferta pracy"):].strip()
    segments = [s.strip() for s in body.split(",")]
    if len(segments) < 2:
        return None

    role = company = None
    city: str | None = None
    if truncated:
        last = segments[-1]
        ends_with_company = bool(_COMPANY_MARKER.search(last)) or (
            _norm(sname) and _norm(sname) in _norm(last)
        )
        if ends_with_company or len(segments) == 2:
            role = ", ".join(segments[:-1])
            company = last
        else:
            role = ", ".join(segments[:-2])
            company = segments[-2]
            city = _clean_city(segments[-1]) or None
    else:
        if len(segments) < 3:
            return None  # полный формат - всегда «роль, компания, город»
        role = ", ".join(segments[:-2])
        company = segments[-2]
        city = _clean_city(segments[-1]) or None

    role = role.strip()
    if not role or not company:
        return None
    return Vacancy(title=role, city=city, company=company, source=domain, url=url)


def _tavily_hits(tool: TavilySearch, query: str) -> list[dict] | None:
    """Результаты запроса; [] - легитимная пустота, None - сбой источника.

    См. докстринг модуля: пустая выдача приходит строкой, ошибки API -
    словарём {'error': ...} без исключения.
    """
    raw = tool.invoke({"query": query})
    if isinstance(raw, str):
        return []
    if not isinstance(raw, dict) or raw.get("error"):
        logger.warning("tavily error for %r: %s", query, raw)
        return None
    return raw.get("results", [])


def get_jobs(company_name: str, address: str | None) -> JobsResult:
    """Активные вакансии компании из поисковой выдачи, с фильтром тёзок."""
    sname = search_name(company_name)
    result = JobsResult(company_name=company_name)
    result.notes.append(f"поисковое имя: {sname}")

    # Инстансы Tavily создаём внутри функции, не на уровне модуля (порядок
    # load_dotenv - см. web_search.py). Конструктор читает TAVILY_API_KEY и
    # может бросить - адаптер наружу не бросает никогда (контракт sources).
    try:
        plans = [
            ("general", TavilySearch(max_results=_MAX_RESULTS_PER_QUERY),
             f'"{sname}" praca oferty', False),
            ("pracuj.pl", TavilySearch(max_results=_MAX_RESULTS_PER_QUERY,
                                       include_domains=[_TRUSTED_DOMAIN]),
             f'"{sname}" oferty pracy', False),  # свежесть даст сам домен
            ("fresh-month", TavilySearch(max_results=_MAX_RESULTS_PER_QUERY,
                                         time_range="month"),
             f'"{sname}" praca', True),
        ]
    except Exception:
        logger.warning("tavily init failed", exc_info=True)
        result.ok = False
        result.notes.append("поиск вакансий недоступен (нет TAVILY_API_KEY?)")
        return result

    parsed: dict[tuple[str, str], Vacancy] = {}  # дедуп по (роль, город)
    unparsed: list[str] = []
    failures = 0
    # Запросы независимы - параллелим (тот же приём, что run_health_check).
    pool = ThreadPoolExecutor(max_workers=len(plans))
    try:
        futures = [
            pool.submit(_tavily_hits, tool, query) for _, tool, query, _ in plans
        ]
        for (label, _tool, query, marks_fresh), future in zip(plans, futures):
            try:
                hits = future.result(timeout=_SEARCH_TIMEOUT_SECONDS)
            except Exception:
                logger.warning("vacancy search '%s' failed", label, exc_info=True)
                hits = None
            if hits is None:
                result.notes.append(f"поиск '{label}' не удался")
                failures += 1
                continue
            result.queries.append(f"{label}: {query}")
            for item in hits:
                title = (item.get("title") or "").strip()
                url = item.get("url") or ""
                vacancy = _parse_hit(title, url, sname)
                if vacancy is None:
                    if title:
                        unparsed.append(title[:90])
                    continue
                if vacancy.city is None and _is_trusted_host(vacancy.source):
                    vacancy.city = _city_from_url(url, address)
                # Свежесть: индексировано за месяц ИЛИ живая карточка pracuj.
                fresh = marks_fresh or _is_trusted_host(vacancy.source)
                key = (vacancy.title.lower(), (vacancy.city or "").lower())
                if key in parsed:
                    parsed[key].fresh = parsed[key].fresh or fresh
                else:
                    vacancy.fresh = fresh
                    parsed[key] = vacancy
    finally:
        # Повисший вызов не ждём: поток догорит в фоне (у requests внутри
        # langchain_tavily нет таймаута) - лучше, чем повесить отчёт.
        pool.shutdown(wait=False, cancel_futures=True)

    if failures == len(plans):
        result.ok = False
        return result

    # Один и тот же оффер мог прийти полным заголовком (с городом) и
    # обрезанным (без) - склеиваем безгородный вариант, если городская
    # версия ровно одна (при нескольких слить не во что однозначно).
    by_title: dict[str, list[Vacancy]] = {}
    for vacancy in parsed.values():
        by_title.setdefault(vacancy.title.lower(), []).append(vacancy)

    rejected_examples: list[str] = []
    for group in by_title.values():
        with_city = [v for v in group if v.city]
        cityless = [v for v in group if not v.city]
        if cityless and len(with_city) == 1:
            with_city[0].fresh = with_city[0].fresh or any(v.fresh for v in cityless)
            cityless = []
        for vacancy in with_city + cityless:
            if not _same_company(vacancy.company, sname):
                result.rejected_namesakes += 1
                if len(rejected_examples) < 3:
                    rejected_examples.append(f"{vacancy.company} - {vacancy.title}")
                continue
            if _city_status(vacancy.city, address) in ("matched", "remote"):
                result.vacancies.append(vacancy)
            else:
                # mismatch или город неизвестен: тёзку с тем же именем и
                # скрытым городом не отличить - только «под вопросом».
                result.unconfirmed.append(vacancy)

    result.buckets = dict(Counter(_bucket(v.title) for v in result.vacancies))

    if result.rejected_namesakes:
        result.notes.append(
            f"отсечено тёзок: {result.rejected_namesakes}, "
            f"например: {'; '.join(rejected_examples)}"
        )
    if unparsed:
        # Профили работодателей и агрегаторы-архивы не матчатся - это
        # норма, но не теряем молча: счётчик и примеры для отладки.
        unique = list(dict.fromkeys(unparsed))
        result.notes.append(
            f"нераспознанных заголовков выдачи: {len(unique)}, "
            f"например: {'; '.join(unique[:3])}"
        )
    if not result.has_data:
        result.notes.append("активных вакансий в выдаче не найдено")
    return result


if __name__ == "__main__":
    # Ручной прогон на реальной компании (3 кредита Tavily за запуск):
    #   uv run python -m app.sources.vacancies 0000475078
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    cli = argparse.ArgumentParser(description="Прогон адаптера вакансий по KRS")
    cli.add_argument("krs", help="KRS компании (ведущие нули можно опустить)")
    args = cli.parse_args()

    # Импорт здесь, а не в шапке: как библиотека модуль от app.companies
    # не зависит и не должен тянуть его при импорте из financial_health.
    from app.companies import resolve_company
    from app.models import CompanyQuery

    candidates = resolve_company(CompanyQuery(krs=args.krs.zfill(10)))
    if not candidates:
        raise SystemExit(f"KRS {args.krs} не найден в реестре")
    company = candidates[0]
    print(f"{company.name} | {company.address}")

    jobs = get_jobs(company.name, company.address)
    print(
        f"ok={jobs.ok} vacancies={len(jobs.vacancies)} "
        f"unconfirmed={len(jobs.unconfirmed)} rejected={jobs.rejected_namesakes}"
    )
    for vac in jobs.vacancies:
        marker = "F" if vac.fresh else " "
        print(f"  [{marker}] {vac.title} | {vac.city or 'город?'} | {vac.url}")
    for vac in jobs.unconfirmed:
        print(f"  ? {vac.title} | {vac.city or 'город?'} | {vac.company} | {vac.url}")
    print("buckets:", jobs.buckets)
    for note in jobs.notes:
        print("note:", note)
