"""
Restore LLMProviderConfig (encrypted API keys) and LLMProviderPreference from a backup SQLite DB.

Usage:
  python manage.py restore_llm_config
  python manage.py restore_llm_config --backup ../backups/db-20260525-122804.sqlite3
  python manage.py restore_llm_config --dry-run
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from resume_app.crypto import decrypt_api_key
from resume_app.models import LLMProviderConfig, LLMProviderPreference


class Command(BaseCommand):
    help = "Restore LLM provider keys and preferences from a backup SQLite database."

    def add_arguments(self, parser):
        default_backup = Path(settings.BASE_DIR) / "backups" / "db-20260525-122804.sqlite3"
        parser.add_argument(
            "--backup",
            type=str,
            default=str(default_backup),
            help="Path to backup db.sqlite3",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change without writing",
        )
        parser.add_argument(
            "--preferences",
            action="store_true",
            default=True,
            help="Also replace preference rows from backup (default: yes)",
        )
        parser.add_argument(
            "--no-preferences",
            action="store_true",
            help="Only restore provider config keys, not preference rows",
        )

    def handle(self, *args, **options):
        backup_path = Path(options["backup"])
        if not backup_path.is_file():
            self.stderr.write(self.style.ERROR(f"Backup not found: {backup_path}"))
            return

        dry_run = options["dry_run"]
        restore_prefs = options["preferences"] and not options["no_preferences"]

        src = sqlite3.connect(backup_path)
        src.row_factory = sqlite3.Row
        cur = src.cursor()

        cur.execute("SELECT * FROM resume_app_llmproviderconfig ORDER BY id")
        backup_configs = [dict(r) for r in cur.fetchall()]

        backup_prefs = []
        if restore_prefs:
            cur.execute("SELECT * FROM resume_app_llmproviderpreference ORDER BY priority, id")
            backup_prefs = [dict(r) for r in cur.fetchall()]
        src.close()

        self.stdout.write(f"Backup: {backup_path} ({len(backup_configs)} configs, {len(backup_prefs)} prefs)")

        by_provider = {c["provider"]: c for c in backup_configs}
        updated_configs = 0
        decrypt_ok = 0

        with transaction.atomic():
            for cfg in LLMProviderConfig.objects.all():
                old = by_provider.get(cfg.provider)
                if not old:
                    self.stdout.write(f"  skip (not in backup): {cfg.provider}")
                    continue

                enc = (old.get("encrypted_api_key") or "").strip()
                if enc:
                    if decrypt_api_key(enc):
                        decrypt_ok += 1
                    else:
                        self.stdout.write(
                            self.style.WARNING(
                                f"  {cfg.provider}: ciphertext present but decrypt failed "
                                "(SECRET_KEY may differ from when keys were saved)"
                            )
                        )

                changes = []
                if (cfg.encrypted_api_key or "") != (old.get("encrypted_api_key") or ""):
                    changes.append("encrypted_api_key")
                if cfg.is_active != bool(old.get("is_active")):
                    changes.append(f"is_active {cfg.is_active} -> {old.get('is_active')}")
                if (cfg.default_model or "") != (old.get("default_model") or ""):
                    changes.append("default_model")
                if cfg.priority != old.get("priority"):
                    changes.append("priority")

                if not changes:
                    self.stdout.write(f"  unchanged: {cfg.provider}")
                    continue

                self.stdout.write(
                    f"  restore {cfg.provider}: {', '.join(changes)} "
                    f"(key_len={len(enc)})"
                )
                if not dry_run:
                    cfg.encrypted_api_key = old.get("encrypted_api_key") or ""
                    cfg.is_active = bool(old.get("is_active"))
                    cfg.default_model = old.get("default_model") or ""
                    cfg.priority = old.get("priority") or cfg.priority
                    cfg.last_validated_at = old.get("last_validated_at")
                    cfg.save(
                        update_fields=[
                            "encrypted_api_key",
                            "is_active",
                            "default_model",
                            "priority",
                            "last_validated_at",
                            "updated_at",
                        ]
                    )
                updated_configs += 1

            if restore_prefs and backup_prefs:
                if dry_run:
                    self.stdout.write(
                        f"  would replace {LLMProviderPreference.objects.count()} "
                        f"preference rows with {len(backup_prefs)} from backup"
                    )
                else:
                    deleted, _ = LLMProviderPreference.objects.all().delete()
                    self.stdout.write(f"  deleted {deleted} existing preference rows")
                    id_to_provider = {c["id"]: c["provider"] for c in backup_configs}
                    inserted = 0
                    for row in backup_prefs:
                        provider_name = id_to_provider.get(row["provider_config_id"])
                        if not provider_name:
                            continue
                        current_cfg = LLMProviderConfig.objects.filter(provider=provider_name).first()
                        if not current_cfg:
                            continue
                        LLMProviderPreference.objects.create(
                            provider_config=current_cfg,
                            model=row.get("model") or "",
                            is_local=bool(row.get("is_local")),
                            priority=row.get("priority") or 100,
                            rate_limit_rpm=row.get("rate_limit_rpm"),
                            rate_limit_tpm=row.get("rate_limit_tpm"),
                            rate_limit_cooldown_seconds=row.get("rate_limit_cooldown_seconds"),
                        )
                        inserted += 1
                    self.stdout.write(f"  inserted {inserted} preference rows from backup")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run — no changes written."))
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Done. Updated {updated_configs} provider config(s); "
                    f"{decrypt_ok} key(s) decrypt OK with current SECRET_KEY."
                )
            )
            if updated_configs and decrypt_ok < sum(1 for c in backup_configs if (c.get("encrypted_api_key") or "").strip()):
                self.stdout.write(
                    self.style.WARNING(
                        "Some keys did not decrypt. Restore the original SECRET_KEY from "
                        "when you saved the keys, or re-enter API keys in Settings."
                    )
                )
