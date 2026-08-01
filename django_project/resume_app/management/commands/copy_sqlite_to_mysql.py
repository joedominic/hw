"""Copy all app data from a SQLite file into the configured MySQL/MariaDB database.

Preserves primary keys (with FOREIGN_KEY_CHECKS disabled) so FKs and
content-type IDs stay consistent with the source.

Usage (from django_project/, with MYSQL_* configured in .env):

  ..\\.venv\\Scripts\\python.exe manage.py migrate
  ..\\.venv\\Scripts\\python.exe manage.py copy_sqlite_to_mysql
  ..\\.venv\\Scripts\\python.exe manage.py copy_sqlite_to_mysql --sqlite db.sqlite3 --batch-size 400
"""
from __future__ import annotations

from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, router, transaction


# Ephemeral session rows are skipped unless --include-sessions.
SKIP_LABELS = {
    "sessions.Session",
}


def _label(model) -> str:
    return f"{model._meta.app_label}.{model.__name__}"


def _concrete_models():
    """Return concrete, managed models in an FK-safe forward order."""
    models = [
        m
        for m in apps.get_models()
        if m._meta.managed and not m._meta.proxy and not m._meta.auto_created
    ]
    # Prefer dependency order: models with fewer outgoing FKs first is wrong;
    # use Django's migrate order via app registry + topological-ish sort.
    # With FOREIGN_KEY_CHECKS=0 order does not matter for inserts; we still
    # clear in reverse and load auth/contenttypes early for readability.
    priority = {
        "contenttypes.ContentType": 0,
        "auth.Permission": 1,
        "auth.Group": 2,
        "auth.User": 3,
    }
    models.sort(key=lambda m: (priority.get(_label(m), 50), _label(m)))
    return models


