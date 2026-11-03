import io
import logging
import os
from datetime import datetime

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
    ATTEMPT_STATUS_COMPLETED,
    ATTEMPT_STATUS_IN_PROGRESS,
    EXAM_STATUS_DRAFT,
    EXAM_STATUS_PUBLISHED,
    ArticleType,
    Exam,
    ExamAttempt,
    ExamAttemptAnswer,
    ExamQuestion,
    User,
    db,
)
from llm_service import (
    EXAM_QUESTION_COUNT,
    LLMConfigError,
    LLMRequestError,
    detect_article_type,
    generate_exam_questions,
    grade_exam_answers,
    summarize_article,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

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
            # Все новые пользователи через регистрацию получают роль «гость».
            # Администраторы создаются только через create_user.py.
            role = "guest"
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


# --- Типы статей -------------------------------------------------------------


def _require_admin():
    """Возвращает текущего пользователя-администратора либо перенаправляет."""
    user = get_current_user()
    if user is None or user.role != "administrator":
        flash(
            "Доступ запрещён: эта страница доступна только администратору.",
            "error",
        )
        if user is None:
            return None, redirect(url_for("login"))
        return None, redirect(url_for("index"))
    return user, None


def _parse_article_type_from_form():
    """Читает выбранную статью-тип из формы.

    Пустое значение означает «без типа». При некорректном значении
    возвращает (None, сообщение_об_ошибке), при успехе — (тип, None).
    """
    raw = request.form.get("article_type", "").strip()
    if not raw:
        return None, None
    if not raw.isdigit():
        return None, "Выберите корректный тип статьи."
    article_type = db.session.get(ArticleType, int(raw))
    if article_type is None:
        return None, "Выбранный тип статьи не найден."
    return article_type, None


def _get_article_types():
    """Список типов статей для выбора при загрузке (имя и описание)."""
    return ArticleType.query.order_by(ArticleType.name).all()


def _detect_article_type_by_llm(article_text: str) -> tuple[ArticleType | None, str]:
    """Автоматически определяет тип статьи с помощью языковой модели.

    Статья передаётся в модель вместе со списком названий и описаний
    предусмотренных типов; модель отвечает названием наиболее подходящего
    типа или пустым значением (тогда тип не присваивается).

    Возвращает (тип_или_None, сообщение_для_пользователя).
    При ошибке обращения к модели выбрасывает LLMConfigError/LLMRequestError.
    """
    article_types = _get_article_types()
    if not article_types:
        return None, (
            "Автоопределение типа недоступно: в системе ещё не создано "
            "ни одного типа статей."
        )

    detected_name = detect_article_type(
        article_text,
        [(item.name, item.description) for item in article_types],
    )

    if not detected_name:
        return None, (
            "Модель не отнесла статью ни к одному из типов — тип не "
            "присвоен. При необходимости выберите тип вручную."
        )

    for item in article_types:
        if item.name == detected_name:
            return item, (
                f"Модель определила тип статьи: «{item.name}». "
                "При необходимости скорректируйте его вручную."
            )

    # Ответ модели не совпал ни с одним названием типа
    return None, (
        "Модель не смогла однозначно определить тип статьи — тип не "
        "присвоен. При необходимости выберите тип вручную."
    )


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

    article_text = ""
    article_type = None
    detection_message = None

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        uploaded = request.files.get("article")
        # Текст статьи, перенесённый из предыдущего шага формы: после
        # автоопределения типа повторно загружать файл не требуется
        hidden_text = request.form.get("article_text", "").strip()
        # Признак того, что автоопределение типа уже выполнено
        auto_detected = request.form.get("auto_detected") == "1"
        errors = []

        if not title:
            errors.append("Укажите название экзамена.")

        if uploaded is not None and uploaded.filename:
            if not uploaded.filename.lower().endswith(".pdf"):
                errors.append("Допускаются только файлы в формате PDF.")
            else:
                raw = uploaded.read()
                if not raw:
                    errors.append("Файл пустой.")
                else:
                    try:
                        article_text = extract_pdf_text(raw)
                    except Exception as exc:
                        errors.append(
                            f"Не удалось извлечь текст из PDF: {exc}"
                        )
                # Загружен новый файл — автоопределение выполняем заново
                auto_detected = False
        elif hidden_text:
            article_text = hidden_text
        else:
            errors.append("Выберите PDF-файл со статьёй.")

        if not errors and not article_text.strip():
            errors.append(
                "Не удалось извлечь текст из PDF. Возможно, "
                "это скан-копия без текстового слоя."
            )

        # Тип статьи необязателен: определяет переопределённые промпты
        article_type, type_error = _parse_article_type_from_form()
        if type_error:
            errors.append(type_error)

        if not errors and request.form.get("detect_type") and not auto_detected:
            # Пользователь запросил автоопределение типа статьи:
            # определяем тип моделью и снова показываем форму, давая
            # возможность при необходимости скорректировать тип вручную.
            # Вопросы на этом шаге не генерируются.
            try:
                article_type, detection_message = (
                    _detect_article_type_by_llm(article_text)
                )
            except LLMConfigError as exc:
                errors.append(str(exc))
            except LLMRequestError as exc:
                errors.append(f"Ошибка обращения к языковой модели: {exc}")
            except Exception as exc:
                errors.append(f"Ошибка при обращении к языковой модели: {exc}")
            else:
                return render_template(
                    "exam_create.html",
                    question_count=EXAM_QUESTION_COUNT,
                    article_types=_get_article_types(),
                    title_value=title,
                    article_text_hidden=article_text,
                    auto_detected=True,
                    detection_message=detection_message,
                    selected_type=article_type,
                )

        pairs = []
        if not errors:
            # Если у типа статьи задан свой промпт генерации вопросов —
            # используем его, иначе промпт по умолчанию (внутри сервиса)
            try:
                pairs, _truncated = generate_exam_questions(
                    article_text,
                    system_prompt=(
                        article_type.exam_prompt if article_type else None
                    ),
                )
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
                article_type=article_type,
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

    return render_template(
        "exam_create.html",
        question_count=EXAM_QUESTION_COUNT,
        article_types=_get_article_types(),
        title_value=(
            request.form.get("title", "") if request.method == "POST" else ""
        ),
        article_text_hidden=article_text,
        auto_detected=False,
        detection_message=detection_message,
        selected_type=article_type,
    )


@app.route("/exams/<int:exam_id>")
def exam_view(exam_id):
    """Просмотр сформированного экзамена.

    Студент видит статью, но не вопросы и ответы; если экзамен уже
    сдан — результат со своими ответами, оценками и статьёй.
    Преподаватели и администраторы видят вопросы с правильными ответами.
    """
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

    attempt = None
    answers_by_question = {}
    if user.role == "student":
        attempt = (
            ExamAttempt.query.filter_by(exam_id=exam.id, student_id=user.id)
            .order_by(ExamAttempt.id.desc())
            .first()
        )
        if attempt is not None:
            if attempt.status == ATTEMPT_STATUS_IN_PROGRESS:
                # Экзамен уже начат: статья скрыта, продолжаем сдачу
                return redirect(url_for("exam_take", exam_id=exam.id))
            answers_by_question = {
                answer.question_id: answer for answer in attempt.answers
            }
    return render_template(
        "exam_view.html",
        exam=exam,
        attempt=attempt,
        answers_by_question=answers_by_question,
    )


def _get_published_exam_for_student(exam_id: int, user):
    """Возвращает опубликованный экзамен для сдачи студентом.

    При недоступности возвращает (None, ответ_с_перенаправлением).
    """
    if user.role != "student":
        flash("Сдавать экзамены могут только студенты.", "error")
        return None, redirect(url_for("exams"))
    exam = db.session.get(Exam, exam_id)
    if exam is None:
        flash("Экзамен не найден.", "error")
        return None, redirect(url_for("exams"))
    if exam.is_draft:
        flash("Этот экзамен ещё не сформирован.", "info")
        return None, redirect(url_for("exams"))
    return exam, None


def _get_own_attempt(exam_id: int, user):
    """Возвращает незавершённую попытку сдачи экзамена текущим студентом."""
    return ExamAttempt.query.filter_by(
        exam_id=exam_id,
        student_id=user.id,
        status=ATTEMPT_STATUS_IN_PROGRESS,
    ).first()


@app.route("/exams/<int:exam_id>/start", methods=["POST"])
def exam_start(exam_id):
    """Начало сдачи экзамена: создаётся попытка, статья скрывается."""
    user, redirect_response = _require_login()
    if redirect_response is not None:
        return redirect_response
    exam, redirect_response = _get_published_exam_for_student(exam_id, user)
    if redirect_response is not None:
        return redirect_response

    attempt = (
        ExamAttempt.query.filter_by(exam_id=exam.id, student_id=user.id)
        .order_by(ExamAttempt.id.desc())
        .first()
    )
    if attempt is not None and attempt.status == ATTEMPT_STATUS_COMPLETED:
        flash("Вы уже сдали этот экзамен.", "info")
        return redirect(url_for("exam_view", exam_id=exam.id))
    if attempt is None:
        attempt = ExamAttempt(exam_id=exam.id, student_id=user.id)
        db.session.add(attempt)
        db.session.commit()
    return redirect(url_for("exam_take", exam_id=exam.id))


@app.route("/exams/<int:exam_id>/take")
def exam_take(exam_id):
    """Процесс сдачи: студент видит вопросы, но не ответы и не статью."""
    user, redirect_response = _require_login()
    if redirect_response is not None:
        return redirect_response
    exam, redirect_response = _get_published_exam_for_student(exam_id, user)
    if redirect_response is not None:
        return redirect_response

    attempt = _get_own_attempt(exam.id, user)
    if attempt is None:
        flash("Экзамен не начат или уже завершён.", "info")
        return redirect(url_for("exam_view", exam_id=exam.id))
    return render_template("exam_take.html", exam=exam, attempt=attempt)


@app.route("/exams/<int:exam_id>/submit", methods=["POST"])
def exam_submit(exam_id):
    """Сдача ответов: модель оценивает их, выставляется итоговая оценка."""
    user, redirect_response = _require_login()
    if redirect_response is not None:
        return redirect_response
    exam, redirect_response = _get_published_exam_for_student(exam_id, user)
    if redirect_response is not None:
        return redirect_response

    attempt = _get_own_attempt(exam.id, user)
    if attempt is None:
        flash("Экзамен не начат или уже завершён.", "info")
        return redirect(url_for("exam_view", exam_id=exam.id))

    # Собираем ответы студента: на все вопросы ответы обязательны
    answered = []
    for question in exam.questions:
        text = request.form.get(f"answer_{question.id}", "").strip()
        if not text:
            flash("Ответьте на все вопросы, прежде чем сдавать экзамен.", "error")
            return redirect(url_for("exam_take", exam_id=exam.id))
        answered.append((question, text))

    # Запрос к модели: оценить ответы студента по шкале от 1 до 5
    try:
        grades = grade_exam_answers(
            exam.article_text,
            [(question.question_text, text) for question, text in answered],
            # Промпт проверки — из типа статьи экзамена, если задан
            system_prompt=(
                exam.article_type.grade_prompt
                if exam.article_type else None
            ),
        )
    except LLMRequestError as exc:
        flash(f"Ошибка при проверке ответов языковой моделью: {exc}", "error")
        return redirect(url_for("exam_take", exam_id=exam.id))
    except Exception as exc:
        flash(f"Ошибка при проверке ответов: {exc}", "error")
        return redirect(url_for("exam_take", exam_id=exam.id))

    for (question, text), grade in zip(answered, grades):
        attempt.answers.append(
            ExamAttemptAnswer(
                question_id=question.id,
                answer_text=text,
                grade=grade,
            )
        )
    attempt.score = round(sum(grades) / len(grades), 2)
    attempt.status = ATTEMPT_STATUS_COMPLETED
    attempt.completed_at = datetime.utcnow()
    db.session.commit()
    flash(f"Экзамен сдан! Ваша оценка: {attempt.score}.", "success")
    return redirect(url_for("exam_view", exam_id=exam.id))


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


# --- Управление типами статей (только администратор) -------------------------


@app.route("/admin/article-types")
def admin_article_types():
    """Список типов статей с формой добавления — только администратору."""
    admin, redirect_response = _require_admin()
    if redirect_response is not None:
        return redirect_response
    article_types = _get_article_types()
    return render_template("admin_article_types.html", article_types=article_types)


@app.route("/admin/article-types/new", methods=["GET", "POST"])
def admin_article_type_new():
    """Добавление нового типа статьи — только администратору."""
    admin, redirect_response = _require_admin()
    if redirect_response is not None:
        return redirect_response

    if request.method == "POST":
        errors = _validate_article_type_form()
        if not errors:
            article_type = ArticleType(
                name=request.form.get("name", "").strip(),
                description=request.form.get("description", "").strip(),
                summary_prompt=request.form.get("summary_prompt", "").strip(),
                exam_prompt=request.form.get("exam_prompt", "").strip(),
                grade_prompt=request.form.get("grade_prompt", "").strip(),
            )
            db.session.add(article_type)
            db.session.commit()
            flash(
                f"Тип статьи «{article_type.name}» добавлен.",
                "success",
            )
            return redirect(url_for("admin_article_types"))
        for message in errors:
            flash(message, "error")

    return render_template("admin_article_type_form.html", article_type=None)


@app.route("/admin/article-types/<int:type_id>/edit", methods=["GET", "POST"])
def admin_article_type_edit(type_id):
    """Редактирование типа статьи — только администратору."""
    admin, redirect_response = _require_admin()
    if redirect_response is not None:
        return redirect_response

    article_type = db.session.get(ArticleType, type_id)
    if article_type is None:
        flash("Тип статьи не найден.", "error")
        return redirect(url_for("admin_article_types"))

    if request.method == "POST":
        errors = _validate_article_type_form(exclude_id=article_type.id)
        if not errors:
            article_type.name = request.form.get("name", "").strip()
            article_type.description = request.form.get("description", "").strip()
            article_type.summary_prompt = request.form.get(
                "summary_prompt", ""
            ).strip()
            article_type.exam_prompt = request.form.get("exam_prompt", "").strip()
            article_type.grade_prompt = request.form.get("grade_prompt", "").strip()
            db.session.commit()
            flash(f"Тип статьи «{article_type.name}» сохранён.", "success")
            return redirect(url_for("admin_article_types"))
        for message in errors:
            flash(message, "error")

    return render_template(
        "admin_article_type_form.html", article_type=article_type
    )


@app.route("/admin/article-types/<int:type_id>/delete", methods=["POST"])
def admin_article_type_delete(type_id):
    """Удаление типа статьи — только администратору.

    Статьи, привязанные к типу, сохраняются: у них сбрасывается тип,
    и при работе с ними используется промпт по умолчанию.
    """
    admin, redirect_response = _require_admin()
    if redirect_response is not None:
        return redirect_response

    article_type = db.session.get(ArticleType, type_id)
    if article_type is None:
        flash("Тип статьи не найден.", "error")
        return redirect(url_for("admin_article_types"))

    # Экзамены, привязанные к типу, сохраняются: у них сбрасывается тип
    Exam.query.filter_by(article_type_id=article_type.id).update(
        {Exam.article_type_id: None}
    )
    db.session.delete(article_type)
    db.session.commit()
    flash(f"Тип статьи «{article_type.name}» удалён.", "success")
    return redirect(url_for("admin_article_types"))


def _validate_article_type_form(exclude_id: int | None = None) -> list[str]:
    """Проверяет форму типа статьи; возвращает список ошибок."""
    errors = []
    name = request.form.get("name", "").strip()
    if not name:
        errors.append("Укажите название типа статьи.")
    elif len(name) > 200:
        errors.append("Название типа статьи не должно превышать 200 символов.")
    else:
        query = ArticleType.query.filter_by(name=name)
        if exclude_id is not None:
            query = query.filter(ArticleType.id != exclude_id)
        if query.first():
            errors.append("Тип статьи с таким названием уже существует.")
    return errors


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
    article_type = None
    article_text = ""
    detection_message = None

    if request.method == "POST":
        uploaded = request.files.get("article")
        # Текст статьи, перенесённый из предыдущего шага формы: после
        # автоопределения типа повторно загружать файл не требуется
        hidden_text = request.form.get("article_text", "").strip()
        # Признак того, что автоопределение типа уже выполнено
        auto_detected = request.form.get("auto_detected") == "1"

        if uploaded is not None and uploaded.filename:
            if not uploaded.filename.lower().endswith(".pdf"):
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
                # Загружен новый файл — автоопределение выполняем заново
                auto_detected = False
        elif hidden_text:
            article_text = hidden_text
        else:
            error = "Выберите PDF-файл со статьёй."

        if not error and not article_text.strip():
            error = (
                "Не удалось извлечь текст из PDF. Возможно, "
                "это скан-копия без текстового слоя."
            )

        # Тип статьи необязателен: определяет промпт краткого содержания
        if not error:
            article_type, type_error = _parse_article_type_from_form()
            if type_error:
                error = type_error

        if (
            not error
            and request.form.get("detect_type")
            and not auto_detected
        ):
            # Пользователь запросил автоопределение типа статьи:
            # определяем тип моделью и снова показываем форму, давая
            # возможность при необходимости скорректировать тип вручную.
            # Краткое содержание на этом шаге не формируется.
            try:
                article_type, detection_message = (
                    _detect_article_type_by_llm(article_text)
                )
            except LLMConfigError as exc:
                error = str(exc)
            except LLMRequestError as exc:
                error = f"Ошибка обращения к языковой модели: {exc}"
            except Exception as exc:
                error = f"Ошибка при обращении к языковой модели: {exc}"
            else:
                return render_template(
                    "summary.html",
                    user=user,
                    error=None,
                    result=None,
                    truncated=False,
                    model_name=LLM_MODEL,
                    llm_type_label=(
                        "GigaChat" if LLM_API_TYPE == "gigachat"
                        else "OpenAI-совместимый API"
                    ),
                    article_types=_get_article_types(),
                    selected_type=article_type,
                    article_text_hidden=article_text,
                    auto_detected=True,
                    detection_message=detection_message,
                )

        if not error:
            try:
                result, truncated = summarize_article(
                    article_text,
                    # Промпт — из типа статьи, если задан
                    system_prompt=(
                        article_type.summary_prompt
                        if article_type else None
                    ),
                )
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
        article_types=_get_article_types(),
        selected_type=article_type,
        article_text_hidden=article_text if request.method == "POST" else "",
        auto_detected=False,
        detection_message=detection_message,
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


def _ensure_exam_article_type_column() -> None:
    """Добавляет колонку article_type_id в таблицу exam для существующих БД.

    db.create_all() не изменяет уже существующие таблицы, поэтому для баз,
    созданных до появления типов статей, колонка добавляется вручную.
    """
    with db.engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.execute(db.text("PRAGMA table_info(exam)"))
        }
        if "article_type_id" not in columns:
            connection.execute(
                db.text(
                    "ALTER TABLE exam ADD COLUMN article_type_id INTEGER "
                    "REFERENCES article_type(id)"
                )
            )
            connection.commit()


# Создаём таблицы в SQLite (файл users.db) при загрузке приложения
with app.app_context():
    db.create_all()
    _ensure_exam_article_type_column()

if __name__ == "__main__":
    app.run(debug=True)
