const resultCard = document.querySelector("#result-card");
const resultCompany = document.querySelector("#result-company");
const reportLink = document.querySelector("#report-link");
const evidenceLink = document.querySelector("#evidence-link");
const reportOutput = document.querySelector("#report-output");

// Внешние ссылки из отчёта открываем в новой вкладке. Хук работает
// внутри санитайзера, поэтому target/rel не вырезаются.
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && node.hasAttribute("href")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

// Отчёт приходит как Markdown от LLM - недоверенный контент,
// в DOM только через DOMPurify.
function renderMarkdownReport(markdown) {
  const html = marked.parse(markdown ?? "", { async: false });
  reportOutput.innerHTML = DOMPurify.sanitize(html);
  // Широкая таблица (финансы по годам) скроллится в собственной обёртке,
  // иначе она раскатывает всю страницу за край вьюпорта на телефоне.
  for (const table of reportOutput.querySelectorAll("table")) {
    const wrap = document.createElement("div");
    wrap.className = "table-scroll";
    table.replaceWith(wrap);
    wrap.append(table);
    // Сумма, разорванная на три строки («50 311 436 PLN»), не сверяется
    // глазами. Числовым ячейкам — перенос запрещён: пусть лучше таблица
    // уедет в скролл обёртки, чем колонка цифр сложится в столбик.
    for (const td of table.querySelectorAll("td")) {
      if (/^[\d\s.,%–—-]+(PLN|zł)?$/iu.test(td.textContent.trim())) {
        td.classList.add("td-amount");
      }
    }
  }
  buildReportToc();
}

// Оглавление отчёта из его h2: секции предсказуемы (финансы, отзывы,
// риск-флаги...), а простыня длинная. Отчёт под общим скроллом страницы;
// TOC липнет к верху окна на десктопе, на узком экране — обычный список.
function buildReportToc() {
  // LLM размечает секции то как h2, то как h3 - берём тот уровень,
  // на котором секций достаточно для навигации
  let headings = [...reportOutput.querySelectorAll("h2")];
  if (headings.length < 2) {
    headings = [...reportOutput.querySelectorAll("h3")];
  }
  if (headings.length < 2) {
    return;
  }

  const nav = document.createElement("nav");
  nav.className = "report-toc";
  nav.setAttribute("aria-label", "Разделы отчёта");

  const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  headings.forEach((heading, i) => {
    heading.id = `report-section-${i}`;
    if (/риск/i.test(heading.textContent)) {
      heading.classList.add("report-risk-heading");
    }
    const link = document.createElement("button");
    link.type = "button";
    link.className = "report-toc-link";
    link.textContent = heading.textContent;
    link.addEventListener("click", () =>
      heading.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" })
    );
    nav.append(link);
  });

  reportOutput.prepend(nav);
}

// --- Звезда качества (радар, 5 осей) ---
// Ось с value=null - честное "нет данных": серый пунктир с подписью
// "н/д", в полигон значений не входит и не читается как низкая оценка.

const scoresPanel = document.querySelector("#scores-panel");
const scoresVerdict = document.querySelector("#scores-verdict");
const radarChart = document.querySelector("#radar-chart");
const scoresList = document.querySelector("#scores-list");

const SCORE_AXES = [
  ["reliability", "Надёжность"],
  ["finances", "Финансы"],
  ["dynamics", "Динамика"],
  ["people", "Люди"],
  ["transparency", "Прозрачность"],
];

// Полосы силы - способ прочтения оценки, не новая формула:
// пороги продублированы в легенде под радаром.
function scoreBand(value) {
  if (value === null) return "nd";
  if (value >= 70) return "strong";
  if (value >= 40) return "mid";
  return "weak";
}