class Command(BaseCommand):
    help = "Copy SQLite data into the default MySQL database (preserves PKs)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sqlite",
            default=str(Path(settings.BASE_DIR) / "db.sqlite3"),
            help="Path to source db.sqlite3 (default: django_project/db.sqlite3).",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=400,
            help="bulk_create batch size (default 400).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count source rows only; do not write to MySQL.",
        )
        parser.add_argument(
            "--include-sessions",
            action="store_true",
            help="Also copy django_session rows (default: skip).",
        )

    def handle(self, *args, **options):
        engine = settings.DATABASES["default"].get("ENGINE", "")
        if "mysql" not in engine:
            raise CommandError(
                "default DATABASES engine must be MySQL/MariaDB. "
                "Set MYSQL_DATABASE (and related MYSQL_*) in .env first."
            )

        sqlite_path = Path(options["sqlite"]).resolve()
        if not sqlite_path.is_file():
            raise CommandError(f"SQLite file not found: {sqlite_path}")

        batch_size = max(1, int(options["batch_size"]))
        dry_run = bool(options["dry_run"])
        include_sessions = bool(options["include_sessions"])

        # Register ephemeral sqlite alias for this process.
        settings.DATABASES["sqlite"] = {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(sqlite_path),
            "OPTIONS": {"timeout": 30},
            "TIME_ZONE": settings.TIME_ZONE,
            "CONN_MAX_AGE": 0,
            "CONN_HEALTH_CHECKS": False,
            "AUTOCOMMIT": True,
            "ATOMIC_REQUESTS": False,
        }
        # Ensure Django connection handler picks up the new alias.
        connections.databases["sqlite"] = settings.DATABASES["sqlite"]
        connections.close_all()

        # Sanity: source opens and has users (or at least migrations).
        try:
            with connections["sqlite"].cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                )
                table_count = cursor.fetchone()[0]
        except Exception as exc:
            raise CommandError(f"Cannot open SQLite source: {exc}") from exc

        self.stdout.write(
            f"Source SQLite: {sqlite_path} ({table_count} tables) -> "
            f"MySQL {settings.DATABASES['default'].get('HOST')}/"
            f"{settings.DATABASES['default'].get('NAME')}"
        )

        models = _concrete_models()
        if not include_sessions:
            models = [m for m in models if _label(m) not in SKIP_LABELS]

        if dry_run:
            for model in models:
                if not router.allow_migrate_model("default", model):
                    continue
                n = model.objects.using("sqlite").count()
                self.stdout.write(f"  {_label(model)}: {n}")
            self.stdout.write(self.style.WARNING("Dry run — no data written."))
            return

        dest = connections["default"]
        with dest.cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS=0")
            cursor.execute("SET UNIQUE_CHECKS=0")

        try:
            # Clear destination tables (schema kept; migrate history kept).
            for model in reversed(models):
                table = model._meta.db_table
                with dest.cursor() as cursor:
                    cursor.execute(f"TRUNCATE TABLE `{table}`")
                self.stdout.write(f"truncated {table}")

            # Also clear auto-created M2M through tables (not in get_models list
            # when auto_created=True — we skip those above). Truncate them too.
            for model in models:
                for m2m in model._meta.local_many_to_many:
                    through = m2m.remote_field.through
                    if not through._meta.auto_created:
                        continue
                    table = through._meta.db_table
                    with dest.cursor() as cursor:
                        cursor.execute(f"TRUNCATE TABLE `{table}`")

            total_rows = 0
            for model in models:
                if not router.allow_migrate_model("default", model):
                    continue
                copied = self._copy_model(model, batch_size=batch_size)
                total_rows += copied
                self.stdout.write(
                    self.style.SUCCESS(f"copied {_label(model)}: {copied}")
                )

            # Copy auto-created M2M through tables.
            for model in models:
                for m2m in model._meta.local_many_to_many:
                    through = m2m.remote_field.through
                    if not through._meta.auto_created:
                        continue
                    copied = self._copy_model(through, batch_size=batch_size)
                    total_rows += copied
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"copied M2M {_label(through)}: {copied}"
                        )
                    )

            # Fix AUTO_INCREMENT counters.
            for model in models:
                self._reset_autoincrement(model)
                for m2m in model._meta.local_many_to_many:
                    through = m2m.remote_field.through
                    if through._meta.auto_created:
                        self._reset_autoincrement(through)

        finally:
            with dest.cursor() as cursor:
                cursor.execute("SET UNIQUE_CHECKS=1")
                cursor.execute("SET FOREIGN_KEY_CHECKS=1")
            connections.close_all()

        self.stdout.write(
            self.style.SUCCESS(f"Done. Copied {total_rows} rows into MySQL.")
        )

    def _copy_model(self, model, *, batch_size: int) -> int:
        fields = [
            f
            for f in model._meta.local_fields
            if f.column is not None
        ]
        attnames = [f.attname for f in fields]
        qs = model.objects.using("sqlite").all().order_by(model._meta.pk.attname)
        batch: list = []
        copied = 0
        is_user = _label(model) == "auth.User"

        def flush():
            nonlocal batch, copied
            if not batch:
                return
            # One transaction per batch — avoids MariaDB 1180 on huge commits.
            with transaction.atomic(using="default"):
                model.objects.using("default").bulk_create(
                    batch, batch_size=batch_size
                )
            copied += len(batch)
            batch = []

        for src in qs.iterator(chunk_size=batch_size):
            kwargs = {name: getattr(src, name) for name in attnames}
            # SQLite used a partial unique index (email != ''); MySQL uses a full
            # unique index, so blank emails must be unique per row.
            if is_user and not (kwargs.get("email") or "").strip():
                kwargs["email"] = f"user-{kwargs.get('id')}.blank@localhost.invalid"
            batch.append(model(**kwargs))
            if len(batch) >= batch_size:
                flush()
        flush()
        return copied

    def _reset_autoincrement(self, model) -> None:
        pk = model._meta.pk
        if pk.get_internal_type() not in ("AutoField", "BigAutoField"):
            return
        table = model._meta.db_table
        with connections["default"].cursor() as cursor:
            cursor.execute(f"SELECT COALESCE(MAX(`{pk.column}`), 0) FROM `{table}`")
            max_id = int(cursor.fetchone()[0] or 0)
            cursor.execute(
                f"ALTER TABLE `{table}` AUTO_INCREMENT = %s", [max_id + 1]
            )
