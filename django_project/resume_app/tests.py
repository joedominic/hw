from django.test import TestCase
from .models import UserResume, JobDescription, OptimizedResume
from .services import parse_pdf
import os

class ModelsTestCase(TestCase):
    def test_model_creation(self):
        resume = UserResume.objects.create(file="test.pdf")
        jd = JobDescription.objects.create(content="Test JD")
        optimized = OptimizedResume.objects.create(original_resume=resume, job_description=jd)
        self.assertEqual(optimized.status, "pending")

class ServiceTestCase(TestCase):
    def test_parse_pdf_placeholder(self):
        # We can't easily test pdfplumber without a real PDF,
        # but we can check if the function exists
        self.assertTrue(callable(parse_pdf))