// Две геометрии радара. На десктопе SVG живёт в колонке ~400px; на
// телефоне рисуется во всю ширину, но CSS-ширина всего ~300px — если
// сжимать десктопный viewBox 460px, подписи осей мельчают до ~8px.
// Компактный пресет уменьшает поле и радиус вместо шрифта.
// cx смещён вправо от центра: запас слева — под самую длинную боковую
// подпись («Прозрачность 100» ~112px), она анкорится снаружи оси и не
// должна вылезать за viewBox.
const RADAR_PRESETS = {
  wide: { cx: 230, cy: 150, r: 95, labelOffset: 16, width: 460, height: 300 },
  compact: { cx: 196, cy: 114, r: 66, labelOffset: 12, width: 350, height: 228 },
};

// Единая точка перелома с CSS (@media max-width: 720px).
const compactRadarQuery = window.matchMedia("(max-width: 720px)");

function currentRadar() {
  return compactRadarQuery.matches ? RADAR_PRESETS.compact : RADAR_PRESETS.wide;
}

function axisValue(axis) {
  return typeof axis?.value === "number"
    ? Math.max(0, Math.min(100, axis.value))
    : null;
}

function polarPoint(angleDeg, radius) {
  const rad = (angleDeg * Math.PI) / 180;
  const { cx, cy } = currentRadar();
  return [cx + radius * Math.cos(rad), cy + radius * Math.sin(rad)];
}

function axisAngle(index) {
  return -90 + index * (360 / SCORE_AXES.length);
}

function svgEl(name, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attrs)) {
    el.setAttribute(key, value);
  }
  return el;
}

function drawRadar(scores) {
  const R = currentRadar();
  radarChart.setAttribute("viewBox", `0 0 ${R.width} ${R.height}`);
  radarChart.replaceChildren();

  for (const level of [25, 50, 75, 100]) {
    const points = SCORE_AXES.map((_, i) =>
      polarPoint(axisAngle(i), (R.r * level) / 100).join(",")
    ).join(" ");
    radarChart.append(svgEl("polygon", { points, class: "radar-grid" }));
  }

  const valuePoints = [];
  SCORE_AXES.forEach(([key, label], i) => {
    const value = axisValue(scores[key]);
    const band = scoreBand(value);
    const angle = axisAngle(i);

    const [x2, y2] = polarPoint(angle, R.r);
    radarChart.append(
      svgEl("line", {
        x1: R.cx,
        y1: R.cy,
        x2,
        y2,
        class: value === null ? "radar-axis radar-axis-null" : "radar-axis",
      })
    );

    // Подпись оси несёт и значение: имя + число в цвете полосы,
    // чтобы звезда читалась без списка справа.
    const [lx, ly] = polarPoint(angle, R.r + R.labelOffset);
    const anchor =
      Math.abs(lx - R.cx) < 6 ? "middle" : lx > R.cx ? "start" : "end";
    const baseline =
      ly < R.cy - 6 ? "auto" : ly > R.cy + 6 ? "hanging" : "middle";
    const text = svgEl("text", {
      x: lx,
      y: ly,
      "text-anchor": anchor,
      "dominant-baseline": baseline,
      class: value === null ? "radar-label radar-label-null" : "radar-label",
    });
    const nameTspan = svgEl("tspan", {});
    nameTspan.textContent = label;
    const valueTspan = svgEl("tspan", {
      dx: 6,
      class: `radar-label-value radar-band--${band}`,
    });
    valueTspan.textContent = value === null ? "н/д" : String(value);
    text.append(nameTspan, valueTspan);
    radarChart.append(text);

    if (value !== null) {
      valuePoints.push({ point: polarPoint(angle, (R.r * value) / 100), band });
    }
  });

  const pointsAttr = valuePoints.map((v) => v.point.join(",")).join(" ");
  if (valuePoints.length >= 3) {
    const polygon = svgEl("polygon", { points: pointsAttr, class: "radar-value" });
    // Анимация расцветает из центра радара; в компактной геометрии он
    // смещён от центра viewBox, CSS-ное transform-origin: center не годится.
    polygon.style.transformOrigin = `${R.cx}px ${R.cy}px`;
    radarChart.append(polygon);
  } else if (valuePoints.length === 2) {
    radarChart.append(
      svgEl("polyline", { points: pointsAttr, class: "radar-value" })
    );
  }
  valuePoints.forEach(({ point: [px, py], band }, i) => {
    const dot = svgEl("circle", {
      cx: px,
      cy: py,
      r: 4.5,
      class: `radar-dot radar-band--${band}`,
    });
    dot.style.animationDelay = `${0.3 + i * 0.07}s`;
    radarChart.append(dot);
  });
}

