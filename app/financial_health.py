"""Company Pulse: что происходит в компании сейчас и что ей можно продать.

Пивот от «финансового здоровья» к sales intelligence: официальные
реквизиты и факты KRS/CRBR остаются фундаментом (идентификация, масштаб,
реорганизации, владельцы), но вопрос отчёта другой - не «насколько
компания здорова», а «что в ней происходит за последние 6-12 месяцев,
какие инициативы идут и какие у неё вероятные потребности». Источники
агента: сайт компании, новости, вакансии, LinkedIn (best-effort),
открытый веб; всё вторичное помечается, каждый вывод - со ссылкой.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime

import requests
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_tavily import TavilySearch
from langgraph.prebuilt import create_react_agent

from dataclasses import asdict

from app import scoring
from app.models import CompanyCandidate
from app.sources.crbr import BeneficiariesResult, get_beneficiaries
from app.sources.financials import FinancialsResult, get_financials
from app.sources.vacancies import JobsResult, get_jobs


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


# Маркеры бот-защиты (Cloudflare и т.п.): страница отдаётся с кодом 200,
# но телом идёт челлендж, а не контент. Пробивать его тут не пытаемся
# (это отдельная задача с прокси) - честно сообщаем агенту, что прочитать
# не удалось, иначе он примет заглушку за пустую страницу и напишет
# ложное "отзывов не найдено". Тот же приём, что в financials._fetch_markdown.
_BOT_WALL_MARKERS = (
    "just a moment",
    "security verification",
    "enable javascript and cookies",
)
# Целевое чтение одной страницы: 8k хватает на сайт компании целиком и на
# таблицу агрегатора; хвост длинных лендингов - футер/меню (проверено).
_WEBPAGE_CHAR_LIMIT = 8000


@tool
def extract_website_text(url: str) -> str:
    """Используй это, чтобы прочитать полный текст веб-страницы."""
    headers = {"Accept": "text/markdown"}
    try:
        # Jina возвращает чистый текст (markdown) без рендеринга браузером.
        response = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=15)
    except Exception as e:
        return f"Ошибка при доступе к {url}: {str(e)}"

    if response.status_code != 200:
        return f"Ошибка при чтении сайта: {response.status_code}"

    text = response.text
    if len(text) < 800 or any(m in text[:2000].lower() for m in _BOT_WALL_MARKERS):
        return (
            f"Страница {url} недоступна для чтения (бот-защита сайта). "
            "Контент прочитать не удалось - не выдумывай его и не считай "
            "страницу пустой."
        )
    return text[:_WEBPAGE_CHAR_LIMIT]


FINANCIAL_SYSTEM_PROMPT = """\
Ты - аналитик, который готовит «пульс компании» для B2B-продавца.
Читатель решает, что этой компании можно предложить и с каким заходом.
Твой вопрос - не «насколько компания финансово здорова», а «что в ней
происходит СЕЙЧАС, куда она движется и какие у неё вероятные
потребности».

Правила:
- Юрлицо уже подтверждено по государственным реестрам, его реквизиты
  даны в задаче. Не меняй их и не "уточняй".
- В задаче есть блоки жёстких фактов (одпис KRS, владельцы CRBR,
  финансы, вакансии) - используй их как ГОТОВЫЙ контекст: масштаб и
  динамику компании бери из финансового блока, реорганизации (слияния/
  поглощения/разделения) из dział 6 - это важнейшее событие для
  раздела «Что изменилось», владельцы из CRBR подсказывают, кто
  реально принимает решения. Не ищи эти данные заново: самостоятельно
  в вебе ты ищешь только сайт компании, новости и LinkedIn.
- Каждый вывод сопровождай ссылкой на источник, из которого ты его
  взял, и датой события, если она известна. Событие без даты - слабый
  сигнал, скажи об этом честно.
- Строго отделяй факты от гипотез. Гипотезы (вероятные проблемы,
  потребности, что можно предложить) помечай словом «гипотеза» и
  строй по схеме «сигнал → вывод»: какой наблюдаемый факт на неё
  указывает. Гипотеза без сигнала-основания запрещена.
