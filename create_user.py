"""Утилита командной строки для создания нового пользователя NoteMaker.

Работает напрямую с базой данных SQLite (users.db), не требует настроенного
окружения языковой модели (переменных .env), поэтому её можно использовать
даже если приложение не запускается из-за ошибки конфигурации.

Если файл базы данных ещё не существует, утилита создаст пустую базу
данных со всеми таблицами.

Модель пользователя (User) и объект db импортируются из db_models.py —
это те же модели, что использует веб-приложение app.py.

Имя пользователя, адрес почты и роль можно передать параметрами командной
строки; если какой-то из них не указан, он будет запрошен с клавиатуры.
Пароль ВСЕГДА запрашивается только с клавиатуры: ввод скрыт и требует
подтверждения (повторного ввода).

Примеры использования:

    # все данные, кроме пароля, заданы явно
    python create_user.py --name "Иван" --email ivan@example.com --role student

    # интерактивный ввод недостающих данных
    python create_user.py

    # указать другой файл базы данных
    python create_user.py --name "Иван" --email ivan@example.com --db /path/to/users.db
"""

import argparse
import getpass
import os
import sys

from flask import Flask

from db_models import User, db

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "users.db")

# Доступные роли — те же, что и в веб-приложении (app.py, ROLES).
# Список продублирован, чтобы не импортировать app.py вместе с его
# конфигурацией (требующей .env с настройками языковой модели).
ROLES = ("administrator", "guest", "student", "teacher")

ROLE_LABELS = {
    "administrator": "Администратор",
    "guest": "Гость",
    "student": "Студент",
    "teacher": "Преподаватель",
}

DEFAULT_ROLE = "guest"

# Минимальная длина пароля — та же, что и при регистрации в app.py
MIN_PASSWORD_LENGTH = 6


def parse_args(argv=None) -> argparse.Namespace:
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Создание нового пользователя NoteMaker.",
        epilog=(
            "Имя, почта и роль, не переданные в командной строке, будут "
            "запрошены с клавиатуры. Пароль всегда запрашивается только с "
            "клавиатуры (ввод скрыт, требуется подтверждение)."
        ),
    )
    parser.add_argument(
        "--name",
        "--username",
        dest="name",
        default=None,
        help="имя пользователя (по умолчанию запрашивается с клавиатуры)",
    )
    parser.add_argument(
        "--email",
        default=None,
        help="адрес электронной почты (по умолчанию запрашивается с клавиатуры)",
    )
    parser.add_argument(
        "--role",
        default=None,
        help="роль пользователя; одна из: " + ", ".join(ROLES),
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_DB_PATH,
        help=f"путь к файлу базы данных SQLite (по умолчанию: {DEFAULT_DB_PATH})",
    )
    return parser.parse_args(argv)


def read_input(prompt: str) -> str:
    """Читает строку с клавиатуры, повторяя запрос до непустого ввода."""
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("Ошибка: значение не может быть пустым.")


def read_email_interactively() -> str:
    """Запрашивает адрес почты и проверяет его корректность."""
    while True:
        email = read_input("Адрес электронной почты: ").lower()
        if "@" in email and "." in email:
            return email
        print("Ошибка: укажите корректный адрес почты (например, user@example.com).")


def read_role_interactively() -> str:
    """Запрашивает роль, показывая список допустимых значений."""
    labels = ", ".join(
        f"{role} ({ROLE_LABELS[role]})" for role in ROLES
    )
    print(f"Доступные роли: {labels}")
    while True:
        role = read_input(f"Роль [{DEFAULT_ROLE}]: ").lower()
        if not role:
            return DEFAULT_ROLE
        if role in ROLES:
            return role
        print(f"Ошибка: недопустимая роль. Допустимые значения: {', '.join(ROLES)}.")


def read_password_interactively() -> str:
    """Запрашивает пароль дважды с маскировкой ввода и проверяет его."""
    while True:
        password = getpass.getpass("Пароль: ")
        if len(password) < MIN_PASSWORD_LENGTH:
            print(
                f"Ошибка: пароль должен содержать не менее "
                f"{MIN_PASSWORD_LENGTH} символов."
            )
            continue
        confirmation = getpass.getpass("Повторите пароль: ")
        if password != confirmation:
            print("Ошибка: пароли не совпадают. Попробуйте ещё раз.")
            continue
        return password


def collect_user_data(args: argparse.Namespace) -> tuple[str, str, str]:
    """Возвращает (имя, почта, роль), дополняя недостающие данные с клавиатуры."""
    name = args.name.strip() if args.name else None
    if not name:
        name = read_input("Имя пользователя: ")

    email = args.email.strip().lower() if args.email else None
    if not email:
        email = read_email_interactively()
    elif "@" not in email or "." not in email:
        sys.exit(f"Ошибка: некорректный адрес почты: '{email}'.")

    role = args.role.strip().lower() if args.role else None
    if not role:
        role = read_role_interactively()
    elif role not in ROLES:
        sys.exit(
            f"Ошибка: недопустимая роль '{role}'. "
            f"Допустимые значения: {', '.join(ROLES)}."
        )

    return name, email, role


def main(argv=None) -> int:
    args = parse_args(argv)

    name, email, role = collect_user_data(args)

    # Пароль всегда запрашивается только с клавиатуры
    password = read_password_interactively()

    # Минимальное приложение Flask для доступа к моделям db_models
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + args.db
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    try:
        with app.app_context():
            # Если файла базы данных ещё нет, создаем пустую БД
            # со всеми таблицами (SQLite создаст файл при подключении).
            if not os.path.exists(args.db):
                print(f"База данных не найдена, создается новая: {args.db}")
                db.create_all()
                print("База данных создана.")

            existing = User.query.filter_by(email=email).first()
            if existing is not None:
                sys.exit(
                    f"Ошибка: пользователь с почтой '{email}' уже существует "
                    f"({existing.username}, роль: {existing.role})."
                )
            user = User(username=name, email=email, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            print(
                f"Пользователь создан: {user.username} <{user.email}>, "
                f"роль: {role} ({ROLE_LABELS[role]})."
            )
    except SystemExit:
        raise
    except Exception as exc:  # откат при любой ошибке БД
        db.session.rollback()
        sys.exit(f"Ошибка при создании пользователя: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
