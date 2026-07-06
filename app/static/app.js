const form = document.querySelector("#analyze-form");
const input = document.querySelector("#company-name");
const button = document.querySelector("#submit-button");
const statusBox = document.querySelector("#status");
const resultCard = document.querySelector("#result-card");
const resultCompany = document.querySelector("#result-company");
const reportLink = document.querySelector("#report-link");
const evidenceLink = document.querySelector("#evidence-link");
const reportOutput = document.querySelector("#report-output");

function setStatus(message, isError = false) {
  statusBox.textContent = message;
  statusBox.classList.toggle("is-error", isError);
}

function setLoading(isLoading) {
  button.disabled = isLoading;
  input.disabled = isLoading;
  button.textContent = isLoading ? "Проверяю..." : "Проверить";
}

function renderReport(value) {
  if (typeof value === "string") {
    return value;
  }

  if (value === null || value === undefined) {
    return "";
  }

  return JSON.stringify(value, null, 2);
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
  reportOutput.textContent = renderReport(data.report ?? data.report_preview);
  resultCard.classList.remove("is-hidden");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const companyName = input.value.trim();
  if (!companyName) {
    setStatus("Введите название компании.", true);
    return;
  }

  setLoading(true);
  setStatus("Идёт анализ. Обычно это занимает от 30 секунд до пары минут.");
  resultCard.classList.add("is-hidden");

  try {
    const response = await fetch("/analyze", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        company_name: companyName,
        include_report: true,
      }),
    });

    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.detail || "Не удалось выполнить анализ.");
    }

    showResult(data);
    setStatus(`Готово. Отчёт сохранён: ${data.report_file}`);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    setLoading(false);
  }
});

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
