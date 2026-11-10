"""Django settings for the project."""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import dj_database_url
import django_stubs_ext
import structlog
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
django_stubs_ext.monkeypatch()
try:
    _APP_VERSION = _package_version("ebook-server")
except PackageNotFoundError:  # pragma: no cover - running from a bare checkout
    _APP_VERSION = "0"
DEFAULT_METADATA_USER_AGENT = (
    f"ebook-server/{_APP_VERSION} (+https://github.com/; self-hosted hackerspace library)"
)
DEV_SECRET_KEY = "dev-only-secret-change-me-before-production-0123456789abcdef"


class Settings(BaseSettings):
    environment: Literal["development", "test", "production"] = "development"
    secret_key: SecretStr = SecretStr(DEV_SECRET_KEY)
    debug: bool = False
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    default_database_url: str = "sqlite:///db.sqlite3"
    database_url: str | None = None
    storage_root: str = str(BASE_DIR / "data" / "storage")
    storage_require_existing_root: bool = True
    storage_sentinel_file: str = ""
    source_min_age_seconds: int = 60
    ingest_max_auto_attempts: int = 3
    redis_url: str | None = None
    queue_name: str = "ebook-server"
    queue_job_timeout_seconds: int = 30 * 60
    queue_result_ttl_seconds: int = 24 * 60 * 60
    queue_failure_ttl_seconds: int = 7 * 24 * 60 * 60
    queue_retry_max: int = 3
    queue_retry_intervals: list[int] = [30, 120, 600]
    ldap_mode: str = "stub"
    ldap_server_uri: str = ""
    ldap_user_dn_template: str = ""
    ldap_user_search_base: str = ""
    ldap_user_search_filter: str = "(uid={username})"
    ldap_service_bind_dn: str = ""
    ldap_service_bind_password: SecretStr = SecretStr("")
    ldap_email_attribute: str = "mail"
    ldap_connect_timeout: float = 5.0
    ldap_start_tls: bool = False
    ldap_ca_file: str = ""
    ldap_tls_require_cert: bool = True
    ldap_username_attribute: str = "uid"
    log_level: str = "INFO"
    metrics_enabled: bool = True
    metrics_bearer_token: SecretStr = SecretStr("")
    max_upload_bytes: int = 150 * 1024 * 1024
    metadata_providers: list[str] = [
        "ebook_server.core.identification.OpenLibraryMetadataProvider",
    ]
    metadata_user_agent: str = DEFAULT_METADATA_USER_AGENT
    identification_auto_accept: float = 0.86
    identification_review: float = 0.55
    ocr_service_url: str = "http://localhost:8020"
    ocr_request_timeout: float = 30.0
    ocr_max_attempts: int = 40
    ocr_poll_interval_seconds: float = 0.2
    secure_ssl_redirect: bool = False
    secure_hsts_seconds: int = 0
    secure_hsts_include_subdomains: bool = True
    secure_hsts_preload: bool = False
    secure_proxy_ssl_header: bool = False
    session_cookie_secure: bool = False
    csrf_cookie_secure: bool = False
    csrf_trusted_origins: list[str] = []
    scantailor_event_stream_interval_seconds: float = 1.0
    scantailor_event_stream_max_events: int = 45

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_prefix="BOOK_SCANNER_",
    )

    @model_validator(mode="after")
    def validate_production_safety(self) -> Settings:
        if self.environment != "production":
            return self
        errors: list[str] = []
        if self.secret_key.get_secret_value() == DEV_SECRET_KEY:
            errors.append("secret_key must be changed")
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            errors.append("allowed_hosts must explicitly name production hosts")
        database_url = self.database_url or self.default_database_url
        if database_url.startswith("sqlite"):
            errors.append("PostgreSQL database_url is required")
        if not self.redis_url:
            errors.append("redis_url is required for asynchronous work")
        if self.ldap_mode != "ldap":
            errors.append("ldap_mode must be 'ldap'")
        if self.ldap_mode == "ldap":
            parsed_uri = urlparse(self.ldap_server_uri.strip())
            if parsed_uri.scheme not in {"ldap", "ldaps"} or not parsed_uri.hostname:
                errors.append("ldap_server_uri must be an ldap:// or ldaps:// URI")
            if not (self.ldap_user_dn_template.strip() or self.ldap_user_search_base.strip()):
                errors.append("ldap_user_dn_template or ldap_user_search_base is required")
            if self.ldap_start_tls and parsed_uri.scheme == "ldaps":
                errors.append("ldap_start_tls cannot be combined with an ldaps:// URI")
            if self.ldap_ca_file and not Path(self.ldap_ca_file).is_file():
                errors.append("ldap_ca_file does not exist")
            if self.ldap_user_dn_template and "{username}" not in self.ldap_user_dn_template:
                errors.append("ldap_user_dn_template must contain {username}")
        if not self.secure_ssl_redirect:
            errors.append("secure_ssl_redirect must be enabled")
        if not self.session_cookie_secure or not self.csrf_cookie_secure:
            errors.append("secure session and CSRF cookies must be enabled")
        if self.secure_hsts_seconds <= 0:
            errors.append("secure_hsts_seconds must be positive")
        if self.metrics_enabled and not self.metrics_bearer_token.get_secret_value():
            errors.append("metrics_bearer_token is required when metrics are enabled")
        if errors:
            raise ValueError("unsafe production configuration: " + "; ".join(errors))
        return self


settings = Settings()

