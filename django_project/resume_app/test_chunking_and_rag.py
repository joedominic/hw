"""
Tests for Dual-Sectored Job Description Representation & ResumeChunk Hierarchical RAG.
"""
from unittest.mock import MagicMock, patch
from django.contrib.auth import get_user_model
from django.test import TestCase

from resume_app import embeddings
from resume_app.jd_cleanser import JDCleanserService
from resume_app.models import ResumeChunk, UserResume
from resume_app.optimizer_budget import build_optimizer_context_state_raw
from resume_app.resume_chunker import (
    ResumeChunker,
    format_retrieval_context,
    retrieve_relevant_chunks,
    sync_resume_chunks,
)

User = get_user_model()


class DualSectorJobDescriptionTests(TestCase):
    def test_extract_scope_and_requirements_with_headers(self):
        jd = """
        About the Role:
        We are seeking a Lead Backend Architect to scale our financial platform.
        You will design high-throughput microservices and mentor junior engineers.

        Requirements:
        - 8+ years of distributed systems engineering
        - Deep proficiency in Go, Python, and PostgreSQL
        - Hands-on experience with Kafka and Kubernetes
        - BS/MS in Computer Science
        """
        scope, reqs = JDCleanserService.extract_scope_and_requirements(jd, title="Lead Backend Architect")
        self.assertIn("Lead Backend Architect", scope)
        self.assertIn("microservices", scope.lower())
        self.assertIn("Go, Python", reqs)
        self.assertIn("Kafka", reqs)

    def test_extract_scope_and_requirements_fallback_without_headers(self):
        jd = """
        Line 1: Software Engineer at fast-growing fintech startup.
        Line 2: Build scalable APIs and data processing pipelines.
        Line 3: Collaborate with cross-functional product teams.
        Line 4: Must have 4+ years of Python and Django experience.
        Line 5: Strong knowledge of PostgreSQL query optimization.
        Line 6: Experience deploying containerized apps with Docker.
        """
        scope, reqs = JDCleanserService.extract_scope_and_requirements(jd, title="Software Engineer")
        self.assertTrue(len(scope) > 0)
        self.assertTrue(len(reqs) > 0)

    def test_embed_full_dual_sectors_produces_384_dim_unit_vector(self):
        jd = """
        About the Role:
        Senior Data Platform Engineer building real-time telemetry pipelines.

        Qualifications:
        - Proficient in Python, Spark, and AWS (EMR, S3)
        - 5+ years building distributed ETL data lakes
        """
        vec = embeddings.embed_full("Senior Data Engineer", jd)
        self.assertIsNotNone(vec)
        self.assertEqual(len(vec), 384)

        # Verify unit L2 norm
        import numpy as np
        norm = np.linalg.norm(np.array(vec, dtype=np.float32))
        self.assertAlmostEqual(float(norm), 1.0, places=3)

    def test_embed_full_batch_dual_sectors(self):
        items = [
            ("Frontend Engineer", "Responsibilities: Build React UI. Requirements: TypeScript, Next.js"),
            ("DevOps Lead", "Role: Maintain AWS infra. Requirements: Terraform, Kubernetes, Helm"),
        ]
        vecs = embeddings.embed_full_batch(items)
        self.assertEqual(len(vecs), 2)
        self.assertEqual(len(vecs[0]), 384)
        self.assertEqual(len(vecs[1]), 384)


class ResumeChunkerAndRAGTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="rag_test_user", password="secret_password_123")
        self.resume = UserResume.objects.create(
            owner=self.user,
            original_filename="sample_resume.pdf",
            is_library=True,
        )

    def test_resume_chunker_extracts_sections_with_context_prepending(self):
        resume_text = """
        Jane Doe
        jane@example.com | San Francisco, CA

        SUMMARY
        Staff Distributed Systems Engineer with 10 years of experience building resilient cloud infrastructure.

        EXPERIENCE
        Staff Software Engineer | Stripe (2021 - Present)
        - Architected Kafka event streaming pipeline handling 50k events/sec.
        - Mentored team of 6 distributed systems engineers across 3 timezones.
        - Cut infrastructure costs by $180,000 annually by migrating to Graviton instances.

        Senior Backend Engineer | Datadog (2018 - 2021)
        - Designed and deployed Go microservices for real-time log ingestion.
        - Maintained PostgreSQL cluster with 99.99% query uptime SLA.

        TECHNICAL SKILLS
        Languages: Python, Go, SQL, Rust, Java
        Cloud & DevOps: AWS (EKS, RDS, S3), Kubernetes, Terraform, Docker, Kafka

        EDUCATION
        B.S. in Computer Science | UC Berkeley (2014 - 2018)
        """
        chunks = ResumeChunker.chunk(resume_text)
        self.assertTrue(len(chunks) >= 4)

        # Verify context prepending on experience
        exp_chunks = [c for c in chunks if c["section"] == "EXPERIENCE"]
        self.assertTrue(len(exp_chunks) >= 2)
        self.assertTrue(any("Stripe" in c["text"] for c in exp_chunks))
        self.assertTrue(any("Datadog" in c["text"] for c in exp_chunks))

        # Verify skills chunk has [Skills] context
        skills_chunks = [c for c in chunks if c["section"] == "SKILLS"]
        self.assertTrue(len(skills_chunks) >= 1)
        self.assertTrue("[Skills]" in skills_chunks[0]["text"])

    def test_sync_resume_chunks_creates_db_rows(self):
        resume_text = """
        EXPERIENCE
        Senior Engineer | Acme Corp (2020 - 2023)
        - Built Python APIs using Django and FastAPI.
        - Deployed microservices to Kubernetes on AWS.

        SKILLS
        Python, Go, Docker, AWS, Kubernetes, Postgres
        """
        chunks = sync_resume_chunks(self.resume, text=resume_text)
        self.assertTrue(len(chunks) >= 2)

        # Check DB rows
        db_rows = ResumeChunk.objects.filter(user_resume=self.resume)
        self.assertEqual(db_rows.count(), len(chunks))

        # Verify idempotency
        chunks_repeat = sync_resume_chunks(self.resume, text=resume_text)
        self.assertEqual(len(chunks_repeat), len(chunks))
        self.assertEqual(ResumeChunk.objects.filter(user_resume=self.resume).count(), len(chunks))

    def test_retrieve_relevant_chunks_and_formatting(self):
        resume_text = """
        EXPERIENCE
        Lead Cloud Architect | CloudCorp (2020 - 2024)
        - Migrated multi-region Kubernetes clusters to AWS EKS with zero downtime.
        - Implemented Terraform Infrastructure-as-Code modules for 40+ microservices.

        Mobile Developer | AppWorks (2017 - 2020)
        - Developed iOS applications using Swift and Objective-C.
        - Built local SQLite caching for offline navigation.

        SKILLS
        AWS, Kubernetes, Terraform, Swift, Python, Docker
        """
        sync_resume_chunks(self.resume, text=resume_text)

        # Query targeting Cloud / Kubernetes
        query = "Kubernetes AWS cloud infrastructure architect with Terraform"
        retrieved = retrieve_relevant_chunks(self.resume, query, top_k=3)
        self.assertTrue(len(retrieved) > 0)

        # Highest scored chunk should be the Cloud / Kubernetes experience, not iOS
        top_chunk_text = retrieved[0]["text"]
        self.assertTrue("CloudCorp" in top_chunk_text or "Kubernetes" in top_chunk_text or "Skills" in top_chunk_text)

        formatted = format_retrieval_context(retrieved)
        self.assertIn("- [", formatted)
        self.assertNotEqual(formatted, "(none)")

    def test_optimizer_budget_integration_with_rag(self):
        resume_text = """
        EXPERIENCE
        Senior Python Engineer | TechCo (2021 - Present)
        - Built scalable Django applications and Celery background task processing.
        - Integrated Stripe payments and Webhooks.

        SKILLS
        Python, Django, PostgreSQL, Celery, Redis
        """
        sync_resume_chunks(self.resume, text=resume_text)

        target_jd = "Looking for Senior Django Developer with Celery and Postgres experience"
        ctx = build_optimizer_context_state_raw(
            resume_text=resume_text,
            job_description=target_jd,
            user_resume_id=self.resume.id,
            job_title="Senior Django Developer",
        )

        self.assertNotEqual(ctx["retrieval_context"], "(none)")
        self.assertIn("TechCo", ctx["retrieval_context"])
        self.assertTrue(ctx["optimizer_context_budget"]["retrieval_chunks_count"] > 0)
        # Verify prompt payload was reduced from 14,000 to RAG budget (8,000)
        self.assertTrue(len(ctx["resume_text"]) <= 8000)
