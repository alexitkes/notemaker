"""Модели базы данных приложения NoteMaker.

Модель пользователя определена здесь в одном месте, чтобы её использовали
и веб-приложение (app.py), и вспомогательные скрипты (например,
change_password.py) — без дублирования определения класса.

Объект db создаётся без привязки к приложению; приложение связывает его
через db.init_app(app) (см. app.py), а скрипты командной строки создают
собственное минимальное приложение Flask для работы с той же базой.
"""

from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

# Статусы экзамена: черновик можно редактировать, опубликованный — нет
EXAM_STATUS_DRAFT = "draft"
EXAM_STATUS_PUBLISHED = "published"

# Статусы попытки сдачи экзамена студентом
ATTEMPT_STATUS_IN_PROGRESS = "in_progress"
ATTEMPT_STATUS_COMPLETED = "completed"


class User(db.Model):
    """Пользователь системы."""

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="guest")

    def set_password(self, raw_password: str) -> None:
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        return check_password_hash(self.password_hash, raw_password)


class Exam(db.Model):
    """Экзамен по статье: набор вопросов с ответами.

    Пока статус — draft (черновик), экзамен виден и редактируем только
    его автором; после публикации (published) экзамен виден всем
    пользователям и больше не редактируется.
    """

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    status = db.Column(
        db.String(20),
        nullable=False,
        default=EXAM_STATUS_DRAFT,
        index=True,
    )
    created_by = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow
    )
    # Исходный текст статьи хранится для повторной генерации вопросов
    article_text = db.Column(db.Text, nullable=False, default="")
    # Необязательный тип статьи: определяет используемые системные промпты
    article_type_id = db.Column(
        db.Integer, db.ForeignKey("article_type.id"), nullable=True, index=True
    )

    author = db.relationship(
        "User", backref="exams", foreign_keys=[created_by]
    )
    article_type = db.relationship("ArticleType")
    questions = db.relationship(
        "ExamQuestion",
        backref="exam",
        cascade="all, delete-orphan",
        order_by="ExamQuestion.position",
        lazy="selectin",
    )

    @property
    def is_draft(self) -> bool:
        return self.status == EXAM_STATUS_DRAFT


class ExamQuestion(db.Model):
    """Вопрос экзамена с правильным ответом."""

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(
        db.Integer, db.ForeignKey("exam.id"), nullable=False, index=True
    )
    position = db.Column(db.Integer, nullable=False, default=0)
    question_text = db.Column(db.Text, nullable=False)
    answer_text = db.Column(db.Text, nullable=False, default="")


class ExamAttempt(db.Model):
    """Попытка студента сдать экзамен.

    Пока попытка in_progress — студент видит вопросы, но не ответы и
    не статью; после оценки моделью (completed) сохраняется итоговая
    оценка за экзамен.
    """

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(
        db.Integer, db.ForeignKey("exam.id"), nullable=False, index=True
    )
    student_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    status = db.Column(
        db.String(20),
        nullable=False,
        default=ATTEMPT_STATUS_IN_PROGRESS,
        index=True,
    )
    # Среднее арифметическое оценок за вопросы; заполняется при завершении
    score = db.Column(db.Float, nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow
    )
    completed_at = db.Column(db.DateTime, nullable=True)

    exam = db.relationship("Exam", backref="attempts")
    student = db.relationship("User", backref="exam_attempts")
    answers = db.relationship(
        "ExamAttemptAnswer",
        backref="attempt",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        # Один студент сдаёт конкретный экзамен один раз
        db.UniqueConstraint("exam_id", "student_id", name="uq_attempt_exam_student"),
    )


class ArticleType(db.Model):
    """Тип статьи (например, «научная статья», «учебное пособие»).

    Для каждого типа администратор может задать переопределения системных
    промптов для трёх задач: составления краткого содержания, генерации
    экзаменационных вопросов и проверки ответов студента. Пустое поле
    означает использование промпта по умолчанию.

    Обычные пользователи видят только name и description; переопределённые
    промпты доступны только администраторам.
    """

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=False, default="")
    # Переопределение промпта составления краткого содержания
    summary_prompt = db.Column(db.Text, nullable=False, default="")
    # Переопределение промпта генерации экзаменационных вопросов
    exam_prompt = db.Column(db.Text, nullable=False, default="")
    # Переопределение промпта проверки ответов на вопросы
    grade_prompt = db.Column(db.Text, nullable=False, default="")


class ExamAttemptAnswer(db.Model):
    """Ответ студента на вопрос экзамена и оценка за него (1–5)."""

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(
        db.Integer, db.ForeignKey("exam_attempt.id"), nullable=False, index=True
    )
    question_id = db.Column(
        db.Integer, db.ForeignKey("exam_question.id"), nullable=False, index=True
    )
    answer_text = db.Column(db.Text, nullable=False, default="")
    grade = db.Column(db.Integer, nullable=True)

    question = db.relationship("ExamQuestion")
