import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_tavily import TavilySearch
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel

load_dotenv()

# Инициализация FastAPI
app = FastAPI(title="Toxic Scanner API")


# Схема входящего JSON-запроса
class CompanyRequest(BaseModel):
    company_name: str


# --- ИНСТРУМЕНТЫ АГЕНТА ---

# 1. Поиск (Tavily)
search_tool = TavilySearch(max_results=3)


# 2. Чтение сайтов (Jina Reader)
@tool
def extract_website_text(url: str) -> str:
    """Используй это, чтобы прочитать полный текст веб-страницы."""
    headers = {"Accept": "text/markdown"}
    try:
        # Jina помогает обойти защиты и возвращает чистый текст
        response = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=15)
        if response.status_code == 200:
            return response.text[:15000]  # Защита от переполнения контекста
    except Exception as e:
        return f"Ошибка при доступе к {url}: {str(e)}"
    return f"Ошибка при чтении сайта: {response.status_code}"


tools = [search_tool, extract_website_text]

# --- НАСТРОЙКА LLM И АГЕНТА ---

# Используем Gemini Flash Lite (быстрая и дешевая модель)
llm = ChatGoogleGenerativeAI(model="gemini-3.1-flash-lite", temperature=1.5)

system_prompt = """
Ты — старший аналитик корпоративной прозрачности.
Используй поиск и чтение сайтов, чтобы собрать досье на работодателя.
Формат ответа:
- 📊 Индекс токсичности (0-100%)
- 🚩 Главные проблемы (Красные флаги)
- 🏆 Плюсы
- 🤖 Подозрительные отзывы (Вероятность накрутки HR)
"""

# Создаем агента, который умеет сам вызывать инструменты
agent_executor = create_react_agent(llm, tools, prompt=system_prompt)

# --- ЭНДПОИНТ ---


@app.post("/analyze")
async def analyze_company_endpoint(request: CompanyRequest):
    try:
        # Формируем задачу для агента
        messages = {
            "messages": [
                HumanMessage(
                    content=f"Собери отзывы о компании {request.company_name} (Польша)"
                )
            ]
        }

        # Агент уходит думать, искать и читать сайты
        result = agent_executor.invoke(messages)

        # Забираем финальный текст ответа
        final_answer = result["messages"][-1].content

        return {
            "company": request.company_name,
            "status": "success",
            "report": final_answer,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
