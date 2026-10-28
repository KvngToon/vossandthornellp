"""
Django settings for vossandthornellp project.
"""

import os
import dj_database_url
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ── Security ──────────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-dev-fallback-key-change-in-production')

DEBUG = os.environ.get('DEBUG', 'False') == 'True'

ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',')

# ── Applications ──────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    'jazzmin',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'tracking',
    'investors',
]

# ── Middleware ─────────────────────────────────────────────────────────────────
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'vossandthornellp.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'vossandthornellp.wsgi.application'

# ── Database ──────────────────────────────────────────────────────────────────
DATABASES = {
    'default': dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}
# Supabase's pooler drops idle SSL connections; without this, Django reuses a
# dead connection and returns 500s with "SSL connection has been closed".
DATABASES['default']['CONN_HEALTH_CHECKS'] = True

# ── Password validation ───────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 12}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ── Internationalisation ──────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# ── Sessions & cookies ────────────────────────────────────────────────────────
# Investors see financial data, so keep sessions short and cookies HTTPS-only
# outside local development (run locally with DEBUG=True).
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_HTTPONLY = True
PASSWORD_RESET_TIMEOUT = 60 * 60 * 24 * 3

# Render terminates TLS and forwards the original scheme in this header. Without
# it Django thinks every request is plain HTTP (breaking secure cookies, CSRF
# origin checks and absolute URLs).
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# Opt-in so local/test runs over http keep working. Enable in Render once you
# have confirmed the domain serves HTTPS end to end (a Cloudflare 'Flexible' SSL
# mode would cause a redirect loop). Raise HSTS gradually: 3600 first, then a year.
SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'False') == 'True'
SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', '0'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = False

# ── Withdrawals: country rule and bank-detail encryption ──────────────────────
# Secret shared with a Cloudflare Transform Rule that adds an `X-Origin-Verify` header; only
# requests carrying it are trusted to have an honest CF-IPCountry header. Unset = every
# withdrawal is refused (fails closed).
CF_ORIGIN_SECRET = os.environ.get('CF_ORIGIN_SECRET', '')
# Two-letter codes as Cloudflare reports them (the United Kingdom is GB).
WITHDRAWAL_ALLOWED_COUNTRIES = ('US', 'GB')
# Local development only (ignored unless DEBUG): pretend to be in this country, e.g. DEV_COUNTRY=FR to see the blocked state.
DEV_COUNTRY = os.environ.get('DEV_COUNTRY', 'US')
# Fernet key for encrypting bank details at rest. See investors/crypto.py. Keep a backup.
FIELD_ENCRYPTION_KEY = os.environ.get('FIELD_ENCRYPTION_KEY', '')

# ── Static & media files ──────────────────────────────────────────────────────
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATICFILES_STORAGE = 'whitenoise.storage.CompressedStaticFilesStorage'

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Investor documents. With SUPABASE_S3_BUCKET set they live in a private Supabase
# Storage bucket (S3-compatible API); otherwise in PRIVATE_MEDIA_ROOT locally.
# Keys come from Supabase dashboard -> Storage -> S3 Connection. Never commit them.
PRIVATE_MEDIA_ROOT = BASE_DIR / 'private_media'
SUPABASE_S3_BUCKET = os.environ.get('SUPABASE_S3_BUCKET', '')
SUPABASE_S3_ENDPOINT = os.environ.get('SUPABASE_S3_ENDPOINT', '')
SUPABASE_S3_REGION = os.environ.get('SUPABASE_S3_REGION', 'us-east-1')
SUPABASE_S3_ACCESS_KEY_ID = os.environ.get('SUPABASE_S3_ACCESS_KEY_ID', '')
SUPABASE_S3_SECRET_ACCESS_KEY = os.environ.get('SUPABASE_S3_SECRET_ACCESS_KEY', '')

# ── Resend email ──────────────────────────────────────────────────────────────
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
DEFAULT_FROM_EMAIL = 'Voss & Thorne LLP <noreply@shipment.vossandthornellp.org>'

# Subdomain that receives inbound client replies via Resend Inbound Email.
# Each shipment gets its own reply address: <tracking_number>@<REPLY_DOMAIN>
REPLY_DOMAIN = os.environ.get('REPLY_DOMAIN', 'reply.vossandthornellp.org')

# Signing secret for the Resend inbound webhook (Svix). Found in the Resend
# dashboard under Webhooks → the inbound endpoint → Signing Secret.
RESEND_WEBHOOK_SECRET = os.environ.get('RESEND_WEBHOOK_SECRET', '')

# Where to send a heads-up notification every time a message is sent OR
# received on the platform. ACTIVITY_NOTIFY_EMAIL is the current name;
# INBOUND_NOTIFY_EMAIL is read too so an already-configured Render env var
# keeps working without needing to be renamed.
ACTIVITY_NOTIFY_EMAIL = os.environ.get(
    'ACTIVITY_NOTIFY_EMAIL',
    os.environ.get('INBOUND_NOTIFY_EMAIL', 'kvngtoon001@gmail.com'),
)

# Base URL used to build links in notification emails (no trailing slash).
SITE_URL = os.environ.get('SITE_URL', 'https://vossandthornellp.org')

# ── Admin theme ───────────────────────────────────────────────────────────────
# Only the tab icon is customised; every other Jazzmin setting keeps its default.
JAZZMIN_SETTINGS = {'site_icon': 'apple-touch-icon.png'}

# ── Misc ───────────────────────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Logging ───────────────────────────────────────────────────────────────────
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '[{levelname}] {asctime} {name} {module}:{lineno} — {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        # Full Django internals: requests, security, template errors, etc.
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        # Every incoming HTTP request & response code
        'django.request': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        # DB queries (set to DEBUG to see SQL; WARNING to silence)
        'django.db.backends': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        # Security middleware warnings
        'django.security': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        # Template engine errors
        'django.template': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        # Admin & auth
        'django.contrib.admin': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
    },
}