function buildVerdict(scores) {
  const byBand = { strong: [], mid: [], weak: [], nd: [] };
  for (const [key, label] of SCORE_AXES) {
    byBand[scoreBand(axisValue(scores[key]))].push(label);
  }

  const parts = [];
  if (byBand.strong.length) {
    const title = byBand.strong.length > 1 ? "Сильные стороны" : "Сильная сторона";
    parts.push(`${title}: ${byBand.strong.join(", ")}`);
  }
  if (byBand.weak.length) {
    const title = byBand.weak.length > 1 ? "Слабые места" : "Слабое место";
    parts.push(`${title}: ${byBand.weak.join(", ")}`);
  }
  if (byBand.mid.length) {
    parts.push(`Средне: ${byBand.mid.join(", ")}`);
  }
  if (byBand.nd.length) {
    parts.push(`Без данных: ${byBand.nd.join(", ")}`);
  }
  return parts.join(" · ") || "Оценок пока нет";
}

function renderScoresList(scores) {
  scoresList.replaceChildren();

  for (const [key, label] of SCORE_AXES) {
    const axis = scores[key] ?? {};
    const value = axisValue(axis);
    const band = scoreBand(value);

    const item = document.createElement("details");
    item.className = `score-item score-item--${band}`;

    const summary = document.createElement("summary");
    const name = document.createElement("span");
    name.className = "score-name";
    name.textContent = label;
    const valueEl = document.createElement("span");
    valueEl.className = `score-value score-value--${band}`;
    valueEl.textContent = value === null ? "н/д" : String(value);
    summary.append(name, valueEl);
    item.append(summary);

    const basisList = document.createElement("ul");
    basisList.className = "score-basis";
    const basis =
      Array.isArray(axis.basis) && axis.basis.length ? axis.basis : ["—"];
    for (const line of basis) {
      const li = document.createElement("li");
      li.textContent = String(line);
      basisList.append(li);
    }
    item.append(basisList);

    scoresList.append(item);
  }
}

// Последние оценки — чтобы перерисовать радар при смене геометрии
// (поворот телефона, ресайз окна через брейкпоинт).
let lastScores = null;

function renderScores(scores) {
  if (!scores || typeof scores !== "object") {
    lastScores = null;
    scoresPanel.classList.add("is-hidden");
    return;
  }
  lastScores = scores;
  drawRadar(scores);
  renderScoresList(scores);
  scoresVerdict.textContent = buildVerdict(scores);
  scoresPanel.classList.remove("is-hidden");
}

function showResult(data) {
  resultCompany.textContent = data.company;
  reportLink.href = data.report_url;
  if (data.evidence_url) {
    evidenceLink.href = data.evidence_url;
    evidenceLink.classList.remove("is-hidden");
  } else {
    evidenceLink.classList.add("is-hidden");
  }
  renderScores(data.scores);
  renderMarkdownReport(data.report);
  resultCard.classList.remove("is-hidden");
}

// --- Пейвол (фейк-дор) ---
// Срабатывает на 402: per-IP лимит бесплатных отчётов исчерпан. Клик по
// «оплатить» и оставленный email — сигналы готовности платить, уходят
// событием на сервер (fire-and-forget: сеть не должна ломать UI).

const paywallCard = document.querySelector("#paywall-card");
const paywallPay = document.querySelector("#paywall-pay");
const paywallForm = document.querySelector("#paywall-form");
const paywallEmail = document.querySelector("#paywall-email");
const paywallNote = document.querySelector("#paywall-note");
const paywallDone = document.querySelector("#paywall-done");
const paywallToggle = document.querySelector("#paywall-email-toggle");
const paywallAlt = paywallToggle.closest(".paywall-alt");

