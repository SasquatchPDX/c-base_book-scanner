"""Django settings for the project."""

from __future__ import annotations

from pathlib import Path

import dj_database_url
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    secret_key: SecretStr = SecretStr("dev-only-secret-change-me")
    debug: bool = False
    allowed_hosts: list[str] = ["*"]
    default_database_url: str = "sqlite:///db.sqlite3"
    database_url: str | None = None
    storage_root: str = str(BASE_DIR / "data" / "storage")
    redis_url: str | None = None
    ldap_mode: str = "stub"
    log_level: str = "INFO"
    max_upload_bytes: int = 150 * 1024 * 1024
    metadata_providers: list[str] = [
        "ebook_server.core.identification.OpenLibraryMetadataProvider",
    ]
    identification_auto_accept: float = 0.86
    identification_review: float = 0.55

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_prefix="BOOK_SCANNER_",
    )


settings = Settings()


SECRET_KEY = settings.secret_key.get_secret_value()
DEBUG = settings.debug

ALLOWED_HOSTS = settings.allowed_hosts
ROOT_URLCONF = "ebook_server.urls"
WSGI_APPLICATION = "ebook_server.wsgi.application"
ASGI_APPLICATION = "ebook_server.asgi.application"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "ebook_server.core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.locale.LocaleMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "ebook_server.core.auth.LdapBackend",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DATABASES = {
    "default": dj_database_url.parse(
        settings.database_url or settings.default_database_url,
        conn_max_age=600,
    )
}

AUTH_PASSWORD_VALIDATORS: list[dict[str, str]] = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

APP_DATA_DIR = Path(settings.storage_root)
BOOK_SCANNER_QUEUE_URL = settings.redis_url
BOOK_SCANNER_LDAP_MODE = settings.ldap_mode
BOOK_SCANNER_MAX_UPLOAD_BYTES = settings.max_upload_bytes
BOOK_SCANNER_METADATA_PROVIDERS = settings.metadata_providers
BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT = settings.identification_auto_accept
BOOK_SCANNER_IDENTIFICATION_REVIEW = settings.identification_review

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": settings.log_level,
    },
}
