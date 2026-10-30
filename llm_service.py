"""Сервис взаимодействия с языковой моделью.

В этом модуле сосредоточены: работа с конфигурацией языковой модели
(читается и проверяется в модуле config), параметры генерации,
системный промпт и функция получения краткого содержания статьи.
"""

import base64
import time
import uuid

import requests

# Конфигурация (тип API, URL, модель, ключ доступа и т. д.) загружается
# и проверяется в модуле config
from config import (
    GIGACHAT_OAUTH_URL,
    LLM_API_TOKEN,
    LLM_API_TYPE,
    LLM_API_URL,
    LLM_MODEL,
    MAX_LLM_INPUT_CHARS,
    ConfigError,
)

# Для совместимости: ошибка конфигурации LLM — это ConfigError из config
LLMConfigError = ConfigError

# Ограничение размера сводки
SUMMARY_MAX_CHARS = 4000

# Параметры генерации ответа модели
TEMPERATURE = 0.3
REQUEST_TIMEOUT = 120

# Кэш access_token GigaChat: истёкший токен перевыпускается автоматически
_gigachat_token_cache = {"access_token": None, "expires_at": 0}

# Системный промпт для составления краткого содержания статьи.
# Плейсхолдер {max_chars} подставляется при вызове модели.
SYSTEM_PROMPT = """Ты — ассистент, который составляет краткое содержание научных и учебных статей.

Составь краткое содержание предложенной статьи на русском языке объёмом не более {max_chars} символов.
Выдели основную идею, ключевые тезисы и выводы. Отвечай только текстом краткого содержания."

Независимо от языка исходной статьи, отвечай по-русски.
"""



class LLMRequestError(Exception):
    """Ошибка обращения к API языковой модели."""


def get_gigachat_token() -> str:
    """Возвращает действующий access_token GigaChat, при необходимости получая новый."""
    cached = _gigachat_token_cache
    if cached["access_token"] and time.time() < cached["expires_at"] - 60:
        return cached["access_token"]

    # Наличие ключа уже проверено в config.py
    auth_header = base64.b64encode(LLM_API_TOKEN.encode()).decode()
    try:
        response = requests.post(
            GIGACHAT_OAUTH_URL,
            headers={
                "Authorization": f"Basic {auth_header}",
                "RqUID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data="scope=GIGACHAT_API_PERS",
            timeout=30,
        )
        response.raise_for_status()  # выбрасывает HTTPError при неудаче
    except requests.RequestException as exc:
        raise LLMRequestError(str(exc)) from exc

    data = response.json()
    cached["access_token"] = data["access_token"]
    cached["expires_at"] = int(data.get("expires_at", 0))
    return cached["access_token"]


def ask_llm(system_prompt: str, user_prompt: str) -> str:
    """Отправляет запрос к языковой модели через выбранный API и возвращает ответ."""
    url = LLM_API_URL.rstrip("/") + "/chat/completions"

    if LLM_API_TYPE == "gigachat":
        headers = {
            "Authorization": f"Bearer {get_gigachat_token()}",
            "Content-Type": "application/json",
            "X-Request-Id": str(uuid.uuid4()),
        }
    else:
        headers = {
            "Authorization": f"Bearer {LLM_API_TOKEN}",
            "Content-Type": "application/json",
        }

    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": TEMPERATURE,
    }

    try:
        response = requests.post(
            url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise LLMRequestError(str(exc)) from exc

    content = response.json()["choices"][0]["message"]["content"]
    return content.strip()


def summarize_article(article_text: str) -> tuple[str, bool]:
    """Формирует краткое содержание статьи через языковую модель.

    Возвращает кортеж (краткое_содержание, был_ли_обрезан_исходный_текст).
    """
    truncated = False
    if len(article_text) > MAX_LLM_INPUT_CHARS:
        article_text = article_text[:MAX_LLM_INPUT_CHARS]
        truncated = True

    system_prompt = SYSTEM_PROMPT.format(max_chars=SUMMARY_MAX_CHARS)
    result = ask_llm(system_prompt, "Текст статьи:\n\n" + article_text)
    return result[:SUMMARY_MAX_CHARS], truncated