let paywallContext = { krs: null, company: null };

function postInterest(payload) {
  return fetch("/interest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch(() => {});
}

function showPaywall(detail, candidate) {
  paywallContext = { krs: candidate.krs, company: candidate.name };
  const price = detail.price_pln ?? 20;
  const pack = detail.pack_size ?? 10;
  for (const el of document.querySelectorAll(".js-paywall-price")) {
    el.textContent = price;
  }
  for (const el of document.querySelectorAll(".js-paywall-pack")) {
    el.textContent = pack;
  }
  paywallPay.classList.remove("is-hidden");
  paywallForm.classList.add("is-hidden");
  paywallAlt.classList.remove("is-hidden");
  paywallDone.classList.add("is-hidden");
  paywallEmail.value = "";
  paywallCard.classList.remove("is-hidden");
  paywallCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function revealPaywallEmailForm(note) {
  paywallNote.textContent = note;
  paywallForm.classList.remove("is-hidden");
  paywallAlt.classList.add("is-hidden");
  paywallEmail.focus();
}

paywallPay.addEventListener("click", () => {
  postInterest({
    action: "pay_click",
    krs: paywallContext.krs,
    company: paywallContext.company,
  });
  paywallPay.classList.add("is-hidden");
  revealPaywallEmailForm(
    "Оплата ещё не подключена — продукт в запуске. Оставьте email: " +
      "сообщим, когда можно будет оплатить, и первый отчёт дадим бесплатно."
  );
});

paywallToggle.addEventListener("click", () => {
  revealPaywallEmailForm("Оставьте email — сообщим, когда запустимся.");
});

paywallForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const email = paywallEmail.value.trim();
  postInterest({
    action: "notify",
    email: email || null,
    krs: paywallContext.krs,
    company: paywallContext.company,
  });
  paywallForm.classList.add("is-hidden");
  paywallDone.classList.remove("is-hidden");
});

// --- Company Resolver (v0) ---

const resolverForm = document.querySelector("#resolver-form");
const resolverQuery = document.querySelector("#resolver-query");
const resolverButton = document.querySelector("#resolver-submit");
const resolverStatus = document.querySelector("#resolver-status");
const resolverResult = document.querySelector("#resolver-result");
const resolverRaw = document.querySelector("#resolver-raw");
const resolverRawOutput = document.querySelector("#resolver-raw-output");
const onboarding = document.querySelector("#onboarding");

// Кнопки-примеры в пустом состоянии: подставляют KRS и сразу ищут
for (const button of document.querySelectorAll(".example-button")) {
  button.addEventListener("click", () => {
    resolverQuery.value = button.dataset.krs;
    resolverForm.requestSubmit();
  });
}

// Контрольные суммы официальных идентификаторов: опечатку ловим сразу,
// не гоняя пользователя в реестр за обманчивым «ничего не найдено».
const NIP_WEIGHTS = [6, 5, 7, 2, 3, 4, 5, 6, 7];
function isValidNip(digits) {
  const sum = NIP_WEIGHTS.reduce((acc, w, i) => acc + w * Number(digits[i]), 0);
  // сумма mod 11 == 10 у настоящих NIP не встречается
  return sum % 11 !== 10 && sum % 11 === Number(digits[9]);
}

const REGON_WEIGHTS = [8, 9, 2, 3, 4, 5, 6, 7];
function isValidRegon(digits) {
  const sum = REGON_WEIGHTS.reduce((acc, w, i) => acc + w * Number(digits[i]), 0);
  // у REGON, в отличие от NIP, сумма mod 11 == 10 значит контрольную цифру 0
  return (sum % 11) % 10 === Number(digits[8]);
}

