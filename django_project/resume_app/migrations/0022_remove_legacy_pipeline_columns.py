# Remove legacy per-track pipeline columns from JobListing (superseded by JobListingTrackMetrics).

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('resume_app', '0021_joblistingtrackmetrics'),
    ]

    operations = [
        migrations.RemoveField(model_name='joblisting', name='pipeline_focus_percent_ic'),
        migrations.RemoveField(model_name='joblisting', name='pipeline_focus_after_penalty_ic'),
        migrations.RemoveField(model_name='joblisting', name='pipeline_preference_margin_ic'),
        migrations.RemoveField(model_name='joblisting', name='pipeline_focus_percent_mgmt'),
        migrations.RemoveField(model_name='joblisting', name='pipeline_focus_after_penalty_mgmt'),
        migrations.RemoveField(model_name='joblisting', name='pipeline_preference_margin_mgmt'),
    ]
