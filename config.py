"""Конфигурация приложения: загрузка и проверка переменных окружения.

Этот модуль должен импортироваться первым: при импорте выполняется
load_dotenv(), читаются переменные окружения и проверяется их корректность.
Если окружение невалидно (недопустимое значение LLM_API_TYPE, отсутствует
ключ доступа для выбранного типа LLM и т. п.), выбрасывается ConfigError
с понятным описанием того, что нужно исправить.

Каждый тип LLM использует собственный набор переменных окружения, поэтому
переключение между OpenAI-совместимым API и GigaChat выполняется только
изменением LLM_API_TYPE — ключи доступа обоих сервисов хранятся независимо:

- OpenAI-совместимый API: OPENAI_API_KEY, OPENAI_API_URL, OPENAI_MODEL
- GigaChat:               GIGACHAT_API_KEY, GIGACHAT_API_URL, GIGACHAT_MODEL
"""

import base64
import binascii
import os

from dotenv import load_dotenv

# Загружаем переменные окружения из файла .env
load_dotenv()


class ConfigError(Exception):
    """Ошибка конфигурации приложения (невалидное окружение)."""


# ---------------------------------------------------------------------------
# Вспомогательные функции чтения окружения
# ---------------------------------------------------------------------------
def _env(name: str, default: str = "") -> str:
    """Возвращает значение переменной окружения, убирая пробелы по краям."""
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: str) -> int:
    """Читает целочисленную переменную окружения с понятной ошибкой."""
    raw = _env(name, default)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"Переменная окружения {name} должна быть целым числом, "
            f"получено: '{raw}'."
        ) from None
    if value <= 0:
        raise ConfigError(
            f"Переменная окружения {name} должна быть положительным числом, "
            f"получено: {value}."
        )
    return value


# ---------------------------------------------------------------------------
# Тип API языковой модели
# ---------------------------------------------------------------------------
# Тип интерфейса: "openai" (OpenAI-совместимый) или "gigachat"
LLM_API_TYPE = _env("LLM_API_TYPE", "openai").lower()

SUPPORTED_API_TYPES = ("openai", "gigachat")

if LLM_API_TYPE not in SUPPORTED_API_TYPES:
    raise ConfigError(
        f"Недопустимое значение LLM_API_TYPE='{LLM_API_TYPE}'. "
        f"Допустимые значения: {', '.join(SUPPORTED_API_TYPES)}. "
        "Исправьте значение в файле .env или в переменных окружения."
    )

# Значения по умолчанию и имена переменных окружения для каждого типа API
LLM_SETTINGS = {
    "openai": {
        "url_var": "OPENAI_API_URL",
        "model_var": "OPENAI_MODEL",
        "key_var": "OPENAI_API_KEY",
        "default_url": "https://api.proxyapi.ru/openai/v1",
        "default_model": "gpt-4o-mini",
        "key_hint": "API-ключ OpenAI-совместимого сервиса (например, ProxyAPI)",
    },
    "gigachat": {
        "url_var": "GIGACHAT_API_URL",
        "model_var": "GIGACHAT_MODEL",
        "key_var": "GIGACHAT_API_KEY",
        "default_url": "https://gigachat.devices.sberbank.ru/api/v1",
        "default_model": "GigaChat-2",
        "key_hint": "ключ разработчика GigaChat (строка в формате base64)",
    },
}

_settings = LLM_SETTINGS[LLM_API_TYPE]

# ---------------------------------------------------------------------------
# Параметры активного типа LLM (выбираются по LLM_API_TYPE)
# ---------------------------------------------------------------------------
LLM_API_URL = _env(_settings["url_var"]) or _settings["default_url"]
LLM_MODEL = _env(_settings["model_var"]) or _settings["default_model"]
LLM_API_TOKEN = _env(_settings["key_var"])

if not LLM_API_TOKEN:
    raise ConfigError(
        f"Не задан ключ доступа для выбранного типа LLM (LLM_API_TYPE="
        f"'{LLM_API_TYPE}'). Укажите {_settings['key_var']} "
        f"({_settings['key_hint']}) в файле .env."
    )

# Для GigaChat ключ разработчика должен быть корректной строкой base64:
# он передаётся в OAuth-запросе как Basic auth
if LLM_API_TYPE == "gigachat":
    try:
        base64.b64decode(LLM_API_TOKEN, validate=True)
    except (binascii.Error, ValueError):
        raise ConfigError(
            "Переменная GIGACHAT_API_KEY должна содержать ключ разработчика "
            "GigaChat в формате base64. Проверьте значение в файле .env."
        ) from None

# ---------------------------------------------------------------------------
# Прочие настройки
# ---------------------------------------------------------------------------
# OAuth-эндпоинт GigaChat для получения access_token
GIGACHAT_OAUTH_URL = _env(
    "GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
)

# Максимальный размер текста статьи (символов), передаваемого модели
MAX_LLM_INPUT_CHARS = _env_int("MAX_LLM_INPUT_CHARS", "75000")

# ---------------------------------------------------------------------------
# Температура генерации для разных задач LLM
# ---------------------------------------------------------------------------
# Чем меньше температура, тем детерминированнее и строже ответы модели.
# Проверка ответов студента — самая формальная задача, нужен минимум
# «творчества» и строгий соблюдаемый формат вывода.
TEMPERATURE_GRADING = 0.1

# Составление краткого содержания — по связному тексту допустима
# умеренная вариативность формулировок.
TEMPERATURE_SUMMARY = 0.3

# Генерация экзаменационных вопросов — требуется разнообразие формулировок,
# поэтому температура наибольшая из трёх.
TEMPERATURE_QUESTIONS = 0.5

# Секретный ключ Flask для подписи сессий
SECRET_KEY = _env("SECRET_KEY", "dev-secret-key-change-me")
