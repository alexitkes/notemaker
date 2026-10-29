import base64
import io
import os
import time
import uuid

import requests
from dotenv import load_dotenv
from pypdf import PdfReader
from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

# Загружаем переменные окружения из файла .env (LLM_API_TYPE, LLM_API_URL и т. д.)
load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
# В продакшене обязательно задайте свой SECRET_KEY через переменную окружения
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "users.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ---------------------------------------------------------------------------
# Настройки языковой модели (значения читаются из файла .env)
# ---------------------------------------------------------------------------
# Тип API: "openai" (OpenAI-совместимый) или "gigachat"
LLM_API_TYPE = os.environ.get("LLM_API_TYPE", "openai").strip().lower()
LLM_API_URL = os.environ.get("LLM_API_URL", "").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "").strip()
LLM_API_TOKEN = os.environ.get("LLM_API_TOKEN", "").strip()

# Значения по умолчанию: для OpenAI-совместимого API — сервис ProxyAPI,
# для GigaChat — основной сервер GigaChat
DEFAULT_LLM_URL = {
    "openai": "https://api.proxyapi.ru/openai/v1",
    "gigachat": "https://gigachat.devices.sberbank.ru/api/v1",
}
DEFAULT_LLM_MODEL = {
    "openai": "gpt-4o-mini",
    "gigachat": "GigaChat-20",
}
if LLM_API_TYPE not in DEFAULT_LLM_URL:
    LLM_API_TYPE = "openai"
if not LLM_API_URL:
    LLM_API_URL = DEFAULT_LLM_URL[LLM_API_TYPE]
if not LLM_MODEL:
    LLM_MODEL = DEFAULT_LLM_MODEL[LLM_API_TYPE]

# OAuth-эндпоинт GigaChat для получения access_token
GIGACHAT_OAUTH_URL = os.environ.get(
    "GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
)

# Ограничения: размер загружаемого PDF (10 МБ) и размер текста для модели
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
MAX_LLM_INPUT_CHARS = int(os.environ.get("MAX_LLM_INPUT_CHARS", "24000"))
SUMMARY_MAX_CHARS = 4000

# Кэш access_token GigaChat: истёкший токен перевыпускается автоматически
_gigachat_token_cache = {"access_token": None, "expires_at": 0}

# Доступные роли в системе
ROLES = ("administrator", "guest", "student", "teacher")
ROLE_LABELS = {
    "administrator": "Администратор",
    "guest": "Гость",
    "student": "Студент",
    "teacher": "Преподаватель",
}


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="guest")

    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)


def get_current_user():
    """Возвращает пользователя из текущей сессии или None."""
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return db.session.get(User, user_id)


@app.context_processor
def inject_template_globals():
    """Передаёт во все шаблоны словарь ролей и текущего пользователя."""
    return {"role_labels": ROLE_LABELS, "current_user": get_current_user()}


