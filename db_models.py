"""Модели базы данных приложения NoteMaker.

Модель пользователя определена здесь в одном месте, чтобы её использовали
и веб-приложение (app.py), и вспомогательные скрипты (например,
change_password.py) — без дублирования определения класса.

Объект db создаётся без привязки к приложению; приложение связывает его
через db.init_app(app) (см. app.py), а скрипты командной строки создают
собственное минимальное приложение Flask для работы с той же базой.
"""

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


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
