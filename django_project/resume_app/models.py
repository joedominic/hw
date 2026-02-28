from django.db import models

class UserResume(models.Model):
    file = models.FileField(upload_to='resumes/')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Resume {self.id} uploaded at {self.uploaded_at}"

class JobDescription(models.Model):
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

class OptimizedResume(models.Model):
    original_resume = models.ForeignKey(UserResume, on_delete=models.CASCADE)
    job_description = models.ForeignKey(JobDescription, on_delete=models.CASCADE)
    optimized_content = models.TextField(blank=True, null=True)
    status = models.CharField(max_length=50, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class AgentLog(models.Model):
    optimized_resume = models.ForeignKey(OptimizedResume, related_name='logs', on_delete=models.CASCADE)
    step_name = models.CharField(max_length=100)
    thought = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