@app.route("/")
def index():
    user = get_current_user()
    if user is None:
        return redirect(url_for("login"))
    return render_template("dashboard.html", user=user)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        errors = []
        if not username:
            errors.append("Укажите имя пользователя.")
        if not email or "@" not in email or "." not in email:
            errors.append("Укажите корректный адрес почты.")
        if len(password) < 6:
            errors.append("Пароль должен содержать не менее 6 символов.")
        elif password != password2:
            errors.append("Пароли не совпадают.")

        if not errors and User.query.filter_by(email=email).first():
            errors.append("Пользователь с такой почтой уже зарегистрирован.")

        if errors:
            for message in errors:
                flash(message, "error")
        else:
            # Первый зарегистрированный пользователь становится администратором,
            # все остальные — гостями.
            role = "administrator" if User.query.count() == 0 else "guest"
            user = User(username=username, email=email, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(
                "Регистрация прошла успешно! Теперь вы можете войти "
                f"в систему. Ваша роль — «{ROLE_LABELS[role]}».",
                "success",
            )
            return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user is None or not user.check_password(password):
            flash("Неверная почта или пароль.", "error")
        else:
            session.clear()
            session["user_id"] = user.id
            flash(f"Добро пожаловать, {user.username}!", "success")
            return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Вы вышли из системы.", "info")
    return redirect(url_for("login"))


@app.route("/admin/users")
def admin_users():
    """Список пользователей — доступен только администратору."""
    user = get_current_user()
    if user is None or user.role != "administrator":
        flash("Доступ запрещён: эта страница доступна только администратору.", "error")
        return redirect(url_for("login"))
    users = User.query.order_by(User.id).all()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
def admin_change_role(user_id):
    """Изменение роли пользователя — доступно только администратору."""
    admin = get_current_user()
    if admin is None or admin.role != "administrator":
        flash("Доступ запрещён: эта страница доступна только администратору.", "error")
        return redirect(url_for("login"))

    target = db.session.get(User, user_id)
    if target is None:
        flash("Пользователь не найден.", "error")
        return redirect(url_for("admin_users"))

    new_role = request.form.get("role")
    if new_role not in ROLES:
        flash("Некорректная роль.", "error")
        return redirect(url_for("admin_users"))

    # Защита от случайной потери прав: админ не меняет свою собственную роль.
    if target.id == admin.id and new_role != "administrator":
        flash("Нельзя изменять собственную роль администратора.", "error")
        return redirect(url_for("admin_users"))

    target.role = new_role
    db.session.commit()
    flash(
        f"Роль пользователя {target.username} изменена на «{ROLE_LABELS[new_role]}».",
        "success",
    )
    return redirect(url_for("admin_users"))


@app.route("/summary", methods=["GET", "POST"])
def summary():
    """Краткое содержание статьи из PDF — доступно всем зарегистрированным пользователям."""
    user = get_current_user()
    if user is None:
        flash("Войдите в систему, чтобы использовать эту функцию.", "info")
        return redirect(url_for("login"))

    error = None
    result = None
    truncated = False

    if request.method == "POST":
        uploaded = request.files.get("article")
        if uploaded is None or not uploaded.filename:
            error = "Выберите PDF-файл со статьёй."
        elif not uploaded.filename.lower().endswith(".pdf"):
            error = "Допускаются только файлы в формате PDF."
        else:
            raw = uploaded.read()
            if not raw:
                error = "Файл пустой."
            else:
                try:
                    article_text = extract_pdf_text(raw)
                except Exception as exc:
                    article_text = ""
                    error = f"Не удалось извлечь текст из PDF: {exc}"

                if not article_text.strip():
                    error = error or (
                        "Не удалось извлечь текст из PDF. Возможно, "
                        "это скан-копия без текстового слоя."
                    )
                elif not LLM_API_TOKEN:
                    error = (
                        "Не задан токен доступа. Укажите LLM_API_TOKEN "
                        "в файле .env."
                    )
                else:
                    if len(article_text) > MAX_LLM_INPUT_CHARS:
                        article_text = article_text[:MAX_LLM_INPUT_CHARS]
                        truncated = True
                    system_prompt = (
                        "Ты — ассистент, который составляет краткое "
                        "содержание научных и учебных статей. Составь "
                        "краткое содержание предложенной статьи на русском "
                        f"языке объёмом не более {SUMMARY_MAX_CHARS} символов. "
                        "Выдели основную идею, ключевые тезисы и выводы. "
                        "Отвечай только текстом краткого содержания."
                    )
                    try:
                        result = ask_llm(
                            system_prompt, "Текст статьи:\n\n" + article_text
                        )
                        result = result[:SUMMARY_MAX_CHARS]
                    except requests.RequestException as exc:
                        error = f"Ошибка обращения к языковой модели: {exc}"
                    except Exception as exc:
                        error = f"Ошибка при обращении к языковой модели: {exc}"

    return render_template(
        "summary.html",
        user=user,
        error=error,
        result=result,
        truncated=truncated,
        model_name=LLM_MODEL,
        llm_type_label=(
            "GigaChat" if LLM_API_TYPE == "gigachat" else "OpenAI-совместимый API"
        ),
    )


@app.errorhandler(413)
def request_entity_too_large(_error):
    """Слишком большой загруженный файл (максимум 10 МБ)."""
    flash("Файл слишком большой. Максимальный размер — 10 МБ.", "error")
    return redirect(url_for("summary"))


# --- Вспомогательные функции для работы с PDF и языковой моделью -----------
def extract_pdf_text(raw: bytes) -> str:
    """Извлекает текстовое содержимое из PDF-файла."""
    reader = PdfReader(io.BytesIO(raw))
    pages = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        cleaned = "\n".join(
            line.strip() for line in page_text.splitlines() if line.strip()
        )
        if cleaned:
            pages.append(cleaned)
    return "\n\n".join(pages)


def get_gigachat_token() -> str:
    """Возвращает действующий access_token GigaChat, при необходимости получая новый."""
    cached = _gigachat_token_cache
    if cached["access_token"] and time.time() < cached["expires_at"] - 60:
        return cached["access_token"]

    if not LLM_API_TOKEN:
        raise ValueError(
            "Для работы с GigaChat укажите LLM_API_TOKEN (ключ разработчика "
            "в формате base64) в файле .env."
        )

    auth_header = base64.b64encode(LLM_API_TOKEN.encode()).decode()
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
        "temperature": 0.3,
    }

    response = requests.post(url, headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return content.strip()


# Создаём таблицы в SQLite (файл users.db) при загрузке приложения
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True)
