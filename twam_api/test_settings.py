import sys
import types
from django.db import models
from pathlib import Path

# --- MOCK POSTGRES FIELDS FOR SQLITE ---
class MockArrayField(models.JSONField):
    def __init__(self, base_field=None, size=None, **kwargs):
        if 'default' not in kwargs:
            kwargs['default'] = list
        # Remove postgres-specific arguments
        # base_field and size are captured in signature
        super().__init__(**kwargs)

    def db_type(self, connection):
        return 'text'

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        return name, path, args, kwargs

# Create module structure
fields_mod = types.ModuleType("django.contrib.postgres.fields")
fields_mod.ArrayField = MockArrayField
sys.modules["django.contrib.postgres.fields"] = fields_mod

postgres_mod = types.ModuleType("django.contrib.postgres")
postgres_mod.fields = fields_mod
sys.modules["django.contrib.postgres"] = postgres_mod

# Ensure django.contrib exists and has postgres
try:
    import django.contrib
    django.contrib.postgres = postgres_mod
except ImportError:
    pass 

# Also mock django.contrib.postgres.search if needed by other migrations?
search_mod = types.ModuleType("django.contrib.postgres.search")
sys.modules["django.contrib.postgres.search"] = search_mod
postgres_mod.search = search_mod
# ---------------------------------------

from pathlib import Path
from datetime import timedelta
import os
import environ

# Initialize environ
env = environ.Env()
mode = "prod"

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

if "WEBSITE_NAME" in os.environ:
    # Running in Azure -> Load from environment variables (No .env file)
    env.read_env()  # This won't load any file, just ensures env is initialized
else:
    # Read the .env file
    if mode == "dev":
        environ.Env.read_env(os.path.join(BASE_DIR, '.env.dev'))
    else:
        environ.Env.read_env(os.path.join(BASE_DIR, '.env'))

# Export the env object
# __all__ = ['env']

# Load environment variables
SECRET_KEY = "django-insecure-k4=cb+x_0xscqf@$#6a8tz-j22fy8y!*qtq(2hgu9-p46wc6pq"
API_SECRET_KEY = "PWa6Nq51HnWRzh2gXHtJR25wHIv4muGA"
OTP_RESEND_INTERVAL = 30
OTP_MAX_RETRIES = 3
OTP_COOLDOWN_PERIOD = 1800
AWS_SES_VERIFIED_SENDER = "noreply@twam.com"
AWS_REGION_NAME = "us-east-1"


STRIPE_SECRET_KEY = env('STRIPE_SECRET_KEY', default='')
STRIPE_WEBHOOK_SECRET = env('STRIPE_WEBHOOK_SECRET', default='')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True
ALLOWED_HOSTS = [env('ALLOWED_HOSTS', default='*')]

AUTH_USER_MODEL = 'api.User'

# Application definition
INSTALLED_APPS = [
    "corsheaders",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "api",
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "twam_api.middleware.ApiSecretMiddleware",
]

ROOT_URLCONF = "twam_api.urls"
CORS_ALLOW_HEADERS = ["x-api-secret", "content-type", "authorization"]
CORS_ALLOW_ALL_ORIGINS = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_SSL_REDIRECT = False
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CORS_ALLOW_CREDENTIALS = True
APPEND_SLASH = True

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

WSGI_APPLICATION = "twam_api.wsgi.application"

# Tests run on an in-memory SQLite database (ArrayField is mocked above), so
# they never need a real database or any credentials.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=7),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=30),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
}

AUTHENTICATION_BACKENDS = [
    "api.authentication_backends.EmailBackend",
    # "api.authentication_backends.GoogleAuthBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'level': 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        'core.middleware': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': True,
        },
    },
}


LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# --- TEST OVERRIDES ---
BASE_DIR = Path(__file__).resolve().parent.parent

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'test_db.sqlite3',
    }
}

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
EMAIL_BACKEND = 'django.core.mail.backends.dummy.EmailBackend'
