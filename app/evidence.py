"""Evidence Store - файлы отчётов и сырых данных (см. README).

Пока это просто файлы в reports/: Markdown-отчёт для человека и
evidence JSON, по которому можно проверить, на каких данных отчёт
построен. База для истории отчётов - в беклоге.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

# Локально пишем в ./reports; на Cloud Run ФС эфемерная, поэтому
# каталог задаётся через REPORTS_DIR (в деплое — /tmp/reports).
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", "reports"))


def slugify_filename(value: str) -> str:
    slug_chars: list[str] = []
    for char in value.lower():
        if char.isalnum():
            slug_chars.append(char)
        elif slug_chars and slug_chars[-1] != "-":
            slug_chars.append("-")

    slug = "".join(slug_chars).strip("-")
    return (slug or "company")[:80]


def save_markdown_report(company_name: str, report: str) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = REPORTS_DIR / f"{timestamp}-{slugify_filename(company_name)}.md"
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    report_path.write_text(
        f"# Отчет по компании: {company_name}\n\n"
        f"Дата создания: {created_at}\n\n"
        f"{report}\n",
        encoding="utf-8",
    )
    return report_path


def save_evidence_json(company_name: str, evidence: dict) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence_path = (
        REPORTS_DIR / f"{timestamp}-{slugify_filename(company_name)}-evidence.json"
    )
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return evidence_path
