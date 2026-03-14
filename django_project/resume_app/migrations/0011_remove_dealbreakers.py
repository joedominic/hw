from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("resume_app", "0010_userdisqualifier"),
    ]

    operations = [
        migrations.RemoveField(model_name="joblisting", name="work_model"),
        migrations.RemoveField(model_name="joblisting", name="high_travel"),
        migrations.RemoveField(model_name="joblisting", name="no_visa_sponsorship"),
        migrations.RemoveField(model_name="joblisting", name="no_c2c"),
        migrations.RemoveField(model_name="joblisting", name="requires_us_citizen"),
        migrations.RemoveField(model_name="joblisting", name="requires_clearance"),
        migrations.DeleteModel(name="UserDealbreakerPreference"),
    ]
