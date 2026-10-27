from django.apps import AppConfig


class InvestorsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'investors'

    def ready(self):
        from investors import signals  # noqa: F401
