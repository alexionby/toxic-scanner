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

// cx с запасом под боковые подписи (слева "ПРОЗРАЧНОСТЬ 100" ~122px):
// они анкорятся снаружи осей и не должны вылезать за viewBox 460x300.
// cx/cy держим в центре viewBox - от него расцветает анимация полигона.
const RADAR = { cx: 230, cy: 150, r: 95 };

function axisValue(axis) {
  return typeof axis?.value === "number"
    ? Math.max(0, Math.min(100, axis.value))
    : null;
}

function polarPoint(angleDeg, radius) {
  const rad = (angleDeg * Math.PI) / 180;
  return [RADAR.cx + radius * Math.cos(rad), RADAR.cy + radius * Math.sin(rad)];
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
  radarChart.replaceChildren();

  for (const level of [25, 50, 75, 100]) {
    const points = SCORE_AXES.map((_, i) =>
      polarPoint(axisAngle(i), (RADAR.r * level) / 100).join(",")
    ).join(" ");
    radarChart.append(svgEl("polygon", { points, class: "radar-grid" }));
  }

  const valuePoints = [];
  SCORE_AXES.forEach(([key, label], i) => {
    const value = axisValue(scores[key]);
    const band = scoreBand(value);
    const angle = axisAngle(i);

    const [x2, y2] = polarPoint(angle, RADAR.r);
    radarChart.append(
      svgEl("line", {
        x1: RADAR.cx,
        y1: RADAR.cy,
        x2,
        y2,
        class: value === null ? "radar-axis radar-axis-null" : "radar-axis",
      })
    );

    // Подпись оси несёт и значение: имя + число в цвете полосы,
    // чтобы звезда читалась без списка справа.
    const [lx, ly] = polarPoint(angle, RADAR.r + 16);
    const anchor =
      Math.abs(lx - RADAR.cx) < 6 ? "middle" : lx > RADAR.cx ? "start" : "end";
    const baseline =
      ly < RADAR.cy - 6 ? "auto" : ly > RADAR.cy + 6 ? "hanging" : "middle";
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
      valuePoints.push({ point: polarPoint(angle, (RADAR.r * value) / 100), band });
    }
  });

  const pointsAttr = valuePoints.map((v) => v.point.join(",")).join(" ");
  if (valuePoints.length >= 3) {
    radarChart.append(
      svgEl("polygon", { points: pointsAttr, class: "radar-value" })
    );
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

function renderScores(scores) {
  if (!scores || typeof scores !== "object") {
    scoresPanel.classList.add("is-hidden");
    return;
  }
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
const resolverName = document.querySelector("#resolver-name");
const resolverNip = document.querySelector("#resolver-nip");
const resolverKrs = document.querySelector("#resolver-krs");
const resolverButton = document.querySelector("#resolver-submit");
const resolverStatus = document.querySelector("#resolver-status");
const resolverResult = document.querySelector("#resolver-result");
const resolverRaw = document.querySelector("#resolver-raw");
const resolverRawOutput = document.querySelector("#resolver-raw-output");
const onboarding = document.querySelector("#onboarding");

// Кнопки-примеры в пустом состоянии: подставляют KRS и сразу ищут
for (const button of document.querySelectorAll(".example-button")) {
  button.addEventListener("click", () => {
    resolverName.value = "";
    resolverNip.value = "";
    resolverKrs.value = button.dataset.krs;
    resolverForm.requestSubmit();
  });
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
  { text: "Собираю финансовые показатели с агрегаторов", at: 4 },
  { text: "Ищу отзывы сотрудников: GoWork, Reddit", at: 25 },
  { text: "Считаю оси звезды и собираю отчёт", at: 80 },
];

let waitingTimer = null;

function drawSkeletonRadar() {
  waitingRadar.replaceChildren();
  for (const level of [25, 50, 75, 100]) {
    const points = SCORE_AXES.map((_, i) =>
      polarPoint(axisAngle(i), (RADAR.r * level) / 100).join(",")
    ).join(" ");
    waitingRadar.append(svgEl("polygon", { points, class: "radar-grid" }));
  }
  SCORE_AXES.forEach(([, label], i) => {
    const angle = axisAngle(i);
    const [x2, y2] = polarPoint(angle, RADAR.r);
    waitingRadar.append(
      svgEl("line", {
        x1: RADAR.cx,
        y1: RADAR.cy,
        x2,
        y2,
        class: "radar-axis radar-axis-null",
      })
    );
    const [lx, ly] = polarPoint(angle, RADAR.r + 16);
    const anchor =
      Math.abs(lx - RADAR.cx) < 6 ? "middle" : lx > RADAR.cx ? "start" : "end";
    const baseline =
      ly < RADAR.cy - 6 ? "auto" : ly > RADAR.cy + 6 ? "hanging" : "middle";
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

  const startedAt = Date.now();
  const tick = () => {
    const elapsed = Math.round((Date.now() - startedAt) / 1000);
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

    if (!response.ok || data === null) {
      showRawResponse(`HTTP ${response.status}\n${rawText.slice(0, 2000)}`);
      const detail =
        data && data.detail ? JSON.stringify(data.detail) : rawText.slice(0, 300);
      throw new Error(`Ошибка отчёта (HTTP ${response.status}): ${detail}`);
    }

    showResult({
      company: data.company.name,
      report_url: data.report_url,
      evidence_url: data.evidence_url,
      report: data.report,
      scores: data.scores,
    });
    setResolverStatus(`Отчёт готов: ${data.report_file}`);
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

  const payload = {
    company_name: resolverName.value.trim() || null,
    nip: resolverNip.value.trim() || null,
    krs: resolverKrs.value.trim() || null,
  };

  if (!payload.company_name && !payload.nip && !payload.krs) {
    setResolverStatus("Укажите название компании, NIP или KRS.", true);
    return;
  }

  // Поиск по названию идёт через web search + верификацию в реестрах,
  // это заметно дольше прямого lookup по номеру.
  const isNameSearch = !payload.nip && !payload.krs;
  resolverButton.disabled = true;
  setResolverStatus(
    isNameSearch
      ? "Ищу кандидатов в вебе и проверяю каждого по официальным реестрам (10–30 секунд)..."
      : "Ищу..."
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
