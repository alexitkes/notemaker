import io
import os

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

# Конфигурация (переменные окружения) загружается и проверяется в модуле
# config; конфигурация языковой модели, системный промпт и функция получения
# краткого содержания статьи вынесены в отдельный модуль llm_service
from config import LLM_API_TYPE, LLM_MODEL, SECRET_KEY
from llm_service import (
    LLMConfigError,
    LLMRequestError,
    summarize_article,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
# Секретный ключ читается из окружения в config.py (в продакшене обязательно
# задайте свой SECRET_KEY через переменную окружения)
app.secret_key = SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "users.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# Ограничение: размер загружаемого PDF (10 МБ)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

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
                else:
                    try:
                        result, truncated = summarize_article(article_text)
                    except LLMConfigError as exc:
                        error = str(exc)
                    except LLMRequestError as exc:
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


# --- Вспомогательные функции для работы с PDF ------------------------------
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


# Создаём таблицы в SQLite (файл users.db) при загрузке приложения
with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(debug=True)
