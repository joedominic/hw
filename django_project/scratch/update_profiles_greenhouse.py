import os, sys
sys.path.insert(0, os.path.abspath('django_project'))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
import django; django.setup()

from resume_app.models import SearchProfile, JobSearchTask

print("--- Updating SearchProfiles ---")
sp_updated = 0
for sp in SearchProfile.objects.all():
    current_sites = list(sp.site_names or [])
    if "greenhouse" not in current_sites:
        current_sites.append("greenhouse")
        sp.site_names = current_sites
        sp.save()
        sp_updated += 1
        print(f"Updated SearchProfile ID={sp.id} ({sp.name}) -> {sp.site_names}")
    else:
        print(f"SearchProfile ID={sp.id} ({sp.name}) already has greenhouse -> {sp.site_names}")

print(f"\nTotal SearchProfiles updated: {sp_updated}")

print("\n--- Updating JobSearchTasks ---")
jst_updated = 0
for jst in JobSearchTask.objects.all():
    current_sites = list(jst.site_name or [])
    if "greenhouse" not in current_sites:
        current_sites.append("greenhouse")
        jst.site_name = current_sites
        jst.save()
        jst_updated += 1
        print(f"Updated JobSearchTask ID={jst.id} ({jst.name}) -> {jst.site_name}")
    else:
        print(f"JobSearchTask ID={jst.id} ({jst.name}) already has greenhouse -> {jst.site_name}")

print(f"\nTotal JobSearchTasks updated: {jst_updated}")
print("\nAll search profiles and search tasks now include 'greenhouse'!")
