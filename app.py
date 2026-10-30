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
# Конфигурация (переменные окружения) загружается и проверяется в модуле
# config; конфигурация языковой модели, системный промпт и функция получения
# краткого содержания статьи вынесены в отдельный модуль llm_service
from config import LLM_API_TYPE, LLM_MODEL, SECRET_KEY

# Модели базы данных (db, User) определены в db_models.py и используются
# также вспомогательными скриптами (change_password.py)
from db_models import (
    EXAM_STATUS_DRAFT,
    EXAM_STATUS_PUBLISHED,
    Exam,
    ExamQuestion,
    User,
    db,
)
from llm_service import (
    EXAM_QUESTION_COUNT,
    LLMConfigError,
    LLMRequestError,
    generate_exam_questions,
    summarize_article,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

app = Flask(__name__)
# Секретный ключ читается из окружения в config.py (в продакшене обязательно
# задайте свой SECRET_KEY через переменную окружения)
app.secret_key = SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "users.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

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


# --- Экзамены по статьям -----------------------------------------------------


def _require_login():
    """Возвращает текущего пользователя либо перенаправляет на вход."""
    user = get_current_user()
    if user is None:
        flash("Войдите в систему, чтобы работать с экзаменами.", "info")
        return None, redirect(url_for("login"))
    return user, None


def _get_own_draft(exam_id: int, user):
    """Возвращает черновик экзамена, доступный пользователю для правки.

    Черновик доступен только его автору и только пока он не опубликован.
    При недоступности возвращает (None, ответ_с_перенаправлением).
    """
    exam = db.session.get(Exam, exam_id)
    if exam is None:
        flash("Экзамен не найден.", "error")
        return None, redirect(url_for("exams"))
    if exam.status != EXAM_STATUS_DRAFT:
        flash("Этот экзамен уже сформирован и не редактируется.", "info")
        return None, redirect(url_for("exam_view", exam_id=exam.id))
    if exam.created_by != user.id:
        flash("Доступ запрещён: черновик виден только его автору.", "error")
        return None, redirect(url_for("exams"))
    return exam, None


@app.route("/exams")
def exams():
    """Список экзаменов: опубликованные видят все, черновики — только автор."""
    user, redirect_response = _require_login()
    if redirect_response is not None:
        return redirect_response

    published = (
        Exam.query.filter_by(status=EXAM_STATUS_PUBLISHED)
        .order_by(Exam.created_at.desc())
        .all()
    )
    drafts = []
    if user.role == "teacher":
        drafts = (
            Exam.query.filter_by(status=EXAM_STATUS_DRAFT, created_by=user.id)
            .order_by(Exam.created_at.desc())
            .all()
        )
    return render_template("exams.html", published=published, drafts=drafts)


@app.route("/exams/create", methods=["GET", "POST"])
def exam_create():
    """Создание экзамена: загрузка статьи и генерация вопросов моделью."""
    user, redirect_response = _require_login()
    if redirect_response is not None:
        return redirect_response
    if user.role != "teacher":
        flash(
            "Создавать экзамены могут только пользователи с ролью "
            "«Преподаватель».",
            "error",
        )
        return redirect(url_for("exams"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        uploaded = request.files.get("article")
        errors = []

        if not title:
            errors.append("Укажите название экзамена.")
        if uploaded is None or not uploaded.filename:
            errors.append("Выберите PDF-файл со статьёй.")
        elif not uploaded.filename.lower().endswith(".pdf"):
            errors.append("Допускаются только файлы в формате PDF.")

        article_text = ""
        if not errors:
            raw = uploaded.read()
            if not raw:
                errors.append("Файл пустой.")
            else:
                try:
                    article_text = extract_pdf_text(raw)
                except Exception as exc:
                    errors.append(f"Не удалось извлечь текст из PDF: {exc}")

            if not errors and not article_text.strip():
                errors.append(
                    "Не удалось извлечь текст из PDF. Возможно, "
                    "это скан-копия без текстового слоя."
                )

        pairs = []
        if not errors:
            try:
                pairs, _truncated = generate_exam_questions(article_text)
            except LLMRequestError as exc:
                errors.append(
                    f"Ошибка при генерации вопросов языковой моделью: {exc}"
                )
            except Exception as exc:
                errors.append(f"Ошибка при генерации вопросов: {exc}")
            if not errors and not pairs:
                errors.append("Модель не вернула ни одного вопроса.")

        if errors:
            for message in errors:
                flash(message, "error")
        else:
            exam = Exam(
                title=title,
                created_by=user.id,
                article_text=article_text,
                status=EXAM_STATUS_DRAFT,
            )
            for position, (question, answer) in enumerate(pairs, start=1):
                exam.questions.append(
                    ExamQuestion(
                        position=position,
                        question_text=question,
                        answer_text=answer,
                    )
                )
            db.session.add(exam)
            db.session.commit()
            flash("Черновик экзамена создан. Проверьте вопросы и ответы.", "success")
            return redirect(url_for("exam_edit", exam_id=exam.id))

    return render_template("exam_create.html", question_count=EXAM_QUESTION_COUNT)


@app.route("/exams/<int:exam_id>")
def exam_view(exam_id):
    """Просмотр сформированного экзамена (для всех пользователей)."""
    user, redirect_response = _require_login()
    if redirect_response is not None:
        return redirect_response

    exam = db.session.get(Exam, exam_id)
    if exam is None:
        flash("Экзамен не найден.", "error")
        return redirect(url_for("exams"))
    if exam.is_draft and exam.created_by != user.id:
        flash("Доступ запрещён: черновик виден только его автору.", "error")
        return redirect(url_for("exams"))
    return render_template("exam_view.html", exam=exam)


@app.route("/exams/<int:exam_id>/edit")
def exam_edit(exam_id):
    """Редактирование черновика экзамена (только автор)."""
    user, redirect_response = _require_login()
    if redirect_response is not None:
        return redirect_response
    exam, redirect_response = _get_own_draft(exam_id, user)
    if redirect_response is not None:
        return redirect_response
    return render_template("exam_edit.html", exam=exam)


@app.route("/exams/<int:exam_id>/questions/<int:question_id>/edit", methods=["POST"])
def exam_edit_question(exam_id, question_id):
    """Ручное исправление вопроса или ответа в черновике."""
    user, redirect_response = _require_login()
    if redirect_response is not None:
        return redirect_response
    exam, redirect_response = _get_own_draft(exam_id, user)
    if redirect_response is not None:
        return redirect_response

    question = db.session.get(ExamQuestion, question_id)
    if question is None or question.exam_id != exam.id:
        flash("Вопрос не найден.", "error")
        return redirect(url_for("exam_edit", exam_id=exam.id))

    question_text = request.form.get("question_text", "").strip()
    answer_text = request.form.get("answer_text", "").strip()
    if not question_text:
        flash("Текст вопроса не может быть пустым.", "error")
    else:
        question.question_text = question_text
        question.answer_text = answer_text
        db.session.commit()
        flash("Вопрос сохранён.", "success")
    return redirect(url_for("exam_edit", exam_id=exam.id))


@app.route("/exams/<int:exam_id>/questions/<int:question_id>/delete", methods=["POST"])
def exam_delete_question(exam_id, question_id):
    """Удаление вопроса с генерацией нового вопроса моделью вместо него."""
    user, redirect_response = _require_login()
    if redirect_response is not None:
        return redirect_response
    exam, redirect_response = _get_own_draft(exam_id, user)
    if redirect_response is not None:
        return redirect_response

    question = db.session.get(ExamQuestion, question_id)
    if question is None or question.exam_id != exam.id:
        flash("Вопрос не найден.", "error")
        return redirect(url_for("exam_edit", exam_id=exam.id))

    # Сначала запрашиваем замену у модели: если генерация не удастся,
    # существующий вопрос не удаляем.
    try:
        pairs, _truncated = generate_exam_questions(exam.article_text, count=1)
    except LLMRequestError as exc:
        flash(f"Не удалось сгенерировать новый вопрос: {exc}", "error")
        return redirect(url_for("exam_edit", exam_id=exam.id))
    except Exception as exc:
        flash(f"Не удалось сгенерировать новый вопрос: {exc}", "error")
        return redirect(url_for("exam_edit", exam_id=exam.id))

    new_position = (
        db.session.query(db.func.max(ExamQuestion.position))
        .filter_by(exam_id=exam.id)
        .scalar()
        or 0
    ) + 1

    db.session.delete(question)
    new_question_text, new_answer_text = pairs[0]
    exam.questions.append(
        ExamQuestion(
            position=new_position,
            question_text=new_question_text,
            answer_text=new_answer_text,
        )
    )
    db.session.commit()
    flash("Вопрос удалён, вместо него сгенерирован новый вопрос.", "success")
    return redirect(url_for("exam_edit", exam_id=exam.id))


@app.route("/exams/<int:exam_id>/publish", methods=["POST"])
def exam_publish(exam_id):
    """Завершение работы над экзаменом: черновик становится сформированным."""
    user, redirect_response = _require_login()
    if redirect_response is not None:
        return redirect_response
    exam, redirect_response = _get_own_draft(exam_id, user)
    if redirect_response is not None:
        return redirect_response

    if not exam.questions:
        flash(
            "Нельзя сформировать экзамен без вопросов. Добавьте хотя бы "
            "один вопрос.",
            "error",
        )
        return redirect(url_for("exam_edit", exam_id=exam.id))

    exam.status = EXAM_STATUS_PUBLISHED
    db.session.commit()
    flash("Экзамен сформирован и доступен преподавателям и студентам.", "success")
    return redirect(url_for("exam_view", exam_id=exam.id))


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