- Не выдумывай. Если по какому-то разделу ничего не нашлось - пиши
  «данных не нашлось» прямо; для малых компаний это нормально.
- Вакансии - один из самых честных сигналов о планах компании: кого
  нанимают, сколько примерно и куда (новые роли = новые инициативы,
  массовый найм = рост, вакансии продажников на новый рынок =
  экспансия). Активные вакансии уже собраны из живой выдачи pracuj.pl и
  даны в задаче готовым блоком (открыты сейчас, с реальными датами
  публикации) - используй только их и НЕ ищи вакансии сам; ссылайся на
  URL и дату из блока.
- LinkedIn часто закрыт для чтения - если страница не открылась, не
  придумывай её содержимое; используй то, что видно из поисковой
  выдачи (число сотрудников, свежие посты в сниппетах), с пометкой.
- Серьёзные стоп-сигналы из фактов (ликвидация, банкротство,
  задолженности dział 4) не замалчивай - продавать банкроту не надо;
  упомяни их кратко в «Снимке» и «Качестве данных», но не превращай
  отчёт в риск-аудит.
- Таблицы ориентируй временем вниз: периоды (годы, кварталы) - всегда
  строки, показатели - колонки. Отчёт читают с телефона; таблица с
  годами в колонках растёт вправо с каждым годом истории и уезжает за
  край экрана. Правильная шапка: `| Год | Выручка | Чистая прибыль |
  Капитал |`. Не больше 4-5 колонок; источник цифр - не колонка, а
  примечание курсивом под таблицей.

