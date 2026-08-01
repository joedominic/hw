"""Create a logical backup of the configured MySQL/MariaDB database.

Two strategies, auto-selected:

1. Native ``mysqldump`` / ``mariadb-dump`` when a binary is available (fast,
   canonical). Configure an explicit path with ``MYSQL_DUMP_BIN`` or ``--dump-bin``.
2. Pure-Python fallback via the existing DB connection (no client tools needed;
   works on Windows). Produces a restorable ``.sql`` file compatible with
   :mod:`resume_app.management.commands.dbrestore`.

Output is gzipped into ``BASE_DIR/backups`` by default.

Examples (from ``django_project/``)::

    ..\\.venv\\Scripts\\python.exe manage.py dbbackup
    ..\\.venv\\Scripts\\python.exe manage.py dbbackup --out ../backups --no-gzip
    ..\\.venv\\Scripts\\python.exe manage.py dbbackup --dump-bin mariadb-dump

TODO(ops): schedule via cron/Task Scheduler and ship dumps off-host (S3/rsync);
add retention pruning and checksum manifests for production.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


_DUMP_BINARY_CANDIDATES = ("mariadb-dump", "mysqldump")


def _timestamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _resolve_dump_bin(explicit: str | None) -> str | None:
    explicit = explicit or getattr(settings, "MYSQL_DUMP_BIN", "") or None
    if explicit:
        return explicit if (Path(explicit).exists() or shutil.which(explicit)) else None
    for name in _DUMP_BINARY_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


class Command(BaseCommand):
    help = "Back up the MySQL/MariaDB database (native mysqldump or Python fallback)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            default=str(Path(settings.BASE_DIR) / "backups"),
            help="Output directory (default: django_project/backups).",
        )
        parser.add_argument(
            "--no-gzip",
            action="store_true",
            help="Write plain .sql instead of .sql.gz.",
        )
        parser.add_argument(
            "--dump-bin",
            default=None,
            help="Path/name of mysqldump/mariadb-dump. Overrides auto-detect.",
        )
        parser.add_argument(
            "--python",
            action="store_true",
            help="Force the pure-Python dumper even if a dump binary exists.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Rows per INSERT in the Python dumper (default 500).",
        )

    def handle(self, *args, **options):
        db = settings.DATABASES["default"]
        if "mysql" not in db.get("ENGINE", ""):
            raise CommandError(
                "dbbackup targets MySQL/MariaDB. Set MYSQL_DATABASE in .env first."
            )

        out_dir = Path(options["out"]).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        gzip_out = not options["no_gzip"]
        name = db["NAME"]
        suffix = ".sql.gz" if gzip_out else ".sql"
        out_path = out_dir / f"{name}-{_timestamp()}{suffix}"

        dump_bin = None if options["python"] else _resolve_dump_bin(options["dump_bin"])
        if dump_bin:
            self.stdout.write(f"Using native dump: {dump_bin}")
            self._dump_native(dump_bin, db, out_path, gzip_out)
        else:
            self.stdout.write(
                "No mysqldump/mariadb-dump found; using Python fallback dumper."
            )
            self._dump_python(out_path, gzip_out, batch_size=options["batch_size"])

        size_mb = out_path.stat().st_size / 1024 / 1024
        self.stdout.write(
            self.style.SUCCESS(f"Backup written: {out_path} ({size_mb:.2f} MB)")
        )

    # -- native mysqldump ---------------------------------------------------
    def _dump_native(self, dump_bin, db, out_path: Path, gzip_out: bool) -> None:
        args = [
            dump_bin,
            f"--host={db.get('HOST') or '127.0.0.1'}",
            f"--port={db.get('PORT') or 3306}",
            f"--user={db.get('USER') or ''}",
            "--single-transaction",
            "--routines",
            "--triggers",
            "--default-character-set=utf8mb4",
            db["NAME"],
        ]
        # Password via env to keep it off the process arg list.
        import os

        proc_env = dict(os.environ)
        if db.get("PASSWORD"):
            proc_env["MYSQL_PWD"] = db["PASSWORD"]

        opener = gzip.open if gzip_out else open
        with opener(out_path, "wb") as fh:
            proc = subprocess.run(
                args, stdout=fh, stderr=subprocess.PIPE, env=proc_env, check=False
            )
        if proc.returncode != 0:
            out_path.unlink(missing_ok=True)
            raise CommandError(
                f"{Path(dump_bin).name} failed: {proc.stderr.decode(errors='replace')}"
            )

    # -- pure-Python logical dump ------------------------------------------
    def _dump_python(self, out_path: Path, gzip_out: bool, *, batch_size: int) -> None:
        opener = gzip.open if gzip_out else open
        with opener(out_path, "wt", encoding="utf-8", newline="\n") as fh:
            self._write_python_dump(fh, batch_size=batch_size)

    def _write_python_dump(self, fh, *, batch_size: int) -> None:
        conn = connection  # Django wraps PyMySQL
        conn.ensure_connection()
        raw = conn.connection  # underlying PyMySQL connection (has .escape)

        with conn.cursor() as cur:
            cur.execute("SELECT DATABASE()")
            dbname = cur.fetchone()[0]
            cur.execute("SHOW FULL TABLES WHERE Table_type = 'BASE TABLE'")
            tables = [row[0] for row in cur.fetchall()]

        fh.write(f"-- ResumeElite logical backup of `{dbname}`\n")
        fh.write(f"-- generated {_dt.datetime.now().isoformat(timespec='seconds')}\n")
        fh.write("SET FOREIGN_KEY_CHECKS=0;\n")
        fh.write("SET UNIQUE_CHECKS=0;\n")
        fh.write("SET NAMES utf8mb4;\n\n")

        for table in tables:
            with conn.cursor() as cur:
                cur.execute(f"SHOW CREATE TABLE `{table}`")
                create_sql = cur.fetchone()[1]
            fh.write(f"DROP TABLE IF EXISTS `{table}`;\n")
            fh.write(create_sql.rstrip().rstrip(";") + ";\n")
            self._dump_table_rows(fh, raw, table, batch_size=batch_size)
            fh.write("\n")

        fh.write("SET UNIQUE_CHECKS=1;\n")
        fh.write("SET FOREIGN_KEY_CHECKS=1;\n")

    def _dump_table_rows(self, fh, raw, table: str, *, batch_size: int) -> None:
        # Stream rows with a server-side cursor to bound memory on large tables.
        import pymysql.cursors

        cur = raw.cursor(pymysql.cursors.SSCursor)
        try:
            cur.execute(f"SELECT * FROM `{table}`")
            columns = [d[0] for d in cur.description]
            col_list = ", ".join(f"`{c}`" for c in columns)
            batch: list[str] = []

            def flush():
                if not batch:
                    return
                fh.write(
                    f"INSERT INTO `{table}` ({col_list}) VALUES\n"
                    + ",\n".join(batch)
                    + ";\n"
                )
                batch.clear()

            for row in cur:
                values = ", ".join(raw.escape(v) for v in row)
                batch.append(f"({values})")
                if len(batch) >= batch_size:
                    flush()
            flush()
        finally:
            cur.close()
