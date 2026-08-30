from django.apps import AppConfig


class ResumeAppConfig(AppConfig):
    name = "resume_app"

    def ready(self):
        import resume_app.onboarding  # noqa: F401 — seed defaults on signup
        import resume_app.hijack_handlers  # noqa: F401 — impersonation audit

        from resume_app.account import install_user_email_signals
        from resume_app.application.event_handlers import register_event_handlers

        install_user_email_signals()
        register_event_handlers()

        # Reduce "database is locked" when Huey and the dev server share SQLite.
        from django.db.backends.signals import connection_created

        def _sqlite_setup(sender, connection, **kwargs):
            if connection.vendor != "sqlite":
                return
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA journal_mode=WAL;")
                cursor.execute("PRAGMA busy_timeout=30000;")

        connection_created.connect(_sqlite_setup)
