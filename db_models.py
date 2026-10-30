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

    author = db.relationship(
        "User", backref="exams", foreign_keys=[created_by]
    )
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