if settings.secure_ssl_redirect and not settings.secure_proxy_ssl_header:
    logging.getLogger(__name__).warning(
        "SECURE_SSL_REDIRECT is enabled but secure_proxy_ssl_header is off: behind a "
        "TLS-terminating reverse proxy every request looks like plain HTTP and will "
        "redirect forever. Set BOOK_SCANNER_SECURE_PROXY_SSL_HEADER=true if the proxy "
        "sends X-Forwarded-Proto.",
    )


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
    "ebook_server.core.middleware.LocalBanMiddleware",
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
STATIC_ROOT = BASE_DIR / "staticfiles"
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/library/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

APP_DATA_DIR = Path(settings.storage_root)
STORAGE_REQUIRE_EXISTING_ROOT = settings.storage_require_existing_root
STORAGE_SENTINEL_FILE = settings.storage_sentinel_file
if settings.environment != "production" and STORAGE_REQUIRE_EXISTING_ROOT:
    # Development convenience only: production must never create the root itself,
    # because that is how an unmounted share silently fills the local disk.
    try:
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
BOOK_SCANNER_SOURCE_MIN_AGE_SECONDS = settings.source_min_age_seconds
BOOK_SCANNER_INGEST_MAX_AUTO_ATTEMPTS = settings.ingest_max_auto_attempts
BOOK_SCANNER_METADATA_USER_AGENT = settings.metadata_user_agent
BOOK_SCANNER_QUEUE_URL = settings.redis_url
BOOK_SCANNER_QUEUE_NAME = settings.queue_name
BOOK_SCANNER_QUEUE_JOB_TIMEOUT_SECONDS = settings.queue_job_timeout_seconds
BOOK_SCANNER_QUEUE_RESULT_TTL_SECONDS = settings.queue_result_ttl_seconds
BOOK_SCANNER_QUEUE_FAILURE_TTL_SECONDS = settings.queue_failure_ttl_seconds
BOOK_SCANNER_QUEUE_RETRY_MAX = settings.queue_retry_max
BOOK_SCANNER_QUEUE_RETRY_INTERVALS = settings.queue_retry_intervals
BOOK_SCANNER_LDAP_MODE = settings.ldap_mode
BOOK_SCANNER_LDAP_SERVER_URI = settings.ldap_server_uri
BOOK_SCANNER_LDAP_USER_DN_TEMPLATE = settings.ldap_user_dn_template
BOOK_SCANNER_LDAP_USER_SEARCH_BASE = settings.ldap_user_search_base
BOOK_SCANNER_LDAP_USER_SEARCH_FILTER = settings.ldap_user_search_filter
BOOK_SCANNER_LDAP_SERVICE_BIND_DN = settings.ldap_service_bind_dn
BOOK_SCANNER_LDAP_SERVICE_BIND_PASSWORD = settings.ldap_service_bind_password.get_secret_value()
BOOK_SCANNER_LDAP_EMAIL_ATTRIBUTE = settings.ldap_email_attribute
BOOK_SCANNER_LDAP_CONNECT_TIMEOUT = settings.ldap_connect_timeout
BOOK_SCANNER_LDAP_START_TLS = settings.ldap_start_tls
BOOK_SCANNER_LDAP_CA_FILE = settings.ldap_ca_file
BOOK_SCANNER_LDAP_TLS_REQUIRE_CERT = settings.ldap_tls_require_cert
BOOK_SCANNER_LDAP_USERNAME_ATTRIBUTE = settings.ldap_username_attribute
BOOK_SCANNER_MAX_UPLOAD_BYTES = settings.max_upload_bytes
BOOK_SCANNER_METADATA_PROVIDERS = settings.metadata_providers
BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT = settings.identification_auto_accept
BOOK_SCANNER_IDENTIFICATION_REVIEW = settings.identification_review
BOOK_SCANNER_OCR_SERVICE_URL = settings.ocr_service_url
BOOK_SCANNER_OCR_REQUEST_TIMEOUT = settings.ocr_request_timeout
BOOK_SCANNER_OCR_MAX_ATTEMPTS = settings.ocr_max_attempts
BOOK_SCANNER_OCR_POLL_INTERVAL_SECONDS = settings.ocr_poll_interval_seconds
BOOK_SCANNER_METRICS_ENABLED = settings.metrics_enabled
BOOK_SCANNER_METRICS_BEARER_TOKEN = settings.metrics_bearer_token.get_secret_value()
SCANTAILOR_EVENT_STREAM_INTERVAL_SECONDS = settings.scantailor_event_stream_interval_seconds
SCANTAILOR_EVENT_STREAM_MAX_EVENTS = settings.scantailor_event_stream_max_events

SECURE_SSL_REDIRECT = settings.secure_ssl_redirect
SECURE_HSTS_SECONDS = settings.secure_hsts_seconds
SECURE_HSTS_INCLUDE_SUBDOMAINS = settings.secure_hsts_include_subdomains
SECURE_HSTS_PRELOAD = settings.secure_hsts_preload
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https") if settings.secure_proxy_ssl_header else None
)
SESSION_COOKIE_SECURE = settings.session_cookie_secure
CSRF_COOKIE_SECURE = settings.csrf_cookie_secure
CSRF_TRUSTED_ORIGINS = settings.csrf_trusted_origins

_SHARED_LOG_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
]
structlog.configure(
    processors=[
        *_SHARED_LOG_PROCESSORS,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": structlog.stdlib.ProcessorFormatter,
            "foreign_pre_chain": _SHARED_LOG_PROCESSORS,
            "processors": [
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": settings.log_level,
    },
    "loggers": {
        "pikepdf": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