// Единое поле поиска: тип ввода определяем сами, как gowork.pl.
// NIP и KRS не пересекаются в 10-значном пространстве: NIP не начинается
// с нуля (первые три цифры — код налоговой, все >= 101), а 10-значный KRS
// всегда с ведущими нулями (номера едва перевалили за миллион). KRS без
// ведущих нулей — это <= 8 цифр; 9 или 14 цифр — REGON.
function classifyQuery(raw) {
  const digits = raw
    .replace(/^(?:PL|NIP|KRS|REGON)[\s:.-]*/i, "")
    .replace(/[\s.-]/g, "");
  if (!/^\d+$/.test(digits)) {
    return { kind: "name", payload: { company_name: raw } };
  }
  if (digits.length === 9 || digits.length === 14) {
    // 14-значный REGON — «локальная единица»; базовый номер юр. лица
    // с собственной контрольной цифрой лежит в первых девяти позициях.
    const base = digits.slice(0, 9);
    if (!isValidRegon(base)) {
      return {
        error:
          "Похоже на REGON, но контрольная сумма не сходится — в номере опечатка.",
      };
    }
    return { kind: "regon", payload: { regon: base } };
  }
  if (digits.length === 10) {
    if (digits.startsWith("0")) {
      return { kind: "krs", payload: { krs: digits } };
    }
    if (!isValidNip(digits)) {
      return {
        error:
          "Похоже на NIP, но контрольная сумма не сходится — в номере опечатка.",
      };
    }
    return { kind: "nip", payload: { nip: digits } };
  }
  if (digits.length <= 8) {
    return { kind: "krs", payload: { krs: digits.padStart(10, "0") } };
  }
  return {
    error:
      "Столько цифр не бывает ни у NIP/KRS (10 знаков), ни у REGON (9 или 14). Проверьте номер или введите название.",
  };
}

function showRawResponse(text) {
  resolverRawOutput.textContent = text;
  resolverRaw.classList.remove("is-hidden");
}

function setResolverStatus(message, isError = false) {
  resolverStatus.textContent = message;
  resolverStatus.classList.toggle("is-error", isError);
}

function renderCandidates(candidates) {
  resolverResult.replaceChildren();

  for (const candidate of candidates) {
    const card = document.createElement("article");
    card.className = "candidate-card";

    const header = document.createElement("header");
    const title = document.createElement("h3");
    title.textContent = candidate.name;
    const badges = document.createElement("span");
    badges.className = "badges";
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = candidate.status ?? "unknown";
    badges.append(badge);
    header.append(title, badges);

    // confidence < 1 значит кандидат найден по названию через web search:
    // данные проверены в реестре, но выбор компании подтверждает пользователь.
    const needsConfirmation = candidate.confidence < 1;
    if (needsConfirmation) {
      const confirmBadge = document.createElement("span");
      confirmBadge.className = "badge badge-confirm";
      confirmBadge.textContent = "подтвердите";
      badges.prepend(confirmBadge);
    }

    const details = document.createElement("dl");
    const fields = [
      ["KRS", candidate.krs],
      ["NIP", candidate.nip],
      ["REGON", candidate.regon],
      ["Адрес", candidate.address],
      ["Источник", candidate.source],
      ["Совпадение", `${Math.round(candidate.confidence * 100)}%`],
    ];

    const facts = candidate.facts;
    if (facts) {
      const statements = facts.annual_statements ?? [];
      const statementsText = statements.length
        ? `сдано ${statements.length}, последний: ${facts.last_statement_period}`
        : "не сдавались — риск";
      fields.push(
        ["Регистрация", facts.registration_date],
        ["Капитал", facts.share_capital],
        ["Отчётность", statementsText]
      );
      if ((facts.distress_flags ?? []).length) {
        fields.push(["⚠ Dział 6", facts.distress_flags.join(", ")]);
      }
      if ((facts.arrears_flags ?? []).length) {
        fields.push(["⚠ Dział 4", facts.arrears_flags.join(", ")]);
      }
    }
    for (const [label, value] of fields) {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value ?? "—";
      // Реестровые номера сверяют посимвольно с офертой - моноширинным
      if (["KRS", "NIP", "REGON"].includes(label)) {
        dd.classList.add("mono");
      }
      details.append(dt, dd);
    }

    card.append(header, details);

    if (candidate.krs) {
      const reportButton = document.createElement("button");
      reportButton.type = "button";
      reportButton.className = "candidate-report-button";
      // Явное подтверждение выбора: без клика по конкретному кандидату
      // отчёт по найденной через web search компании не строится.
      reportButton.textContent = needsConfirmation
        ? "Да, это она — построить отчёт"
        : "Построить отчёт";
      reportButton.addEventListener("click", () =>
        buildHealthCheck(candidate, reportButton)
      );
      card.append(reportButton);
    }

    resolverResult.append(card);
  }
}

