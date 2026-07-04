# Используем минимальный образ
FROM python:3.11-slim

# Ставим uv из официального образа
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uv

WORKDIR /app

# Копируем файлы проекта
COPY pyproject.toml uv.lock ./

# Устанавливаем зависимости максимально быстро (слои Docker любят uv)
RUN uv sync --frozen

# Копируем остальной код
COPY . .

# Запуск
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]