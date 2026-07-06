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
}

// --- Звезда качества (радар, 5 осей) ---
// Ось с value=null - честное "нет данных": серый пунктир с подписью
// "н/д", в полигон значений не входит и не читается как низкая оценка.

const scoresPanel = document.querySelector("#scores-panel");
const radarChart = document.querySelector("#radar-chart");
const scoresList = document.querySelector("#scores-list");

const SCORE_AXES = [
  ["reliability", "Надёжность"],
  ["finances", "Финансы"],
  ["people", "Люди"],
  ["transparency", "Прозрачность"],
  ["future_readiness", "Будущее"],
];

// cx с запасом под боковые подписи ("Прозрачность", "Будущее — н/д"):
// они анкорятся снаружи осей и не должны вылезать за viewBox 420x300.
const RADAR = { cx: 210, cy: 150, r: 95 };

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
    text.textContent = value === null ? `${label} — н/д` : label;
    radarChart.append(text);

    if (value !== null) {
      valuePoints.push(polarPoint(angle, (RADAR.r * value) / 100));
    }
  });

  const pointsAttr = valuePoints.map((p) => p.join(",")).join(" ");
  if (valuePoints.length >= 3) {
    radarChart.append(
      svgEl("polygon", { points: pointsAttr, class: "radar-value" })
    );
  } else if (valuePoints.length === 2) {
    radarChart.append(
      svgEl("polyline", { points: pointsAttr, class: "radar-value" })
    );
  }
  for (const [px, py] of valuePoints) {
    radarChart.append(svgEl("circle", { cx: px, cy: py, r: 4, class: "radar-dot" }));
  }
}

function renderScoresList(scores) {
  scoresList.replaceChildren();

  for (const [key, label] of SCORE_AXES) {
    const axis = scores[key] ?? {};
    const value = axisValue(axis);

    const item = document.createElement("details");
    item.className = "score-item";

    const summary = document.createElement("summary");
    const name = document.createElement("span");
    name.className = "score-name";
    name.textContent = label;
    const valueEl = document.createElement("span");
    valueEl.className =
      value === null ? "score-value score-value-nd" : "score-value";
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

// --- Company Resolver (v0) ---

const resolverForm = document.querySelector("#resolver-form");
const resolverNip = document.querySelector("#resolver-nip");
const resolverKrs = document.querySelector("#resolver-krs");
const resolverButton = document.querySelector("#resolver-submit");
const resolverStatus = document.querySelector("#resolver-status");
const resolverResult = document.querySelector("#resolver-result");
const resolverRaw = document.querySelector("#resolver-raw");
const resolverRawOutput = document.querySelector("#resolver-raw-output");

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
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = candidate.status ?? "unknown";
    header.append(title, badge);

    const details = document.createElement("dl");
    const fields = [
      ["KRS", candidate.krs],
      ["NIP", candidate.nip],
      ["REGON", candidate.regon],
      ["Адрес", candidate.address],
      ["Источник", candidate.source],
      ["Confidence", candidate.confidence],
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
      details.append(dt, dd);
    }

    card.append(header, details);

    if (candidate.krs) {
      const reportButton = document.createElement("button");
      reportButton.type = "button";
      reportButton.className = "candidate-report-button";
      reportButton.textContent = "Построить отчёт";
      reportButton.addEventListener("click", () =>
        buildHealthCheck(candidate, reportButton)
      );
      card.append(reportButton);
    }

    resolverResult.append(card);
  }
}

async function buildHealthCheck(candidate, buttonEl) {
  buttonEl.disabled = true;
  buttonEl.textContent = "Строю отчёт...";
  setResolverStatus(
    "Строю отчёт. Обычно это занимает от 30 секунд до пары минут."
  );
  resultCard.classList.add("is-hidden");

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
    buttonEl.disabled = false;
    buttonEl.textContent = "Построить отчёт";
  }
}

resolverForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  const payload = {
    nip: resolverNip.value.trim() || null,
    krs: resolverKrs.value.trim() || null,
  };

  if (!payload.nip && !payload.krs) {
    setResolverStatus("Укажите NIP или KRS — по одному названию v0 не ищет.", true);
    return;
  }

  resolverButton.disabled = true;
  setResolverStatus("Ищу...");
  resolverResult.replaceChildren();
  resolverRaw.classList.add("is-hidden");
  resolverRawOutput.textContent = "";

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

    setResolverStatus(
      data.candidates.length
        ? `Найдено кандидатов: ${data.candidates.length}.`
        : "Ничего не найдено по официальным реестрам."
    );
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
