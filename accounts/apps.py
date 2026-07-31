from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self):
        # Imported for its side effect: connecting the signup -> tenant hook.
        from . import signals  # noqa: F401
