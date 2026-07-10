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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime

import requests
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_tavily import TavilySearch
from langgraph.prebuilt import create_react_agent

from dataclasses import asdict

from app import scoring
from app.models import CompanyCandidate
from app.sources.crbr import BeneficiariesResult, get_beneficiaries
from app.sources.financials import FinancialsResult, get_financials


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
Ты - аналитик, который строит health check польской компании для
человека, решающего, подписывать ли с ней контракт или оффер.

Правила:
- Юрлицо уже подтверждено по государственным реестрам, его реквизиты
  даны в задаче. Не меняй их и не "уточняй".
- В задаче есть блок "Жёсткие факты из одписа KRS" - это первоисточник
  (государственный реестр). Строй выводы в первую очередь на нём.
  Польские компании обязаны сдавать годовой отчёт ежегодно, срок подачи
  за год Y - примерно 15 июля года Y+1. В строке "Годовые отчёты" явно
  сказано, соблюдён ли сейчас этот срок: просрочку называй красным
  флагом прямо, но НЕ записывай в риски отсутствие отчёта, срок подачи
  которого ещё не истёк.
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
  отзывы за факты о компании. ВАЖНО: полный текст отзывов gowork обычно
  НЕ читается (бот-защита - инструмент вернёт "недоступно"); тогда НЕ
  придумывай темы и тональность. Что обычно доступно из выдачи - число
  отзывов ("N opinii"): передай его как слабый сигнал ("на gowork
  N отзывов"), сверяя город/адрес (у тёзок и филиалов профили разные).
  Балла (X/5) в выдаче обычно нет - не выдумывай. Если не нашлось
  ничего - так и напиши (для маленьких компаний это нормально, а не риск).
- Отделяй факты от предположений.

Структура отчёта (Markdown, на русском). Каждую секцию оформляй
заголовком второго уровня (## ...) ровно с этими названиями - по ним
интерфейс строит навигацию; содержимое секции - обычный текст, списки,
таблицы, БЕЗ жирных псевдозаголовков вместо секций:
# Health Check: <название компании>
## Health score
0-100 с одним предложением обоснования
## Идентификация
KRS, NIP, REGON, адрес, статус (из задачи)
## О компании
3-6 предложений - чем занимается (отталкивайся от официального PKD из
задачи и сайта компании), какие продукты/услуги, для кого работает,
масштаб; затем 2-4 свежие новости или публичных события с датами и
ссылками (расширение, награды, суды, увольнения - что нашлось). Нет
новостей - так и напиши, это нормально для малых компаний. Не
пересказывай финансы здесь - для них своя секция.
Если в фактах есть строка "Реорганизация" (слияние/поглощение/разделение
из dział 6 KRS) - это первоисточник и существенное событие: ОБЯЗАТЕЛЬНО
опиши его здесь (что произошло, с кем, когда) и что оно значит для
контрагента - смена периметра активов и обязательств. Поглощение другой
компании - как правило консолидация, а не бедствие; не путай с
ликвидацией/банкротством
## Факты из реестра KRS
дата регистрации, капитал, сдача годовой отчётности, задолженности,
ликвидация/банкротство, реорганизация (слияния/разделения) - как есть
из задачи
## Владельцы и управление
СНАЧАЛА реальные владельцы (бенефициары из CRBR, первоисточник) -
это главный сигнал "кто стоит за компанией": имя, гражданство, доля.
ЗАТЕМ, короче, наёмное управление из KRS (правление/надзор/прокура) -
имена там реестр отдаёт замаскированными звёздочками. Раскрывай полным
именем ТОЛЬКО тех, кого факты уже раскрыли по совпадению с CRBR;
остальных приводи ровно как в реестре (со звёздочками) и НЕ подставляй
их имена из веб-поиска или агрегаторов, даже если встретил. НИКОГДА не
приписывай реестру KRS имя, которого нет в блоке фактов (не пиши
"в реестре указан ...") - в KRS эти имена замаскированы, утверждать
обратное - фактическая ошибка. Если факты отмечают, что владелец
сам сидит в органах/прокуре или что правление номинальное (не
пересекается с владельцами), - прямо скажи об этом, это значимый
сигнал контроля. Отсутствие записи в CRBR трактуй строго по подсказке в
фактах (у свежей фирмы это норма, у давно зарегистрированной -
красный флаг, у листингованной S.A. - освобождение). Иностранные
владельцы у молодой фирмы с минимальным капиталом - усиливающий
сигнал, оценивай в совокупности, а не как приговор сам по себе.
Если факты перечисляют ДРУГИЕ фирмы тех же владельцев (сеть) - кратко
приведи их (название, KRS); особо отметь общий адрес нескольких фирм
(возможна массовая регистрация) и фирмы в процедуре банкротства -
это флаги. Если сеть не проверялась (нет PESEL) - так и скажи, без домыслов.
Если факты помечают РАСХОЖДЕНИЕ CRBR или недавнюю СМЕНУ ВЛАДЕЛЬЦЕВ -
вынеси это отдельным явным флагом (расхождение = серьёзный сигнал;
смена собственника перед сделкой - возможная активация «полки»/феникс)
## Краткий вывод
2-4 предложения
## Финансовые показатели по годам
таблица (выручка, прибыль/убыток, активы, капитал) со ссылками на
источники; если данных нет - явно скажи об этом
## Отзывы сотрудников
если текст отзывов прочитать удалось - повторяющиеся жалобы и плюсы со
ссылками, пометка "неофициальный сигнал"; если из gowork доступен только
счётчик - укажи число отзывов как слабый сигнал (город/адрес против
тёзок), без выдуманной тональности; если ничего - явно скажи об этом
## Риск-флаги
список
## Качество данных
какие источники нашлись, чего не хватает
## Источники
список URL
## Что проверить перед подписанием контракта
список

После отчёта, отдельной ПОСЛЕДНЕЙ строкой (одна строка, без
код-блока и без текста после неё), выдай машиночитаемый JSON ровно
такого вида:
{"people": {"value": 0-100 или null, "basis": ["..."]}}
- people - оценка климата в компании ТОЛЬКО по реально найденным
  отзывам сотрудников; если отзывов не нашлось, value обязан быть null.
- basis - 1-4 короткие строки, каждая опирается на конкретный
  найденный отзыв или источник; для null объясни, чего не хватило.
Не выдумывай значение ради заполнения: null с объяснением лучше
угаданного числа. Финансовые оси считаются отдельно, от тебя их не
требуется.
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
            f"публичный госреестр, {src}). ЭТО ГЛАВНЫЙ сигнал 'кто стоит за "
            "компанией' - в отчёте излагай раньше и подробнее наёмного "
            "правления:"
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
            "поищи их сам (см. ниже).\n\n"
        )

    lines = [
        "Финансовые показатели по годам (источник: "
        f"{', '.join(fin.sources)}, вторичный - агрегаторы официального "
        "реестра). ИСПОЛЬЗУЙ ИХ как основную финансовую таблицу отчёта, "
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


def _short_company_name(raw: str) -> str:
    """Ядро названия для веб-запроса: без правовой формы и хвоста (KRS ...).

    "M.E.FOLIE SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ (KRS 000...)" ->
    "M.E.FOLIE". Полная форма в поисковом запросе только шумит.
    """
    name = raw.split(" (KRS")[0]
    upper = name.upper()
    cut = min(
        (idx for idx in (upper.find(" SPÓŁKA"), upper.find(" SP. Z"), upper.find(" SP Z")) if idx != -1),
        default=len(name),
    )
    return name[:cut].strip(" ,")


def _reorg_search_hint(company: CompanyCandidate) -> str:
    """Наводка агенту: обогатить известное из KRS слияние деталями из веба.

    Сам факт реорганизации стабилен (dział 6), но реестр не объясняет её
    причин. Даём целевой запрос по названиям сторон - обогащение, а не
    обнаружение: не нашлось новости, базовый факт всё равно в отчёте.
    """
    facts = company.facts
    if not (facts and facts.reorganizations):
        return ""

    others: list[str] = []
    for r in facts.reorganizations:
        for party in r.parties:
            short = _short_company_name(party)
            if short and short not in others:
                others.append(short)
    if not others:
        return ""

    pair = " ".join([_short_company_name(company.name), *others]).strip()
    return (
        "В фактах есть реорганизация (слияние/поглощение) - это стабильный "
        "факт из KRS, но причины и детали сделки реестр не объясняет. Сделай "
        "ОДИН целевой веб-поиск именно про это событие (например "
        f"'{pair} połączenie' или '{pair} przejęcie'), чтобы найти новость с "
        "причинами и контекстом. Что нашлось - добавь со ссылкой в 'О "
        "компании' как обогащение к факту из KRS; не нашлось - опиши событие "
        "по факту из реестра, без домыслов о причинах.\n\n"
    )


def _task_prompt(
    company: CompanyCandidate, fin: FinancialsResult, ben: BeneficiariesResult
) -> str:
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
        + _owners_block(ben, company, date.today())
        + _management_block(company, ben)
        + _financials_block(fin)
        +
        "Сначала представь компанию: найди её сайт (например запросом "
        f"'{company.name} oficjalna strona') и прочитай главную страницу, "
        "затем поищи свежие новости и публичные события (например "
        f"'{company.name} aktualności' и '{company.name} news'). "
        "Официальный PKD уже дан в фактах выше - сверь с ним описание.\n\n"
        + _reorg_search_hint(company)
        + "Если каких-то финансовых цифр выше не хватает, можешь поискать "
        f"их по официальным реквизитам: 'aleo.com KRS {company.krs}', "
        f"'rejestr.io {company.krs}', '{company.name} wyniki finansowe "
        "przychody'. Найденные страницы агрегаторов читай целиком.\n\n"
        "Затем собери сигнал по отзывам сотрудников: найди профиль на "
        f"gowork.pl (например 'gowork.pl {company.name} opinie') и "
        f"упоминания на Reddit (например '{company.name} reddit praca "
        "opinie'). Полный текст отзывов gowork обычно закрыт бот-защитой: "
        "если инструмент вернул 'недоступно', НЕ выдумывай отзывы, а "
        "возьми из выдачи число отзывов ('N opinii') как слабый сигнал, "
        "сверяя город/адрес компании, чтобы не спутать с тёзками и "
        "филиалами. Reddit-страницы, если открылись, читай целиком."
    )


@dataclass
class HealthCheckResult:
    report_markdown: str
    evidence: dict
    scores: dict


def run_health_check(company: CompanyCandidate) -> HealthCheckResult:
    # fin (цифры агрегаторов) и ben (бенефициары CRBR) тянем детерминированно
    # ДО агента - это готовые данные/первоисточник, ему незачем искать их в
    # вебе и гадать. Запросы независимы и оба сетевые (Jina + POST к CRBR),
    # поэтому гоняем параллельно, чтобы не складывать таймауты на и без того
    # латентном пути (тот же приём, что в companies._resolve_by_name).
    with ThreadPoolExecutor(max_workers=2) as pool:
        fin_future = pool.submit(get_financials, company.name, company.krs or "")
        ben_future = (
            pool.submit(get_beneficiaries, company.nip, with_network=True)
            if company.nip
            else None
        )
        fin = fin_future.result()
        ben = (
            ben_future.result()
            if ben_future
            else BeneficiariesResult(nip="", ok=False, note="NIP неизвестен")
        )

    agent = _get_agent()
    result = agent.invoke(
        {"messages": [HumanMessage(content=_task_prompt(company, fin, ben))]}
    )
    messages = result["messages"]
    raw_report = message_content_to_text(messages[-1].content)

    # JSON-блок с осями finances/people агент выдаёт в хвосте ответа;
    # в отчёт для человека он попасть не должен.
    report, llm_axes = scoring.split_llm_scores(raw_report)
    scores = scoring.build_scores(company, fin, llm_axes)

    evidence = {
        "schema": "toxic-scanner/health-check-evidence/v0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "company": company.model_dump(),
        "data_confidence": "secondary_sources",
        "financials": asdict(fin),
        "beneficiaries": asdict(ben),
        "scores": scores,
        "agent_trace": _messages_to_trace(messages),
    }
    return HealthCheckResult(report_markdown=report, evidence=evidence, scores=scores)


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
