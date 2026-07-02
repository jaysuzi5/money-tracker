import os
import environ

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

env = environ.Env()
_env_file = os.path.join(BASE_DIR, '.env')
if os.path.exists(_env_file):
    environ.Env.read_env(_env_file)

SECRET_KEY = env('SECRET_KEY', default='dev-insecure-key-change-me')
DEBUG = env.bool('DEBUG', default=False)
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS', default=['localhost', '127.0.0.1'])

# Fernet key used to encrypt connection credentials/tokens at rest (see tracker.crypto)
FERNET_KEY = env('FERNET_KEY', default='')

# SimpleFIN Bridge access URL (one-time setup token exchanged for an access URL)
SIMPLEFIN_ACCESS_URL = env('SIMPLEFIN_ACCESS_URL', default='')

# Finance agent (Groq-backed chat). Conversations stored locally (tracker.AgentCall),
# exposed read-only via /api/agent-calls/ (bearer token), and — if HOMELAB_AGENT_DB_URL
# is set — written in real time into homelab-hub's dashboard_agentcall table so the hub
# telemetry page is the single pane of glass.
GROQ_API_KEY = env('GROQ_API_KEY', default='')
AGENT_LOG_TOKEN = env('AGENT_LOG_TOKEN', default='')
HOMELAB_AGENT_DB_URL = env('HOMELAB_AGENT_DB_URL', default='')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'tracker',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'config.middleware.PageLoggingMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
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

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': env.db('DATABASE_URL', default='sqlite:///' + os.path.join(BASE_DIR, 'db.sqlite3')),
}
# homelab-hub Postgres — real-time agent-log mirror into its dashboard_agentcall table.
if HOMELAB_AGENT_DB_URL:
    DATABASES['homelab'] = environ.Env.db_url(HOMELAB_AGENT_DB_URL)
    DATABASE_ROUTERS = ['config.dbrouters.HomelabRouter']

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'America/New_York'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]

STORAGES = {
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/accounts/login/'

CSRF_TRUSTED_ORIGINS = env.list(
    'CSRF_TRUSTED_ORIGINS',
    default=['http://localhost:8000', 'http://127.0.0.1:8000'],
)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {'()': 'config.logging_utils.JsonFormatter'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'json'},
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'tracker': {'handlers': ['console'], 'level': 'DEBUG', 'propagate': False},
        'page': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}

# LAN-only deployment: no public TLS termination. Keep cookies non-secure for plain http
# on the internal network; flip these via env if fronted by a TLS proxy later.
if os.getenv('DJANGO_ENV') == 'production':
    # ingress-nginx terminates TLS and forwards X-Forwarded-Proto
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SESSION_COOKIE_SECURE = env.bool('COOKIE_SECURE', default=True)
    CSRF_COOKIE_SECURE = env.bool('COOKIE_SECURE', default=True)

OTEL_SERVICE_NAME = env('OTEL_SERVICE_NAME', default='money-tracker')
OTLP_ENDPOINT = env('OTLP_ENDPOINT', default='')
