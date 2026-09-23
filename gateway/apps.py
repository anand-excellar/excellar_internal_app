from django.apps import AppConfig


class GatewayConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gateway"

    def ready(self):
        # Registers the startup checks that refuse to serve an unsafe config.
        from portal import checks  # noqa: F401