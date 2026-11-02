import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parents[3]
SECRET_KEY = os.getenv("SECRET_KEY", "local-only-change-before-production")
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
APP_ENV = os.getenv("APP_ENV", "development")
if APP_ENV == "production" and (DEBUG or SECRET_KEY == "local-only-change-before-production"):
    raise RuntimeError("Production requires DEBUG=false and a non-default SECRET_KEY")

ALLOWED_HOSTS = [
    value.strip()
    for value in os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
    if value.strip()
]
ROOT_URLCONF = "platform_api.urls"
ASGI_APPLICATION = "platform_api.asgi.application"
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "channels",
    "corsheaders",
    "core",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
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
            ],
        },
    },
]
CORS_ALLOW_CREDENTIALS = True
CSRF_TRUSTED_ORIGINS = [
    value.strip()
    for value in os.getenv(
        "CSRF_TRUSTED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if value.strip()
]
CORS_ALLOWED_ORIGINS = [
    value.strip()
    for value in os.getenv(
        "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
    if value.strip()
]
DATABASES = {
    "default": dj_database_url.config(
        default="postgresql://agent:agent_dev_password@localhost:5432/agent_stack",
        conn_max_age=60,
        conn_health_checks=True,
    )
}
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
MODEL_PROVIDER_ALLOWED_HOSTS = [
    value.strip().lower()
    for value in os.getenv("MODEL_PROVIDER_ALLOWED_HOSTS", "").split(",")
    if value.strip()
]
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.BasicAuthentication",
    ],
}
