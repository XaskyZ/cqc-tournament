import os
from pathlib import Path
from .database import database_config

BASE_DIR = Path(__file__).resolve().parent.parent
for line in (BASE_DIR / '.env').read_text(encoding='utf-8-sig').splitlines() if (BASE_DIR / '.env').exists() else []:
    if '=' in line and not line.lstrip().startswith('#'):
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip())

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError('Set DJANGO_SECRET_KEY in .env. Run setup.ps1 for first launch.')
IS_VERCEL = os.environ.get('VERCEL') == '1'
RATE_LIMIT_TRUST_VERCEL_IP = IS_VERCEL
DEBUG = os.environ.get('DJANGO_DEBUG', '0' if IS_VERCEL else '1') == '1'
DEMO_MODE = os.environ.get('TOURNAMENT_DEMO_MODE', '0') == '1'
ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost,[::1]').split(',')
CSRF_TRUSTED_ORIGINS = [v for v in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',') if v]
INSTALLED_APPS = ['django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles', 'arena.apps.ArenaConfig']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'whitenoise.middleware.WhiteNoiseMiddleware', 'arena.middleware.SecurityHeadersMiddleware', 'arena.middleware.RateLimitMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware', 'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware', 'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.contrib.messages.middleware.MessageMiddleware', 'django.middleware.clickjacking.XFrameOptionsMiddleware', 'arena.middleware.TournamentTimezoneMiddleware']
ROOT_URLCONF = 'config.urls'
MIDDLEWARE.append('arena.rules.RulesAcceptanceMiddleware')
EVENT_TELEGRAM_ENABLED = os.environ.get('EVENT_TELEGRAM_ENABLED', '0') == '1'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True, 'OPTIONS': {'context_processors': ['django.template.context_processors.request', 'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages', 'arena.context_processors.tournament_context']}}]
WSGI_APPLICATION = 'config.wsgi.application'
DATABASES = {'default': database_config(os.environ, BASE_DIR, DEMO_MODE)}
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache', 'LOCATION': 'cqc-rate-limits'}}
AUTH_PASSWORD_VALIDATORS = [{'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'}, {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator', 'OPTIONS': {'min_length': 10}}, {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'}, {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'}]
LANGUAGE_CODE = 'ru'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles' if IS_VERCEL else BASE_DIR / '.local' / 'static'
WHITENOISE_USE_FINDERS = DEBUG
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/captain/'
LOGOUT_REDIRECT_URL = '/'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
SESSION_COOKIE_AGE = 43200
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'
SECURE_SSL_REDIRECT = not DEBUG
if IS_VERCEL:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
X_FRAME_OPTIONS = 'DENY'
DATA_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
DATA_UPLOAD_MAX_NUMBER_FIELDS = 300
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_WEBHOOK_SECRET = os.environ.get('TELEGRAM_WEBHOOK_SECRET', '')
TELEGRAM_BOT_USERNAME = os.environ.get('TELEGRAM_BOT_USERNAME', 'cqc_tournamentbot').lstrip('@')
_cashier = os.environ.get('TELEGRAM_CASHIER_IDS', '').strip()
if _cashier and (not _cashier.isascii() or not _cashier.isdecimal() or int(_cashier) <= 0):
    raise RuntimeError('Exactly one positive TELEGRAM_CASHIER_IDS value is required')
TELEGRAM_CASHIER_ID = int(_cashier) if _cashier else None
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'http://127.0.0.1:8000').rstrip('/')
MLBB_LOOKUP_PROVIDER = os.environ.get('MLBB_LOOKUP_PROVIDER', 'disabled')
MOOGOLD_PARTNER_ID = os.environ.get('MOOGOLD_PARTNER_ID', '')
MOOGOLD_SECRET_KEY = os.environ.get('MOOGOLD_SECRET_KEY', '')
MOOGOLD_MLBB_PRODUCT_ID = os.environ.get('MOOGOLD_MLBB_PRODUCT_ID', '')
MOOGOLD_MLBB_USER_ID_FIELD = os.environ.get('MOOGOLD_MLBB_USER_ID_FIELD', '')
MOOGOLD_MLBB_SERVER_ID_FIELD = os.environ.get('MOOGOLD_MLBB_SERVER_ID_FIELD', '')
MOOGOLD_MLBB_CONTRACT_CONFIRMED = os.environ.get('MOOGOLD_MLBB_CONTRACT_CONFIRMED', '0') == '1'
MLBB_LOOKUP_TIMEOUT = 5
