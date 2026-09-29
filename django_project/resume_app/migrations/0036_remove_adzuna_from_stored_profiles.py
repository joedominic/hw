"""Data migration: remove 'adzuna' from stored SearchProfile and JobSearchTask site_names."""

from django.db import migrations


def remove_adzuna_from_profiles(apps, schema_editor):
    SearchProfile = apps.get_model("resume_app", "SearchProfile")
    JobSearchTask = apps.get_model("resume_app", "JobSearchTask")

    for profile in SearchProfile.objects.all():
        if isinstance(profile.site_names, list):
            cleaned = [s for s in profile.site_names if str(s).strip().lower() != "adzuna"]
            if cleaned != profile.site_names:
                profile.site_names = cleaned if cleaned else ["indeed"]
                profile.save(update_fields=["site_names"])

    for task in JobSearchTask.objects.all():
        if isinstance(task.site_name, list):
            cleaned = [s for s in task.site_name if str(s).strip().lower() != "adzuna"]
            if cleaned != task.site_name:
                task.site_name = cleaned if cleaned else ["indeed"]
                task.save(update_fields=["site_name"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0035_tenantpromptmodelpreference"),
    ]

    operations = [
        migrations.RunPython(remove_adzuna_from_profiles, noop_reverse),
    ]
