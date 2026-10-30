"""Утилита командной строки для смены пароля пользователя.

Работает напрямую с базой данных SQLite (users.db), не требует настроенного
окружения языковой модели (переменных .env), поэтому её можно использовать
даже если приложение не запускается из-за ошибки конфигурации.

Модель пользователя (User) и объект db импортируются из db_models.py —
это те же модели, что использует веб-приложение app.py.

Примеры использования:

    # интерактивный ввод пароля (скрытый, с подтверждением)
    python change_password.py user@example.com

    # передача нового пароля в командной строке
    python change_password.py user@example.com --password "new-secret"

    # указать другой файл базы данных
    python change_password.py user@example.com --db /path/to/users.db
"""

import argparse
import getpass
import os
import sys

from flask import Flask

from db_models import User, db

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "users.db")

# Минимальная длина пароля — та же, что и при регистрации в app.py
MIN_PASSWORD_LENGTH = 6


def parse_args(argv=None) -> argparse.Namespace:
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Смена пароля пользователя NoteMaker.",
        epilog=(
            'Если пароль не передан через --password, он будет запрошен '
            "интерактивно (ввод скрыт, требуется подтверждение)."
        ),
    )
    parser.add_argument(
        "email",
        help="почта пользователя, для которого меняется пароль",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="новый пароль (по умолчанию запрашивается интерактивно)",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"путь к файлу базы данных SQLite (по умолчанию: {DEFAULT_DB_PATH})",
    )
    return parser.parse_args(argv)


def read_password_interactively() -> str:
    """Запрашивает пароль дважды и проверяет его корректность."""
    password = getpass.getpass("Новый пароль: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(
            f"Ошибка: пароль должен содержать не менее "
            f"{MIN_PASSWORD_LENGTH} символов."
        )
    confirmation = getpass.getpass("Повторите новый пароль: ")
    if password != confirmation:
        sys.exit("Ошибка: пароли не совпадают.")
    return password


def main(argv=None) -> int:
    args = parse_args(argv)

    if not os.path.exists(args.db):
        sys.exit(
            f"Ошибка: база данных не найдена: {args.db}.\n"
            "Убедитесь, что приложение хотя бы раз запускалось и создало "
            "файл users.db, либо укажите путь через --db."
        )

    email = args.email.strip().lower()
    if args.password is not None:
        if len(args.password) < MIN_PASSWORD_LENGTH:
            sys.exit(
                f"Ошибка: пароль должен содержать не менее "
                f"{MIN_PASSWORD_LENGTH} символов."
            )
        password = args.password
    else:
        password = read_password_interactively()

    # Минимальное приложение Flask для доступа к моделям db_models
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + args.db
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    try:
        with app.app_context():
            user = User.query.filter_by(email=email).first()
            if user is None:
                sys.exit(
                    f"Ошибка: пользователь с почтой '{email}' не найден.\n"
                    "Проверьте адрес или посмотрите список пользователей "
                    "на странице «Пользователи» приложения."
                )
            user.set_password(password)
            db.session.commit()
            print(
                f"Пароль пользователя {user.username} <{user.email}> "
                f"(роль: {user.role}) успешно изменён."
            )
    except SystemExit:
        raise
    except Exception as exc:  # откат при любой ошибке БД
        db.session.rollback()
        sys.exit(f"Ошибка при обновлении пароля: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