// --- Сцена ожидания отчёта ---
// Агент работает 30-120 секунд. Этапы совпадают с реальным порядком
// работы бэкенда (одпис уже есть -> финансы адаптером -> отзывы ->
// оси); тайминги переключения - клиентская оценка, поэтому последний
// этап никогда не помечается выполненным до прихода ответа.

const waitingCard = document.querySelector("#waiting-card");
const waitingRadar = document.querySelector("#waiting-radar");
const waitingCompany = document.querySelector("#waiting-company");
const waitingStages = document.querySelector("#waiting-stages");
const waitingElapsed = document.querySelector("#waiting-elapsed");

const WAIT_STAGES = [
  { text: "Читаю одпис KRS — государственный реестр", at: 0 },
  { text: "Читаю сайт компании и свежие новости", at: 4 },
  { text: "Ищу активные вакансии и LinkedIn-сигналы", at: 25 },
  { text: "Собираю пульс-отчёт: изменения, инициативы, потребности", at: 80 },
];

let waitingTimer = null;
// Реальный статус джобы с сервера (queued/running) и момент старта
// рана: этапы сцены ожидания отсчитываются от выхода из очереди.
let currentJobStatus = null;
let waitingStartedAt = 0;

function noteJobStatus(status) {
  if (status === "running" && currentJobStatus !== "running") {
    waitingStartedAt = Date.now();
  }
  currentJobStatus = status;
}

function drawSkeletonRadar() {
  const R = currentRadar();
  waitingRadar.setAttribute("viewBox", `0 0 ${R.width} ${R.height}`);
  waitingRadar.replaceChildren();
  for (const level of [25, 50, 75, 100]) {
    const points = SCORE_AXES.map((_, i) =>
      polarPoint(axisAngle(i), (R.r * level) / 100).join(",")
    ).join(" ");
    waitingRadar.append(svgEl("polygon", { points, class: "radar-grid" }));
  }
  SCORE_AXES.forEach(([, label], i) => {
    const angle = axisAngle(i);
    const [x2, y2] = polarPoint(angle, R.r);
    waitingRadar.append(
      svgEl("line", {
        x1: R.cx,
        y1: R.cy,
        x2,
        y2,
        class: "radar-axis radar-axis-null",
      })
    );
    const [lx, ly] = polarPoint(angle, R.r + R.labelOffset);
    const anchor =
      Math.abs(lx - R.cx) < 6 ? "middle" : lx > R.cx ? "start" : "end";
    const baseline =
      ly < R.cy - 6 ? "auto" : ly > R.cy + 6 ? "hanging" : "middle";
    const text = svgEl("text", {
      x: lx,
      y: ly,
      "text-anchor": anchor,
      "dominant-baseline": baseline,
      class: "radar-label radar-label-null",
    });
    text.textContent = label;
    waitingRadar.append(text);
  });
}

