"""Normalize duplicate auth emails, then enforce a DB unique index."""

from django.db import migrations


INDEX_NAME = "auth_user_email_uniq"


def dedupe_emails(apps, schema_editor):
    """Clear duplicate emails before creating the unique index."""
    # Import live helper so scoring stays in sync with runtime behavior.
    from resume_app.account import dedupe_user_emails

    dedupe_user_emails(dry_run=False)


def create_email_unique_index(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        sql = (
            f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} "
            "ON auth_user (email) WHERE email != ''"
        )
    elif vendor == "postgresql":
        sql = (
            f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} "
            "ON auth_user (email) WHERE email <> ''"
        )
    elif vendor == "mysql":
        # MariaDB/MySQL: no partial indexes; empty emails must already be cleared.
        sql = (
            f"CREATE UNIQUE INDEX {INDEX_NAME} ON auth_user (email)"
        )
    else:
        # Fallback: full unique index (empty-string duplicates must already be cleared).
        sql = f"CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME} ON auth_user (email)"
    schema_editor.execute(sql)


def drop_email_unique_index(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "postgresql":
        schema_editor.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    elif vendor == "mysql":
        schema_editor.execute(f"DROP INDEX `{INDEX_NAME}` ON `auth_user`")
    else:
        # SQLite / others
        schema_editor.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")


class Migration(migrations.Migration):
    # MySQL/MariaDB cannot run CREATE INDEX DDL inside an atomic transaction.
    atomic = False

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("resume_app", "0024_rename_resume_app__owner_i_7c1a0d_idx_resume_app__owner_i_389258_idx"),
    ]

    operations = [
        migrations.RunPython(dedupe_emails, migrations.RunPython.noop),
        migrations.RunPython(create_email_unique_index, drop_email_unique_index),
    ]
