import time
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.core.cache import cache
from resume_app.models import (
    LLMProviderConfig,
    LLMProviderPreference,
    PipelineEntry,
    UserResume,
    JobListing,
)
from resume_app.llm.gateway import local_llm_available
from resume_app.skill_radar import SkillRadarService
from resume_app.services import parse_pdf


class Command(BaseCommand):
    help = "Benchmark Phase B: Local Ollama KV Cache warmth across 3 Review jobs"

    def handle(self, *args, **options):
        User = get_user_model()
        user = User.objects.filter(username="joedhunt").first()
        if not user:
            self.stderr.write("User joedhunt not found")
            return

        self.stdout.write(f"Testing for user: {user.username}")

        # Check local config
        ollama_cfg = LLMProviderConfig.objects.filter(owner=user, provider__icontains="local").first()
        if ollama_cfg:
            self.stdout.write(f"Ollama Local config found: {ollama_cfg.provider} | Model: {ollama_cfg.default_model} | is_active: {ollama_cfg.is_active}")
            if not ollama_cfg.is_active:
                self.stdout.write("Activating Ollama Local config for test...")
                ollama_cfg.is_active = True
                ollama_cfg.save(update_fields=["is_active"])

        # Check preference
        pref = LLMProviderPreference.objects.filter(provider_config__owner=user, is_local=True).first()
        self.stdout.write(f"Local preference exists: {bool(pref)}")
        if not pref and ollama_cfg:
            self.stdout.write("Creating local preference row...")
            LLMProviderPreference.objects.create(
                provider_config=ollama_cfg,
                model=ollama_cfg.default_model,
                is_local=True,
                priority=1,
            )

        self.stdout.write(f"local_llm_available(user): {local_llm_available(user)}")

        # Find user resume
        resume = UserResume.objects.filter(owner=user, is_library=True).order_by("-uploaded_at").first()
        if not resume or not resume.file:
            self.stderr.write("No valid library resume found for user")
            return

        self.stdout.write(f"Using resume: {resume.original_filename} (ID: {resume.id})")
        try:
            resume_text = parse_pdf(resume.file.path)
            self.stdout.write(f"Parsed resume text length: {len(resume_text)} chars")
        except Exception as e:
            self.stderr.write(f"Failed to parse resume: {e}")
            return

        # Pick 3 jobs
        # Find 3 distinct job listings
        jobs = list(JobListing.objects.filter(description__isnull=False).exclude(description="").order_by("-fetched_at")[:3])
        if len(jobs) < 3:
            self.stderr.write(f"Found only {len(jobs)} jobs")
            return

        self.stdout.write("\n=== STARTING BENCHMARK (3 JOBS SEQUENTIAL) ===")
        timings = []

        for i, job in enumerate(jobs, 1):
            self.stdout.write(f"\n--- Job {i}/{len(jobs)}: {job.title[:40]} @ {job.company_name[:30]} (ID: {job.id}) ---")
            
            # Clear cache for this benchmark run
            cache_key = SkillRadarService.get_cache_key(user.id, job.id, resume.id)
            cache.delete(cache_key)

            t0 = time.perf_counter()
            res = SkillRadarService.analyze(
                job,
                resume_text,
                user=user,
                resume_id=resume.id,
                force_refresh=True,
            )
            elapsed = time.perf_counter() - t0
            timings.append((job.title, elapsed, res))

            self.stdout.write(f"Elapsed: {elapsed:.2f}s")
            self.stdout.write(f"Source: {res.get('source')}")
            self.stdout.write(f"Match Score: {res.get('match_score')}%")
            self.stdout.write(f"Core Competencies: {len(res.get('core_competencies', []))} items: {res.get('core_competencies', [])[:4]}")
            self.stdout.write(f"Stretch Skills: {len(res.get('stretch_skills', []))} items: {res.get('stretch_skills', [])[:3]}")
            if res.get("error"):
                self.stderr.write(f"Error details: {res.get('error')}")

        self.stdout.write("\n=== BENCHMARK SUMMARY ===")
        for i, (title, elapsed, res) in enumerate(timings, 1):
            self.stdout.write(f"Job {i} ({'Cold Cache' if i == 1 else 'Warm KV Cache'}): {elapsed:.2f}s | Match: {res.get('match_score')}%")
        
        if len(timings) >= 2 and timings[0][1] > 0:
            speedup = ((timings[0][1] - timings[1][1]) / timings[0][1]) * 100
            self.stdout.write(f"\nLatency difference Job 1 -> Job 2: {speedup:.1f}%")