function renderWaitingStages(elapsedSeconds) {
  const items = waitingStages.children;
  WAIT_STAGES.forEach((stage, i) => {
    const next = WAIT_STAGES[i + 1];
    // последний этап закрывает только реальный ответ сервера
    const done = next ? elapsedSeconds >= next.at : false;
    const current = !done && elapsedSeconds >= stage.at;
    const li = items[i];
    li.className = done
      ? "waiting-stage waiting-stage--done"
      : current
        ? "waiting-stage waiting-stage--current"
        : "waiting-stage";
  });
}

function startWaiting(companyName) {
  waitingCompany.textContent = companyName;
  drawSkeletonRadar();

  waitingStages.replaceChildren();
  for (const stage of WAIT_STAGES) {
    const li = document.createElement("li");
    li.className = "waiting-stage";
    li.textContent = stage.text;
    waitingStages.append(li);
  }

  currentJobStatus = null;
  waitingStartedAt = Date.now();
  const tick = () => {
    const elapsed = Math.round((Date.now() - waitingStartedAt) / 1000);
    // Пока джоба в очереди (все воркеры заняты), этапы не идут -
    // честно показываем ожидание вместо фальшивого прогресса.
    if (currentJobStatus === "queued") {
      waitingElapsed.textContent = `в очереди ${elapsed} с — ждём свободного воркера`;
      renderWaitingStages(0);
      return;
    }
    waitingElapsed.textContent = `идёт ${elapsed} с · обычно 30–120 секунд`;
    renderWaitingStages(elapsed);
  };
  tick();
  waitingTimer = setInterval(tick, 1000);

  waitingCard.classList.remove("is-hidden");
  waitingCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function stopWaiting() {
  if (waitingTimer !== null) {
    clearInterval(waitingTimer);
    waitingTimer = null;
  }
  waitingCard.classList.add("is-hidden");
}

// Пересечение брейкпоинта (поворот телефона, ресайз окна) — радар
// перерисовывается в подходящей геометрии, какой бы из двух ни был виден.
compactRadarQuery.addEventListener("change", () => {
  if (lastScores) {
    drawRadar(lastScores);
  }
  if (!waitingCard.classList.contains("is-hidden")) {
    drawSkeletonRadar();
  }
});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Поллинг статуса джобы. Интервал 2.5с - компромисс: чаще незачем
// (ран идёт минуты), реже - заметная задержка показа готового отчёта.
// Дедлайн - предохранитель от вечного спиннера, если джоба зависла.
const JOB_POLL_INTERVAL_MS = 2500;
const JOB_POLL_DEADLINE_MS = 10 * 60 * 1000;

async function pollJob(statusUrl) {
  const deadline = Date.now() + JOB_POLL_DEADLINE_MS;
  while (Date.now() < deadline) {
    const response = await fetch(statusUrl);

    if (response.status === 404) {
      // Стор джобов живёт в памяти процесса: 404 на поллинге значит,
      // что сервер перезапустился и джоба потерялась.
      throw new Error(
        "Джоба потерялась (сервер перезапустился). Запустите отчёт ещё раз."
      );
    }
    if (!response.ok) {
      throw new Error(`Ошибка статуса джобы (HTTP ${response.status})`);
    }

    const data = await response.json();
    noteJobStatus(data.status);

    if (data.status === "succeeded") {
      return data.result;
    }
    if (data.status === "failed") {
      throw new Error(`Ошибка отчёта: ${data.error ?? "неизвестная ошибка"}`);
    }

    await sleep(JOB_POLL_INTERVAL_MS);
  }
  throw new Error(
    "Отчёт строится дольше 10 минут — что-то пошло не так. Попробуйте позже."
  );
}

async function buildHealthCheck(candidate, buttonEl) {
  const idleLabel = buttonEl.textContent;
  buttonEl.disabled = true;
  buttonEl.textContent = "Строю отчёт...";
  setResolverStatus("Строю отчёт...");
  resultCard.classList.add("is-hidden");
  paywallCard.classList.add("is-hidden");
  startWaiting(candidate.name);

  try {
    const response = await fetch(
      `/companies/${encodeURIComponent(candidate.krs)}/health-check`,
      { method: "POST" }
    );

    const rawText = await response.text();
    let data = null;
    try {
      data = JSON.parse(rawText);
    } catch {
      // сервер вернул не-JSON
    }

    // 402 — исчерпан per-IP лимит: показываем пейвол, а не ошибку.
    if (response.status === 402 && data?.detail?.paywall) {
      showPaywall(data.detail, candidate);
      setResolverStatus("");
      return;
    }

    // Сабмит возвращает не отчёт, а квитанцию: 202 + адрес статуса.
    if (response.status !== 202 || data === null) {
      showRawResponse(`HTTP ${response.status}\n${rawText.slice(0, 2000)}`);
      const detail =
        data && data.detail ? JSON.stringify(data.detail) : rawText.slice(0, 300);
      throw new Error(`Ошибка отчёта (HTTP ${response.status}): ${detail}`);
    }

    const result = await pollJob(data.status_url);

    showResult({
      company: result.company.name,
      report_url: result.report_url,
      evidence_url: result.evidence_url,
      report: result.report,
      scores: result.scores,
    });
    setResolverStatus(`Отчёт готов: ${result.report_file}`);
    resultCard.scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    setResolverStatus(error.message, true);
  } finally {
    stopWaiting();
    buttonEl.disabled = false;
    buttonEl.textContent = idleLabel;
  }
}

resolverForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const raw = resolverQuery.value.trim();
  if (!raw) {
    setResolverStatus("Укажите название компании, NIP, KRS или REGON.", true);
    return;
  }

  const parsed = classifyQuery(raw);
  if (parsed.error) {
    setResolverStatus(parsed.error, true);
    return;
  }
  const payload = {
    company_name: null,
    nip: null,
    krs: null,
    regon: null,
    ...parsed.payload,
  };

  // Поиск по названию идёт через web search + верификацию в реестрах,
  // это заметно дольше прямого lookup по номеру. В статусе показываем,
  // как распознали ввод, — вместо отдельных полей это единственная подсказка.
  const isNameSearch = parsed.kind === "name";
  resolverButton.disabled = true;
  setResolverStatus(
    isNameSearch
      ? "Ищу по названию: кандидаты из веба, каждого проверяю по официальным реестрам (10–30 секунд)..."
      : `Ищу по ${parsed.kind.toUpperCase()} ${payload.nip ?? payload.krs ?? payload.regon}...`
  );
  resolverResult.replaceChildren();
  resolverRaw.classList.add("is-hidden");
  resolverRawOutput.textContent = "";
  onboarding.classList.add("is-hidden");

  try {
    const response = await fetch("/companies/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const rawText = await response.text();
    let data = null;
    let pretty = rawText;
    try {
      data = JSON.parse(rawText);
      pretty = JSON.stringify(data, null, 2);
    } catch {
      // сервер вернул не-JSON - показываем как есть
    }
    showRawResponse(`HTTP ${response.status}\n${pretty}`);

    if (!response.ok || data === null) {
      const detail =
        data && data.detail ? JSON.stringify(data.detail) : rawText.slice(0, 300);
      throw new Error(`Ошибка запроса (HTTP ${response.status}): ${detail}`);
    }

    if (!data.candidates.length) {
      setResolverStatus(
        isNameSearch
          ? "Не нашлось подтверждённых кандидатов. Уточните название (как в документах) или укажите NIP/KRS."
          : "Ничего не найдено по официальным реестрам."
      );
    } else if (isNameSearch) {
      setResolverStatus(
        `Найдено кандидатов: ${data.candidates.length}. Проверьте реквизиты и подтвердите свою компанию.`
      );
    } else {
      setResolverStatus(`Найдено кандидатов: ${data.candidates.length}.`);
    }
    renderCandidates(data.candidates);
  } catch (error) {
    setResolverStatus(error.message, true);
    if (resolverRaw.classList.contains("is-hidden")) {
      showRawResponse(String(error));
    }
  } finally {
    resolverButton.disabled = false;
  }
});
