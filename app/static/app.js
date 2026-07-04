const form = document.querySelector("#analyze-form");
const input = document.querySelector("#company-name");
const button = document.querySelector("#submit-button");
const statusBox = document.querySelector("#status");
const resultCard = document.querySelector("#result-card");
const resultCompany = document.querySelector("#result-company");
const reportLink = document.querySelector("#report-link");
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
