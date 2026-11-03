"""Сервис взаимодействия с языковой моделью.

В этом модуле сосредоточены: работа с конфигурацией языковой модели
(читается и проверяется в модуле config), параметры генерации,
системный промпт и функция получения краткого содержания статьи.
"""

import base64
import logging
import re
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

# Количество вопросов, генерируемых для экзамена по статье
EXAM_QUESTION_COUNT = 5

# Параметры генерации ответа модели
TEMPERATURE = 0.3
REQUEST_TIMEOUT = 120

# Кэш access_token GigaChat: истёкший токен перевыпускается автоматически
_gigachat_token_cache = {"access_token": None, "expires_at": 0}

logger = logging.getLogger(__name__)


def _truncate_article_text(article_text: str) -> tuple[str, bool]:
    """Обрезает текст статьи до MAX_LLM_INPUT_CHARS при необходимости.

    Возвращает кортеж (текст_после_обрезки, был_ли_обрезан_текст).
    При обрезке в лог пишется исходная и итоговая длина текста.
    """
    if len(article_text) > MAX_LLM_INPUT_CHARS:
        original_length = len(article_text)
        article_text = article_text[:MAX_LLM_INPUT_CHARS]
        truncated = True
        logger.warning(
            "Текст статьи обрезан для передачи в LLM: длина %d символов, "
            "обрезан до %d символов (MAX_LLM_INPUT_CHARS)",
            original_length,
            len(article_text),
        )
    else:
        truncated = False
    return article_text, truncated

# Системный промпт для составления краткого содержания статьи.
# Плейсхолдер {max_chars} подставляется при вызове модели.
SYSTEM_PROMPT = """Ты — ассистент, который составляет краткое содержание научных и учебных статей.

Составь краткое содержание предложенной статьи на русском языке объёмом не более {max_chars} символов.
Выдели основную идею, ключевые тезисы и выводы. Отвечай только текстом краткого содержания."

Независимо от языка исходной статьи, отвечай по-русски.
"""

# Системный промпт для оценки ответов студента на экзаменационные вопросы.
GRADE_SYSTEM_PROMPT = """Ты — строгий, но справедливый преподаватель, который проверяет ответы студента на экзаменационные вопросы по статье.

Тебе даны текст статьи, вопросы к ней и ответы студента на эти вопросы. Оцени каждый ответ студента по шкале от 1 до 5, где:
5 — полный и правильный ответ, демонстрирующий понимание материала статьи;
4 — в целом правильный ответ с незначительными неточностями или пробелами;
3 — частично правильный ответ, содержащий существенные пробелы;
2 — ответ в основном неверный, но содержащий отдельные правильные элементы;
1 — ответ отсутствует по смыслу или полностью неверен.

Оценивай каждый ответ на основании текста статьи.

Строго соблюдай формат вывода — для каждого вопроса используй маркер [ВОПРОС N] и строку «Оценка: X», где N — номер вопроса, а X — целое число от 1 до 5:

[ВОПРОС 1]
Оценка: 4

Не добавляй ничего сверх оценок: никаких комментариев, объяснений или выводов.

Независимо от языка исходной статьи, выводи только оценки в указанном формате.
"""

# Системный промпт для генерации экзаменационных вопросов по статье.
# Плейсхолдер {count} подставляется при вызове модели.
EXAM_SYSTEM_PROMPT = """Ты — опытный преподаватель, который составляет экзаменационные вопросы для проверки усвоения материала статьи.

Прочитай текст статьи и сгенерируй ровно {count} вопросов на усвоение материала этой статьи с правильными ответами на них.

Каждый вопрос должен проверять понимание существенных идей, фактов или выводов статьи, а ответ должен быть кратким, но содержательным (2–4 предложения).

Строго соблюдай формат вывода — для каждого вопроса используй маркеры [ВОПРОС] и [ОТВЕТ]:

[ВОПРОС]
Текст вопроса
[ОТВЕТ]
Текст правильного ответа

Не добавляй ничего сверх вопросов и ответов: никаких заголовков, вступлений или нумерации вне маркеров.

Независимо от языка исходной статьи, формулируй вопросы и ответы по-русски.
"""

