"""Django settings for the Sahlisoft platform.

Multi-tenant: each shop (tenant) signs in and sees only its own data, which
is pushed here by the on-premise agent installed on the shop machine.
Secrets and host names come from the environment so the same tree runs on a
laptop and on the VPS without edits.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_list(name: str) -> list[str]:
    """Comma-separated env var -> list, ignoring blanks."""
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


# SECURITY: the fallback only exists so a fresh checkout runs; set SECRET_KEY
# in the environment before serving anything real.
SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY",
    "django-insecure-4i4_%)%r2bng%m)zv6(l+2!gl^s1amf59q^20#m4qv_#r$$9u@",
)

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS") or (["*"] if DEBUG else [])

# Behind a cloudflared tunnel Django sees plain HTTP from the connector while
# the browser is on HTTPS; without these two, CSRF rejects every POST (login
# included) and request.is_secure() lies.
CSRF_TRUSTED_ORIGINS = _env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "allauth",
    "allauth.account",
    "accounts",
    "assistant",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    # Must run after authentication: it checks the signed-in user-s
    # membership of the shop named by the subdomain.
    "accounts.middleware.TenantResolutionMiddleware",
]

ROOT_URLCONF = "project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "project.wsgi.application"
ASGI_APPLICATION = "project.asgi.application"


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


# A file-backed cache, not the default in-process one, because the only thing
# that uses it -- the activation endpoint's throttle (accounts/throttle.py) --
# is worthless unless every gunicorn worker counts against the same tally. Two
# workers each holding their own LocMemCache would silently double whatever
# limit the code says it enforces. Sqlite-on-one-box already assumes a single
# machine, so a shared directory is enough; a real Redis would be the answer
# only once this runs on more than one host.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": os.environ.get("DJANGO_CACHE_DIR", str(BASE_DIR / "cache")),
    }
}


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Identity is our own model: shops sign in with an email, never a username.
AUTH_USER_MODEL = "accounts.User"

AUTHENTICATION_BACKENDS = [
    # Kept so the Django admin still accepts a superuser login directly.
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

LOGIN_URL = "account_login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "account_login"

# allauth. These names are the current ones: ACCOUNT_EMAIL_REQUIRED,
# ACCOUNT_USERNAME_REQUIRED and ACCOUNT_AUTHENTICATION_METHOD were removed
# upstream and silently do nothing if copied from an older tutorial.
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None  # our User has no username column
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_EMAIL_VERIFICATION = os.environ.get("ACCOUNT_EMAIL_VERIFICATION", "optional")
ACCOUNT_SESSION_REMEMBER = None  # show a "remember me" checkbox
ACCOUNT_LOGOUT_ON_GET = False

# Email goes out through Resend, which speaks plain SMTP - no extra package.
# Without a key we fall back to the console backend so a dev checkout still
# runs and verification mails are visible in the log rather than silently lost.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

if RESEND_API_KEY:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = "smtp.resend.com"
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = "resend"
    EMAIL_HOST_PASSWORD = RESEND_API_KEY
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# Must be an address on a domain verified in the Resend dashboard, otherwise
# Resend rejects the send with a 403.
DEFAULT_FROM_EMAIL = os.environ.get(
    "DJANGO_DEFAULT_FROM_EMAIL", "no-reply@bytebalancetech.com"
)
SERVER_EMAIL = DEFAULT_FROM_EMAIL


# The data is Arabic (Sahlisoft stores it in WIN1256 and the agent decodes it
# before sending), and the shops are in Syria.
LANGUAGE_CODE = "ar"
TIME_ZONE = "Asia/Damascus"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# ---------------------------------------------------------------- tenancy

# Every shop gets its own subdomain under this zone. The wildcard DNS record
# and the tunnel ingress already cover *.PLATFORM_DOMAIN, and one Apache vhost
# (ServerAlias *.bytebalancetech.com) hands every unclaimed subdomain to us, so
# onboarding a shop needs no infrastructure change at all.
PLATFORM_DOMAIN = os.environ.get("PLATFORM_DOMAIN", "bytebalancetech.com")

# Subdomains that must never be mistaken for a shop slug. Some are already
# taken by other services on this box, the rest are reserved so a customer can
# never register a name that shadows platform infrastructure.
RESERVED_SUBDOMAINS = {
    "www", "app", "api", "admin", "auth", "static", "media", "cdn",
    "mail", "send", "smtp", "financial", "monitor", "status", "docs",
}


# ---------------------------------------------------------- hardening (prod)

# Cookies stay host-scoped on purpose: SESSION_COOKIE_DOMAIN is deliberately
# left unset so a session minted on one shop subdomain cannot be replayed on
# another. Membership checks are the second line of defence, not the first.
if not DEBUG:
    SECURE_SSL_REDIRECT = True  # safe: Apache sets X-Forwarded-Proto
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"


# ---------------------------------------------------------------- assistant

# The in-dashboard assistant (assistant/) calls the Claude API on the owner's
# behalf. Left unset, the feature stays reachable but every run finishes with
# "not enabled on this platform yet" rather than a traceback -- same shape as
# RESEND_API_KEY above, so a dev checkout runs without any credential.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