Структура отчёта (Markdown, на русском). Каждую секцию оформляй
заголовком второго уровня (## ...) ровно с этими названиями - по ним
интерфейс строит навигацию; содержимое секции - обычный текст, списки,
таблицы, БЕЗ жирных псевдозаголовков вместо секций:
# Пульс компании: <название компании>
## Снимок
3-5 предложений: чем занимается, какого масштаба (люди/выручка, если
известны), главное событие или тренд последних месяцев, одна фраза -
куда компания, судя по всему, движется. Если есть стоп-сигналы
(ликвидация/банкротство) - назови сразу здесь.
## Идентификация
KRS, NIP, REGON, адрес, статус (из задачи)
## Чем занимается компания
продукты/услуги, для кого (сегменты клиентов, рынки), как продаёт;
отталкивайся от официального PKD из задачи и сайта компании; масштаб
по финансовому блоку (выручка, динамика) с пометкой «по данным
агрегаторов»
## Что изменилось за 6-12 месяцев
хронология событий с датами и ссылками, от свежих к старым: новости,
пресс-релизы, реорганизации из dział 6 (первоисточник - обязательно,
если есть), смена владельцев из CRBR (если факты её помечают), новые
продукты/услуги на сайте, открытие офисов, заметные изменения в
команде. Нет событий - так и напиши.
## Текущие инициативы
что компания делает прямо сейчас, по наблюдаемым сигналам: активные
вакансии (кого и сколько нанимают, какие роли новые), анонсы на сайте,
свежие посты/публикации. Каждая инициатива - с источником.
## Вероятные проблемы и потребности
3-6 гипотез по схеме «сигнал → вероятная потребность». Пример формата:
«нанимают трёх разработчиков интеграций (pracuj.pl, опубл. 2026-07-04)
→ вероятно, строят интеграционный слой и им не хватает рук/экспертизы».
Только гипотезы с основанием.
## Что им можно предложить
конкретные категории продуктов/услуг под найденные потребности, каждая
с обоснованием (какая потребность закрывается и по какому сигналу) и,
если видно из данных, кто вероятный покупатель внутри компании
(владелец-управленец, правление, конкретный отдел из вакансий).
## Качество данных
отчитайся по каждому источнику: сайт, новости, LinkedIn (что искал и
что нашлось) и вакансии (даны готовым блоком - сколько подтверждённых,
что под вопросом, или «источник недоступен»); чего не хватает
(закрытый LinkedIn, нет новостей, нет вакансий), стоп-сигналы из
реестров если есть. Если какой-то источник не дал результатов - это
нормально, но он обязан быть здесь упомянут.
## Источники
список URL

Ничего после секции «Источники» не выводи.
"""

_agent = None

# На результат поиска: обычный TavilySearch отдаёт сниппет в пару
# предложений, из-за чего flash-lite-агент почти не открывает страницы
# отдельно (проверено по трейсам). С include_raw_content тело страницы
# приходит сразу в результатах; режем до 5k на результат, чтобы 5 страниц
# за запрос не переполняли контекст - ценность сайтов в начале, хвост шум.
_TAVILY_RESULT_CHAR_LIMIT = 5000


def _make_web_search_tool():
    """Поиск в вебе, отдающий агенту сразу текст найденных страниц."""
    search = TavilySearch(max_results=5, include_raw_content=True)

    @tool
    def web_search(query: str) -> str:
        """Ищет в интернете; возвращает заголовок, URL и текст найденных страниц."""
        results = search.invoke({"query": query}).get("results", [])
        if not results:
            return "По запросу ничего не найдено."
        blocks = []
        for item in results:
            # raw_content есть не у всех доменов (часть отдаёт только сниппет).
            body = item.get("raw_content") or item.get("content") or ""
            blocks.append(
                f"### {item.get('title', '')}\n"
                f"URL: {item.get('url', '')}\n"
                f"{body[:_TAVILY_RESULT_CHAR_LIMIT]}"
            )
        return "\n\n---\n\n".join(blocks)

    return web_search


def _get_agent():
    """Ленивая инициализация: ключи из .env к этому моменту уже загружены."""
    global _agent
    if _agent is None:
        llm = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite", temperature=0.3)
        tools = [_make_web_search_tool(), extract_website_text]
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
        last_year = scoring.latest_statement_year(facts.last_statement_period)
        if last_year is not None:
            expected = scoring.expected_statement_year(date.today())
            if last_year >= expected:
                statements_line += (
                    f"; срок подачи отчёта за {last_year + 1} год ещё не "
                    "истёк, отсутствие более свежего отчёта - НЕ риск"
                )
            else:
                statements_line += (
                    f"; отчёт за {expected} год ПРОСРОЧЕН "
                    f"(срок подачи ~15.07.{expected + 1})"
                )
    else:
        statements_line = "не сдавались (ни одной записи в реестре)"

    # Реорганизации (слияния/поглощения/разделения) из dział 6: описание
    # реестра длинное (юрформулировки, ФИО нотариуса, репертории) - режем,
    # сохраняя начало с типом сделки и датами. Полный текст лежит в evidence.
    reorg_line = ""
    if facts.reorganizations:
        items = []
        for r in facts.reorganizations:
            head = r.circumstance or "реорганизация"
            who = f" - {'; '.join(r.parties)}" if r.parties else ""
            detail = ""
            if r.description:
                d = " ".join(r.description.split())
                detail = f" Детали: {d[:500]}" + ("…" if len(d) > 500 else "")
            items.append(f"{head}{who}.{detail}")
        reorg_line = (
            "- Реорганизация (dział 6, слияния/поглощения/разделения - "
            "первоисточник, НЕ бедствие): " + " | ".join(items) + "\n"
        )

    return (
        "Жёсткие факты из одписа KRS (первоисточник, приоритет над "
        "агрегаторами):\n"
        f"- Дата регистрации в KRS: {facts.registration_date or 'нет данных'}\n"
        f"- Правовая форма: {facts.legal_form or 'нет данных'}\n"
        f"- Основная деятельность: {facts.main_activity or 'нет данных'}\n"
        f"- Уставный капитал: {facts.share_capital or 'нет данных'}\n"
        f"- Годовые отчёты: {statements_line}\n"
        f"- Задолженности/взыскания (dział 4): "
        f"{', '.join(facts.arrears_flags) if facts.arrears_flags else 'записей нет'}\n"
        f"- Ликвидация/банкротство (dział 6): "
        f"{', '.join(facts.distress_flags) if facts.distress_flags else 'записей нет'}\n"
        f"{reorg_line}\n"
    )


# CRBR обязывает подать бенефициаров в течение ~14 рабочих дней от записи
# в KRS. Считаем календарным порогом с запасом на выходные/праздники,
# чтобы свежую фирму не записать в нарушители зря.
_CRBR_FILING_GRACE_DAYS = 21


def _norm_address(address: str) -> str:
    """Грубая нормализация адреса для сравнения (все из CRBR, один формат)."""
    return " ".join(address.lower().replace(",", " ").replace(".", " ").split())


# Свежая смена собственника прямо перед сделкой - классический признак
# активации «полки»/феникса. Порог - когда считать смену «недавней».
_RECENT_OWNERSHIP_CHANGE_DAYS = 180


def _ownership_history_lines(ben: BeneficiariesResult, today: date) -> list[str]:
    """Флаги из истории CRBR: расхождение и недавняя смена владельцев."""
    out: list[str] = []
    if ben.discrepancy:
        out.append(
            "- РАСХОЖДЕНИЕ CRBR: по компании подано официальное расхождение по "
            "бенефициару (обязанный субъект - банк/нотариус - заявил, что "
            "декларация не соответствует данным); красный флаг, назови прямо."
        )
    # Говорим об истории ТОЛЬКО при реальной смене состава. У свежей фирмы
    # owners_since ≈ дата регистрации и тоже «недавняя», но это не флип, а
    # новизна компании (её ловит грейс-период CRBR по дате регистрации).
    if ben.ownership_changed and ben.owners_since:
        try:
            since = date.fromisoformat(ben.owners_since)
        except ValueError:
            since = None
        if since is not None:
            age = (today - since).days
            if age <= _RECENT_OWNERSHIP_CHANGE_DAYS:
                out.append(
                    f"- СМЕНА ВЛАДЕЛЬЦЕВ: состав сменился недавно, текущий "
                    f"действует лишь с {ben.owners_since} (~{age} дн.) - перед "
                    "сделкой это флаг (возможна активация «полки»/феникс)."
                )
            else:
                out.append(
                    "- Состав владельцев за историю CRBR менялся; текущий "
                    f"действует с {ben.owners_since} (давно, сам по себе не флаг)."
                )
    return out


def _network_lines(beneficiary, note_address) -> list[str]:
    """Строки про сеть 1-го уровня (другие фирмы владельца из CRBR)."""
    status = beneficiary.network_status
    if status == "no_pesel":
        return ["  (сеть не проверена: у владельца нет PESEL в CRBR)"]
    if status == "error":
        return ["  (сеть не проверена: обратный запрос CRBR не удался)"]
    if status != "ok":
        return []
    if not beneficiary.linked_companies:
        return ["  других фирм с этим владельцем в CRBR не найдено"]

    lines = [f"  другие фирмы этого владельца (CRBR, {len(beneficiary.linked_companies)}):"]
    for lc in beneficiary.linked_companies:
        note_address(lc.name, lc.address)
        tag = " [в процедуре банкротства/реструктуризации]" if lc.in_proceedings else ""
        details = ", ".join(p for p in (lc.legal_form, lc.address) if p)
        lines.append(f"    - {lc.name} (KRS {lc.krs or 'н/д'}) - {details}{tag}")
    if beneficiary.linked_truncated:
        lines.append("    - … список усечён, у владельца фирм больше (сам по себе сигнал)")
    return lines


def _owners_block(
    ben: BeneficiariesResult, company: CompanyCandidate, today: date
) -> str:
    """Реальные владельцы из CRBR + сеть 1-го уровня + интерпретация пустого."""
    src = ben.source_url
    if not ben.ok:
        return (
            f"Владельцы (CRBR): реестр недоступен ({ben.note or 'ошибка'}) - "
            "не трактуй это как флаг, просто отметь, что данных о владельцах "
            "получить не удалось.\n\n"
        )

    if ben.found:
        lines = [
            "Реальные владельцы (бенефициары) из CRBR (первоисточник, "
            f"публичный госреестр, {src}). Это сигнал, кто реально "
            "принимает решения в компании - используй при ответе, кому "
            "внутри компании адресовать предложение:"
        ]
        # Для сигнала общего адреса собираем адреса всех фирм сети + самой
        # проверяемой (все в одном CRBR-формате, сравнение по нормализации).
        addr_owners: dict[str, list[str]] = {}

        def note_address(company_label: str, address: str | None) -> None:
            # Дедуп по фирме: одна фирма фигурирует у нескольких владельцев,
            # но адрес считаем один раз, иначе фирма «делит адрес сама с собой».
            if address:
                bucket = addr_owners.setdefault(_norm_address(address), [])
                if company_label not in bucket:
                    bucket.append(company_label)

        note_address(company.name, ben.subject_address)

        for b in ben.beneficiaries:
            parts = [b.name]
            if b.citizenship:
                parts.append("гражданство: " + ", ".join(b.citizenship))
            if b.ownership:
                parts.append("; ".join(b.ownership))
            lines.append("- " + " | ".join(parts))
            lines.extend(_network_lines(b, note_address))

        shared = {a: labels for a, labels in addr_owners.items() if len(labels) > 1}
        for labels in shared.values():
            uniq = list(dict.fromkeys(labels))
            lines.append(
                "- ОБЩИЙ АДРЕС у фирм: " + ", ".join(uniq) + " - несколько "
                "фирм владельцев по одному адресу, возможна массовая "
                "регистрация/виртуальный офис; отметь как флаг."
            )
        lines.extend(_ownership_history_lines(ben, today))
        return "\n".join(lines) + "\n\n"

    # Записи нет - интерпретируем по дате регистрации и форме.
    legal_form = (company.facts.legal_form if company.facts else None) or ""
    registered = scoring._parse_registration_date(
        company.facts.registration_date if company.facts else None
    )
    prefix = f"Владельцы (CRBR, {src}): записи о бенефициарах нет. "

    if "AKCYJNA" in legal_form.upper():
        return (
            prefix
            + "Компания - spółka akcyjna; листингованные на регулируемом "
            "рынке освобождены от CRBR, поэтому отсутствие записи здесь НЕ "
            "красный флаг (если это публичная компания).\n\n"
        )
    if registered is not None:
        age_days = (today - registered).days
        if age_days <= _CRBR_FILING_GRACE_DAYS:
            return (
                prefix
                + f"Фирма зарегистрирована недавно ({company.facts.registration_date}), "
                "срок подачи в CRBR (~14 рабочих дней) ещё не истёк - "
                "отсутствие записи это НОРМА, а не флаг.\n\n"
            )
        return (
            prefix
            + f"Фирма зарегистрирована {company.facts.registration_date}, срок "
            "подачи в CRBR давно истёк, но бенефициары так и не поданы - это "
            "КРАСНЫЙ ФЛАГ (неисполнение обязанности CRBR).\n\n"
        )
    return prefix + "Дату регистрации определить не удалось.\n\n"


def _name_signature(name: str) -> tuple[tuple[str, int], ...]:
    """Сигнатура имени для матча маски с полным именем.

    Маскировка KRS сохраняет первую букву и длину каждой части имени
    ("Y*** M*********"), поэтому (первая буква, длина) по каждому токену
    одинаковы у маски и у полного имени из CRBR ("YURI MAKSIMENKA").
    """
    return tuple((tok[0].upper(), len(tok)) for tok in name.split())


def _match_owner(masked_name: str, owner_sigs: dict) -> str | None:
    """Полное имя владельца из CRBR, если маска однозначно с ним совпала.

    При неоднозначности (у двух владельцев одинаковая сигнатура)
    возвращаем None - лучше оставить под звёздочками, чем угадать.
    """
    matches = owner_sigs.get(_name_signature(masked_name), [])
    return matches[0] if len(matches) == 1 else None


def _management_block(company: CompanyCandidate, ben: BeneficiariesResult) -> str:
    """Наёмное управление из dział 2 KRS.

    Имена реестр отдаёт замаскированными. Тех, кто совпал с владельцами
    из CRBR (по инициалу+длине), раскрываем: это законный джойн двух
    публичных наборов, показывающий владельца в органах/прокуре.
    """
    m = company.facts.management if company.facts else None
    if m is None:
        return "Управление (dział 2 KRS): данных нет.\n\n"

    owner_sigs: dict[tuple, list[str]] = {}
    if ben.ok and ben.found:
        for b in ben.beneficiaries:
            owner_sigs.setdefault(_name_signature(b.name), []).append(b.name)

    revealed: list[str] = []  # владельцы, найденные в органах/прокуре

    def render(member) -> str:
        base = f"{member.name} - {member.role}" if member.role else member.name
        owner = _match_owner(member.name, owner_sigs)
        if owner:
            revealed.append(owner)
            return f"{base} → это владелец {owner} (совпадение с CRBR)"
        return base

    lines = [
        "Структура управления из KRS dział 2 (наёмное правление; имена "
        "реестр отдаёт ЗАМАСКИРОВАННЫМИ звёздочками - показывай как есть, "
        "не додумывай полные имена, КРОМЕ явно раскрытых по совпадению с "
        "CRBR):"
    ]
    if m.representation_body:
        lines.append(f"- Орган представительства: {m.representation_body}")
    if m.board:
        lines.append("- Правление (zarząd): " + "; ".join(render(b) for b in m.board))
    if m.supervisory_board:
        lines.append(
            "- Надзорный орган: " + "; ".join(render(b) for b in m.supervisory_board)
        )
    if m.proxies:
        lines.append("- Прокуренты: " + "; ".join(render(p) for p in m.proxies))
    if m.representation_mode:
        lines.append(f"- Способ представительства: {m.representation_mode}")

    # Сигналы из джойна, если владельцы вообще известны.
    if owner_sigs:
        if revealed:
            lines.append(
                f"- ЗНАЧИМО: владельцы ({', '.join(dict.fromkeys(revealed))}) "
                "сами сидят в органах/прокуре - прямой контроль собственников "
                "(у prokura samoistna - полное единоличное право подписи)."
            )
        board_revealed = any(_match_owner(b.name, owner_sigs) for b in m.board)
        if m.board and not board_revealed:
            lines.append(
                "- Ни один член правления не входит в число владельцев - "
                "правление может быть номинальным (наёмный директор при "
                "скрытых через прокуру собственниках)."
            )
    return "\n".join(lines) + "\n\n"


def _financials_block(fin: FinancialsResult) -> str:
    """Готовые цифры из адаптера - приоритетнее гугла, если есть."""
    if not fin.has_data:
        return (
            "Готовые финансовые показатели из агрегаторов не найдены - "
            "масштаб компании оценивай по косвенным сигналам (сайт, "
            "вакансии, LinkedIn), специально цифры не разыскивай.\n\n"
        )

    lines = [
        "Финансовые показатели по годам (источник: "
        f"{', '.join(fin.sources)}, вторичный - агрегаторы официального "
        "реестра). Используй их для оценки масштаба и динамики компании, "
        "не ищи эти цифры заново:"
    ]
    for y in fin.years:
        def money(v: float | None) -> str:
            return f"{v:,.0f} PLN".replace(",", " ") if v is not None else "нет данных"

        lines.append(
            f"- {y.year}: выручка {money(y.revenue)}; "
            f"чистая прибыль {money(y.net_profit)}; "
            f"капитал {money(y.equity)}; обязательства {money(y.liabilities)}"
        )
    if fin.cross_check:
        lines.append("Сверка источников: " + "; ".join(fin.cross_check))
    return "\n".join(lines) + "\n\n"


# Сколько вакансий показывать агенту списком: остальное сворачиваем в
# распределение по корзинам, чтобы у компании с частым именем блок не
# раздувал промпт (его перечитывают на каждом ходе ReAct).
_JOBS_DISPLAY_CAP = 15


def _dist_line(buckets: dict[str, int]) -> str:
    # Сортировка детерминированная (кол-во ↓, имя ↑), иначе одинаковые
    # счётчики рендерятся в порядке прихода из выдачи и evidence «дрожит».
    dist = ", ".join(
        f"{name}: {count}"
        for name, count in sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    return f"Распределение по направлениям: {dist}."


def _jobs_block(jobs: JobsResult) -> str:
    """Готовые вакансии из адаптера - агент их не ищет ни при каком исходе.

    Источник - живая выдача pracuj.pl (через Jina): только реально
    открытые сейчас оферты, с настоящей датой публикации. Это уже
    отфильтровано по работодателю, датам можно доверять.
    """
    if not jobs.ok:
        # Причина - в последней заметке адаптера (сбой Jina / бот-защита /
        # краш): пробрасываем, чтобы отличить сбой от «вакансий нет».
        reason = jobs.notes[-1] if jobs.notes else "сбой источника"
        return (
            f"Источник вакансий недоступен ({reason}) - в «Качестве данных» "
            "честно напиши «вакансии проверить не удалось», НЕ выдавай это "
            "за «вакансий нет». Сам вакансии НЕ ищи.\n\n"
        )
    if not jobs.has_data:
        # Живой поиск pracuj.pl - если пусто, значит открытых оферт правда нет.
        return (
            "Активные вакансии собраны автоматически из живой выдачи "
            "pracuj.pl: открытых нет. Пиши «активных вакансий не найдено» - "
            "для малых компаний это нормально; сам вакансии НЕ ищи и не "
            "выдумывай.\n\n"
        )
    lines = [
        "Активные вакансии - живая выдача pracuj.pl (открыты СЕЙЧАС, "
        "отфильтрованы по работодателю; дата - реальная дата публикации). "
        "Используй их как есть, сам вакансии НЕ ищи; ссылайся на URL:"
    ]
    for v in jobs.vacancies[:_JOBS_DISPLAY_CAP]:
        when = f"опубл. {v.published}" if v.published else "дата не указана"
        lines.append(
            f"- {v.title} - {v.city or 'город не указан'} ({when}; {v.url})"
        )
    extra = len(jobs.vacancies) - _JOBS_DISPLAY_CAP
    if extra > 0:
        lines.append(f"- ... и ещё {extra} (см. распределение ниже)")
    if jobs.buckets:
        lines.append(_dist_line(jobs.buckets))
    if jobs.rejected_namesakes:
        lines.append(
            f"(Ещё {jobs.rejected_namesakes} оферт из выдачи отсеяно как "
            "чужие работодатели-тёзки - в отчёт их не тащи.)"
        )
    lines.append(
        "Как читать (это подсказки, сверяй с остальными данными): несколько "
        "производственных ролей -> вероятно, наращивают мощности; появление "
        "ролей księgowość/kadry в штат -> возможно, бэк-офис перерастает "
        "аутсорсинг; роли sprzedaż под новый рынок -> экспансия."
    )
    return "\n".join(lines) + "\n\n"


def _task_prompt(
    company: CompanyCandidate,
    fin: FinancialsResult,
    ben: BeneficiariesResult,
    jobs: JobsResult,
) -> str:
    today = date.today().isoformat()
    return (
        "Построй «пульс компании»: что в ней происходит сейчас, куда она "
        f"движется и какие у неё вероятные потребности. Сегодня {today} - "
        "«последние 6-12 месяцев» отсчитывай от этой даты.\n\n"
        "Официальные данные (подтверждены государственными реестрами, "
        "используй их как есть):\n"
        f"- Название: {company.name}\n"
        f"- KRS: {company.krs or 'нет'}\n"
        f"- NIP: {company.nip or 'нет'}\n"
        f"- REGON: {company.regon or 'нет'}\n"
        f"- Адрес: {company.address or 'нет'}\n"
        f"- Статус в реестре: {company.status or 'неизвестен'}\n\n"
        + _facts_block(company)
        + _owners_block(ben, company, date.today())
        + _management_block(company, ben)
        + _financials_block(fin)
        + _jobs_block(jobs)
        +
        "Выполни ВСЕ три шага исследования ниже, в каждом - минимум "
        "один отдельный вызов web_search со своим запросом. Один поиск "
        "не покрывает несколько шагов: выдача про сайт не заменяет "
        "поиск новостей. Не начинай писать отчёт, пока не сделал все "
        "три шага. Активные вакансии уже собраны в блоке выше - отдельными "
        "запросами web_search их НЕ ищи (единственное исключение - "
        "страница вакансий на самом сайте компании, шаг 1).\n\n"
        "Шаг 1 - сайт компании: найди его (например запросом "
        f"'{company.name} oficjalna strona') и прочитай главную страницу; "
        "затем прочитай 1-3 полезные подстраницы, если они есть (o nas / "
        "oferta / aktualności / blog / kariera - выбирай по ссылкам с "
        "главной). Страница «kariera» на своём сайте - первоисточник "
        "вакансий: чем дополняет блок выше, добавь с пометкой «источник: "
        "сайт компании». Официальный PKD уже дан в фактах выше - сверь с "
        "ним описание.\n\n"
        "Шаг 2 - новости и события: поищи свежие упоминания (например "
        f"'{company.name} aktualności', '{company.name} news', "
        f"'\"{company.name}\" 2025 OR 2026'). Открывай статьи с датами; "
        "события старше 12 месяцев в хронологию не тащи, кроме "
        "реорганизаций из фактов.\n\n"
        "Шаг 3 - LinkedIn (best-effort): поищи страницу компании "
        f"(например '{company.name} linkedin'). Страница часто закрыта "
        "бот-защитой - тогда используй только то, что видно в поисковой "
        "выдаче (число сотрудников, сниппеты постов), с пометкой об "
        "ограничении.\n\n"
        "После этого собери отчёт по заданной структуре: хронологию "
        "изменений, текущие инициативы, гипотезы о потребностях и что "
        "компании можно предложить - каждый пункт с источником."
    )


@dataclass
class HealthCheckResult:
    report_markdown: str
    evidence: dict
    scores: dict


def run_health_check(company: CompanyCandidate) -> HealthCheckResult:
    # fin (цифры агрегаторов), ben (бенефициары CRBR) и jobs (вакансии из
    # выдачи) тянем детерминированно ДО агента - это готовые данные, ему
    # незачем искать их в вебе и гадать. Запросы независимы и все сетевые,
    # поэтому гоняем параллельно, чтобы не складывать таймауты на и без
    # того латентном пути (тот же приём, что в companies._resolve_by_name).
    # max_workers = числу задач: иначе третья ждала бы свободный поток и
    # «параллельность» стала бы последовательностью.
    with ThreadPoolExecutor(max_workers=3) as pool:
        fin_future = pool.submit(get_financials, company.name, company.krs or "")
        jobs_future = pool.submit(get_jobs, company.name, company.address)
        ben_future = (
            pool.submit(get_beneficiaries, company.nip, with_network=True)
            if company.nip
            else None
        )
        fin = fin_future.result()
        jobs = jobs_future.result()
        ben = (
            ben_future.result()
            if ben_future
            else BeneficiariesResult(nip="", ok=False, note="NIP неизвестен")
        )

    agent = _get_agent()
    result = agent.invoke(
        {"messages": [HumanMessage(content=_task_prompt(company, fin, ben, jobs))]}
    )
    messages = result["messages"]
    raw_report = message_content_to_text(messages[-1].content)

    # JSON-блок с осями finances/people агент выдаёт в хвосте ответа;
    # в отчёт для человека он попасть не должен.
    report, llm_axes = scoring.split_llm_scores(raw_report)
    scores = scoring.build_scores(company, fin, llm_axes)

    evidence = {
        "schema": "toxic-scanner/company-pulse-evidence/v0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "company": company.model_dump(),
        "data_confidence": "secondary_sources",
        "financials": asdict(fin),
        "beneficiaries": asdict(ben),
        "jobs": asdict(jobs),
        "scores": scores,
        "agent_stats": _agent_stats(messages),
        "agent_trace": _messages_to_trace(messages),
    }
    return HealthCheckResult(report_markdown=report, evidence=evidence, scores=scores)


def _agent_stats(messages: list) -> dict:
    """Цена рана агента в сырых числах: LLM-вызовы, токены, инструменты.

    Токены суммируем из usage_metadata AI-сообщений (их заполняет
    langchain на каждый вызов модели). Конверсию в деньги не зашиваем:
    прайсы меняются - множить на текущий прайс Gemini/Tavily снаружи.
    """
    stats: dict = {
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_calls": {},
    }
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        stats["llm_calls"] += 1
        usage = message.usage_metadata or {}
        stats["input_tokens"] += usage.get("input_tokens", 0)
        stats["output_tokens"] += usage.get("output_tokens", 0)
        for call in message.tool_calls or []:
            name = call.get("name") or "unknown"
            stats["tool_calls"][name] = stats["tool_calls"].get(name, 0) + 1
    return stats


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