# Требования к формату вывода при использовании переопределённого промпта
# генерации вопросов (см. ArticleType.exam_prompt). Добавляются к промпту,
# чтобы ответ модели всегда можно было разобрать. Плейсхолдер {count}
# подставляется при вызове модели.
EXAM_FORMAT_REQUIREMENTS = """
Сгенерируй ровно {count} вопросов на усвоение материала статьи с правильными ответами на них.

Строго соблюдай формат вывода — для каждого вопроса используй маркеры [ВОПРОС] и [ОТВЕТ]:

[ВОПРОС]
Текст вопроса
[ОТВЕТ]
Текст правильного ответа

Не добавляй ничего сверх вопросов и ответов: никаких заголовков, вступлений или нумерации вне маркеров.

Независимо от языка исходной статьи, формулируй вопросы и ответы по-русски.
"""

# Требования к формату вывода при использовании переопределённого промпта
# проверки ответов (см. ArticleType.grade_prompt).
GRADE_FORMAT_REQUIREMENTS = """
Оцени каждый ответ студента по шкале от 1 до 5, где 5 — полный и правильный ответ, а 1 — полностью неверный или отсутствующий ответ. Оценивай каждый ответ на основании текста статьи.

Строго соблюдай формат вывода — для каждого вопроса используй маркер [ВОПРОС N] и строку «Оценка: X», где N — номер вопроса, а X — целое число от 1 до 5:

[ВОПРОС 1]
Оценка: 4

Не добавляй ничего сверх оценок: никаких комментариев, объяснений или выводов.

Независимо от языка исходной статьи, выводи только оценки в указанном формате.
"""

# Требования при использовании переопределённого промпта составления
# краткого содержания (см. ArticleType.summary_prompt). Плейсхолдер
# {max_chars} подставляется при вызове модели.
SUMMARY_FORMAT_REQUIREMENTS = """
Объём краткого содержания — не более {max_chars} символов. Отвечай только текстом краткого содержания, без вступлений и комментариев. Независимо от языка исходной статьи, отвечай по-русски.
"""


def _resolve_system_prompt(
    default_prompt: str,
    custom_prompt: str | None,
    format_requirements: str | None = None,
) -> str:
    """Выбирает системный промпт для задачи.

    Если задан непустой custom_prompt (переопределение из типа статьи),
    используется он; при необходимости к нему добавляются обязательные
    требования к формату вывода, чтобы ответ модели можно было разобрать.
    Иначе используется default_prompt.
    """
    custom = (custom_prompt or "").strip()
    if not custom:
        return default_prompt
    if format_requirements:
        custom = custom.rstrip() + "\n\n" + format_requirements
    return custom



class LLMRequestError(Exception):
    """Ошибка обращения к API языковой модели."""


