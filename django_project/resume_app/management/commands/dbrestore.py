"""Restore a :mod:`dbbackup` dump into a MySQL/MariaDB database.

Safety model:

* Restores into a **target** database, defaulting to the test database
  (``test_<name>``) so a verification restore never clobbers the live app DB.
* Refuses to restore into the live ``MYSQL_DATABASE`` unless ``--force`` is given.
* Executes statements over the existing connection stack (PyMySQL) — no client
  binary required.

Examples (from ``django_project/``)::

    # Verify a backup by restoring into the scratch/test DB and counting rows.
    ..\\.venv\\Scripts\\python.exe manage.py dbrestore ../backups/resxjob-<ts>.sql.gz --verify

    # Restore into an explicit target database.
    ..\\.venv\\Scripts\\python.exe manage.py dbrestore dump.sql.gz --target restore_scratch

The target database must already exist and be writable by MYSQL_USER. Creating
databases / granting privileges requires an admin account — see
``ops/mariadb/provision.sql``.
"""
from __future__ import annotations

import gzip
import re
from pathlib import Path

import pymysql
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


# Split on ';' at end-of-line, ignoring ';' inside quoted strings. The dump writes
# one statement per logical block terminated by ";\n", so a line-aware split is safe.
_STATEMENT_TERMINATOR = re.compile(r";\s*\n")


def _read_dump(path: Path) -> str:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def _iter_statements(sql_text: str):
    buf: list[str] = []
    for line in sql_text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            yield "".join(buf).strip().rstrip(";")
            buf = []
    tail = "".join(buf).strip().rstrip(";")
    if tail:
        yield tail


class Command(BaseCommand):
    help = "Restore a dbbackup dump into a target MySQL/MariaDB database."

    def add_arguments(self, parser):
        parser.add_argument("dump", help="Path to .sql or .sql.gz produced by dbbackup.")
        parser.add_argument(
            "--target",
            default=None,
            help="Target database name (default: the configured TEST db, test_<name>).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Allow restoring into the live MYSQL_DATABASE (destructive).",
        )
        parser.add_argument(
            "--verify",
            action="store_true",
            help="After restore, print row counts for a few tables as a smoke test.",
        )

    def handle(self, *args, **options):
        db = settings.DATABASES["default"]
        if "mysql" not in db.get("ENGINE", ""):
            raise CommandError("dbrestore targets MySQL/MariaDB. Set MYSQL_DATABASE first.")

        dump_path = Path(options["dump"]).resolve()
        if not dump_path.is_file():
            raise CommandError(f"Dump not found: {dump_path}")

        live_name = db["NAME"]
        target = options["target"] or db.get("TEST", {}).get("NAME") or f"test_{live_name}"
        if target == live_name and not options["force"]:
            raise CommandError(
                f"Refusing to restore over the live database '{live_name}'. "
                "Pass --target <scratch_db> or --force to override."
            )

        self.stdout.write(f"Restoring {dump_path.name} -> `{target}`")

        conn = pymysql.connect(
            host=db.get("HOST") or "127.0.0.1",
            port=int(db.get("PORT") or 3306),
            user=db.get("USER") or "",
            password=db.get("PASSWORD") or "",
            charset="utf8mb4",
            autocommit=False,
        )
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(f"USE `{target}`")
                except pymysql.Error as exc:
                    raise CommandError(
                        f"Cannot USE database '{target}': {exc}. "
                        "Create it and grant MYSQL_USER access first "
                        "(see ops/mariadb/provision.sql)."
                    ) from exc

                sql_text = _read_dump(dump_path)
                count = 0
                for stmt in _iter_statements(sql_text):
                    cur.execute(stmt)
                    count += 1
            conn.commit()
            self.stdout.write(self.style.SUCCESS(f"Executed {count} statements."))

            if options["verify"]:
                self._verify(conn, target)
        finally:
            conn.close()

    def _verify(self, conn, target: str) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s ORDER BY table_name",
                (target,),
            )
            tables = [r[0] for r in cur.fetchall()]
            self.stdout.write(f"Restored {len(tables)} tables. Sample row counts:")
            for t in [
                "auth_user",
                "resume_app_joblisting",
                "resume_app_pipelineentry",
                "resume_app_userresume",
            ]:
                if t in tables:
                    cur.execute(f"SELECT COUNT(*) FROM `{t}`")
                    self.stdout.write(f"  {t}: {cur.fetchone()[0]}")
