"""Promote per-user ATS judge profiles to system-wide (owner=null) rows."""

from django.db import migrations


def promote_global_ats_profiles(apps, schema_editor):
    AtsJudgeProfile = apps.get_model("resume_app", "AtsJudgeProfile")

    if AtsJudgeProfile.objects.filter(owner__isnull=True).exists():
        return

    # Prefer default profiles and earlier rows when collapsing duplicate slugs.
    seen_slugs: set[str] = set()
    created = []
    for src in AtsJudgeProfile.objects.filter(owner__isnull=False).order_by(
        "-is_default", "pk"
    ):
        slug = (src.slug or "").strip() or f"ats-{src.pk}"
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        created.append(
            AtsJudgeProfile.objects.create(
                owner=None,
                name=src.name,
                slug=slug,
                ats_judge=src.ats_judge or "",
                ats_judge_system=src.ats_judge_system or "",
                ats_judge_user=src.ats_judge_user or "",
                is_builtin=bool(src.is_builtin),
                is_default=False,
            )
        )

    if not created:
        AtsJudgeProfile.objects.create(
            owner=None,
            name="Default",
            slug="default",
            ats_judge="",
            ats_judge_system="",
            ats_judge_user="",
            is_builtin=True,
            is_default=True,
        )
        return

    # Prefer a slug named "default", else the first promoted row.
    default = next((p for p in created if p.slug == "default"), created[0])
    AtsJudgeProfile.objects.filter(owner__isnull=True).update(is_default=False)
    default.is_default = True
    default.save(update_fields=["is_default"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0026_system_prompt_profile"),
    ]

    operations = [
        migrations.RunPython(promote_global_ats_profiles, noop_reverse),
    ]