def get_gigachat_token() -> str:
    """Возвращает действующий access_token GigaChat, при необходимости получая новый."""
    cached = _gigachat_token_cache
    if cached["access_token"] and time.time() < cached["expires_at"] - 60:
        return cached["access_token"]

    # Наличие ключа уже проверено в config.py
    auth_header = LLM_API_TOKEN
    try:
        response = requests.post(
            GIGACHAT_OAUTH_URL,
            headers={
                "Authorization": f"Basic {auth_header}",
                "RqUID": str(uuid.uuid4()),
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={'scope': "GIGACHAT_API_PERS"},
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


def summarize_article(
    article_text: str,
    system_prompt: str | None = None,
) -> tuple[str, bool]:
    """Формирует краткое содержание статьи через языковую модель.

    system_prompt — необязательное переопределение системного промпта
    (из типа статьи). Если оно не задано или пустое, используется промпт
    по умолчанию.

    Возвращает кортеж (краткое_содержание, был_ли_обрезан_исходный_текст).
    """
    article_text, truncated = _truncate_article_text(article_text)

    default_prompt = SYSTEM_PROMPT.format(max_chars=SUMMARY_MAX_CHARS)
    final_prompt = _resolve_system_prompt(
        default_prompt, system_prompt, SUMMARY_FORMAT_REQUIREMENTS
    )
    result = ask_llm(final_prompt, "Текст статьи:\n\n" + article_text)
    return result[:SUMMARY_MAX_CHARS], truncated


def _parse_questions(raw_response: str) -> list[tuple[str, str]]:
    """Разбирает ответ модели в список пар (вопрос, ответ).

    Ожидается формат с маркерами [ВОПРОС] и [ОТВЕТ] (см. EXAM_SYSTEM_PROMPT).
    Если модель нарушила формат, выбрасывается LLMRequestError.
    """
    pattern = re.compile(
        r"\[ВОПРОС\]\s*(.*?)\s*\[ОТВЕТ\]\s*(.*?)(?=\[ВОПРОС\]|$)",
        re.DOTALL,
    )
    pairs = []
    for match in pattern.finditer(raw_response):
        question = match.group(1).strip()
        answer = match.group(2).strip()
        if question and answer:
            pairs.append((question, answer))
    if not pairs:
        raise LLMRequestError(
            "Модель вернула ответ в неожиданном формате: не удалось "
            "выделить вопросы и ответы. Попробуйте повторить запрос."
        )
    return pairs


def generate_exam_questions(
    article_text: str,
    count: int = EXAM_QUESTION_COUNT,
    system_prompt: str | None = None,
) -> tuple[list[tuple[str, str]], bool]:
    """Генерирует экзаменационные вопросы и ответы на статью.

    system_prompt — необязательное переопределение системного промпта
    (из типа статьи). Если оно не задано или пустое, используется промпт
    по умолчанию.

    Возвращает кортеж (список пар (вопрос, ответ), был_ли_обрезан_текст).
    """
    article_text, truncated = _truncate_article_text(article_text)

    default_prompt = EXAM_SYSTEM_PROMPT.format(count=count)
    final_prompt = _resolve_system_prompt(
        default_prompt, system_prompt, EXAM_FORMAT_REQUIREMENTS.format(count=count)
    )
    result = ask_llm(final_prompt, "Текст статьи:\n\n" + article_text)
    pairs = _parse_questions(result)
    return pairs, truncated


def _parse_grades(raw_response: str, expected_count: int) -> list[int]:
    """Разбирает ответ модели в список оценок за каждый вопрос.

    Ожидается формат с маркерами [ВОПРОС N] и «Оценка: X» (см. GRADE_SYSTEM_PROMPT).
    Если модель нарушила формат, пропустила вопрос или вернула оценку
    вне диапазона 1–5, выбрасывается LLMRequestError.
    """
    pattern = re.compile(r"\[ВОПРОС\s*(\d+)\]\s*Оценка:\s*([1-5])")
    grades: dict[int, int] = {}
    for match in pattern.finditer(raw_response):
        number = int(match.group(1))
        grade = int(match.group(2))
        if number not in grades:
            grades[number] = grade
    if len(grades) < expected_count:
        raise LLMRequestError(
            "Модель вернула ответ в неожиданном формате: не удалось "
            "получить оценку за каждый вопрос. Попробуйте повторить "
            "отправку ответов."
        )
    return [grades[number] for number in sorted(grades)][:expected_count]


def grade_exam_answers(
    article_text: str,
    answered_questions: list[tuple[str, str]],
    system_prompt: str | None = None,
) -> list[int]:
    """Оценивает ответы студента на вопросы к статье по шкале от 1 до 5.

    Принимает текст статьи и список пар (текст_вопроса, ответ_студента).
    system_prompt — необязательное переопределение системного промпта
    (из типа статьи). Если оно не задано или пустое, используется промпт
    по умолчанию.

    Возвращает список целых оценок — по одной на каждый вопрос.
    """
    article_text, _truncated = _truncate_article_text(article_text)

    blocks = "\n\n".join(
        f"[ВОПРОС {index}]\n{question}\nОтвет студента: {answer}"
        for index, (question, answer) in enumerate(answered_questions, start=1)
    )
    user_prompt = (
        "Текст статьи:\n\n" + article_text +
        "\n\nВопросы и ответы студента:\n\n" + blocks
    )
    final_prompt = _resolve_system_prompt(
        GRADE_SYSTEM_PROMPT, system_prompt, GRADE_FORMAT_REQUIREMENTS
    )
    result = ask_llm(final_prompt, user_prompt)
    return _parse_grades(result, len(answered_questions))
