import sys
import time
from typing import Dict
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from resume_app.models import PipelineEntry, SearchProfile, UserResume
from resume_app.pipeline_board import _resolve_user_resume_for_track
from resume_app.services import parse_pdf
from resume_app.skill_radar import SkillRadarService

User = get_user_model()


class Command(BaseCommand):
    help = "Re-runs Skill Radar (Interview Fit & Skill Diagnostics) on Ollama Local to overwrite old pipeline insights."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user",
            type=str,
            default="joedhunt",
            help="Username to process (default: joedhunt). Use 'all' for all users.",
        )
        parser.add_argument(
            "--stages",
            type=str,
            default="vetting,applying,pipeline,done",
            help="Comma-separated stages to process (default: vetting,applying,pipeline,done).",
        )
        parser.add_argument(
            "--track",
            "--tracks",
            dest="track",
            type=str,
            default="",
            help="Specific track slug or comma-separated track slugs to target (optional).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Max jobs to process (0 = all).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print targeted jobs without invoking Ollama.",
        )
        parser.add_argument(
            "--skip-recent",
            action="store_true",
            help="Skip pipeline entries that have already been scored within the last 24 hours.",
        )
        parser.add_argument(
            "--allow-cloud",
            action="store_true",
            help="Allow Cloud LLM fallback (strictly disabled by default; auto calculations must run on Local SLM).",
        )

    def handle(self, *args, **options):
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

        username = options["user"]
        stages_str = options["stages"]
        track_filter = options["track"]
        limit = options["limit"]
        dry_run = options["dry_run"]
        skip_recent = options["skip_recent"]

        STAGE_ALIASES = {
            "review": "vetting",
            "vet": "vetting",
            "apply": "applying",
            "new": "pipeline",
            "applied": "done",
        }
        stages = [
            STAGE_ALIASES.get(s.strip().lower(), s.strip().lower())
            for s in stages_str.split(",")
            if s.strip()
        ]

        tracks = [
            t.strip().lower()
            for t in track_filter.replace(";", ",").split(",")
            if t.strip()
        ]

        qs = PipelineEntry.objects.filter(removed_at__isnull=True).exclude(stage="deleted")
        if username.lower() != "all":
            user = User.objects.filter(username__iexact=username).first()
            if not user and username.lower() == "joedhut":
                user = User.objects.filter(username="joedhunt").first()
            if not user:
                self.stderr.write(self.style.ERROR(f"User '{username}' not found."))
                return
            qs = qs.filter(owner=user)
        else:
            user = None

        if stages:
            qs = qs.filter(stage__in=stages)

        if tracks:
            qs = qs.filter(track__in=tracks)

        qs = qs.select_related("owner", "job_listing").order_by("added_at")
        total_count = qs.count()

        if limit > 0:
            entries = list(qs[:limit])
        else:
            entries = list(qs)

        self.stdout.write(
            self.style.SUCCESS(
                f"Found {len(entries)} pipeline entries to refresh (out of {total_count} matching criteria)."
            )
        )
        self.stdout.write(f"Target stages: {', '.join(stages)}")
        if tracks:
            self.stdout.write(f"Target tracks: {', '.join(tracks)}")

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN mode: No LLM calls will be made."))
            for idx, entry in enumerate(entries, 1):
                job = entry.job_listing
                self.stdout.write(f"[{idx}/{len(entries)}] Job #{job.id}: {job.title} at {job.company_name} (Stage: {entry.stage}, Track: {entry.track})")
            return

        # Cache parsed PDF text in memory across runs
        resume_cache: Dict[int, str] = {}

        success_count = 0
        error_count = 0
        start_time = time.time()

        for idx, entry in enumerate(entries, 1):
            owner = entry.owner
            job = entry.job_listing

            # Resolve bound resume for this track
            bound_resume = _resolve_user_resume_for_track(owner, entry.track)
            if not bound_resume:
                active_prof = SearchProfile.objects.filter(owner=owner, is_default=True).first()
                if active_prof and active_prof.resume:
                    bound_resume = active_prof.resume
                else:
                    bound_resume = UserResume.objects.filter(owner=owner, is_library=True).order_by("-uploaded_at").first()

            if not bound_resume or not bound_resume.file:
                self.stdout.write(
                    self.style.WARNING(
                        f"[{idx}/{len(entries)}] Skipping Job #{job.id} ({job.title}): No resume attached."
                    )
                )
                continue

            r_id = bound_resume.id
            if r_id not in resume_cache:
                try:
                    resume_cache[r_id] = parse_pdf(bound_resume.file.path)
                except Exception as e:
                    self.stderr.write(f"Failed to parse resume PDF {bound_resume.original_filename}: {e}")
                    resume_cache[r_id] = ""

            resume_text = resume_cache[r_id]
            if not resume_text:
                self.stdout.write(self.style.WARNING(f"[{idx}/{len(entries)}] Resume empty for Job #{job.id}."))
                continue

            if skip_recent and entry.vetting_interview_scored_at:
                age_hours = (timezone.now() - entry.vetting_interview_scored_at).total_seconds() / 3600
                if age_hours < 24 and entry.vetting_core_competencies:
                    self.stdout.write(
                        self.style.WARNING(
                            f"[{idx}/{len(entries)}] Skipping Job #{job.id} (already refreshed {round(age_hours, 1)}h ago)."
                        )
                    )
                    continue

            self.stdout.write(
                f"\n[{idx}/{len(entries)}] Analyzing Job #{job.id}: '{job.title}' ({job.company_name}) [Track: {entry.track}, Stage: {entry.stage}]..."
            )
            t0 = time.time()

            try:
                # Force refresh to bypass old cache and re-compute on Ollama Local
                res = SkillRadarService.analyze(
                    job_listing=job,
                    resume_text=resume_text,
                    user=owner,
                    resume_id=r_id,
                    force_refresh=True,
                    is_manual_invocation=options.get("allow_cloud", False),
                )
                elapsed = time.time() - t0

                if res.get("source") != "error" and res.get("match_score") is not None:
                    # Update PipelineEntry in DB
                    entry.record_vetting_interview_result(
                        probability=res.get("interview_probability") or res.get("match_score"),
                        match_score=res.get("match_score"),
                        reasoning=res.get("fit_summary"),
                        resume_id=r_id,
                        core_competencies=res.get("core_competencies"),
                        stretch_skills=res.get("stretch_skills"),
                        save=True,
                    )

                    # Update Redis cache so Cockpit UI reflects new diagnostic instantly
                    cache_key = SkillRadarService.get_cache_key(owner.id, job.id, r_id)
                    cache.set(cache_key, res, SkillRadarService.CACHE_TTL_SECONDS if hasattr(SkillRadarService, "CACHE_TTL_SECONDS") else 7 * 86400)

                    success_count += 1
                    core_cnt = len(res.get("core_competencies") or [])
                    stretch_cnt = len(res.get("stretch_skills") or [])
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  -> SUCCESS in {round(elapsed, 1)}s | Match: {res.get('match_score')}% | Callback: {res.get('interview_probability')}% | Core: {core_cnt} | Gaps: {stretch_cnt}"
                        )
                    )
                    self.stdout.write(f"  -> Summary: {res.get('fit_summary')}")
                else:
                    error_count += 1
                    self.stderr.write(f"  -> ERROR: Model failed or returned invalid output: {res.get('error') or res.get('fit_summary')}")

            except Exception as e:
                error_count += 1
                self.stderr.write(self.style.ERROR(f"  -> EXCEPTION on Job #{job.id}: {e}"))

            # Print cumulative ETA
            elapsed_total = time.time() - start_time
            avg_per_job = elapsed_total / idx
            remaining = (len(entries) - idx) * avg_per_job
            self.stdout.write(
                f"  [Progress: {idx}/{len(entries)} | Elapsed: {round(elapsed_total/60, 1)}m | ETA: {round(remaining/60, 1)}m]"
            )

        total_time = round((time.time() - start_time) / 60, 2)
        self.stdout.write(
            self.style.SUCCESS(
                f"\nDONE! Refreshed {success_count} jobs ({error_count} failed) in {total_time} minutes."
            )
        )
