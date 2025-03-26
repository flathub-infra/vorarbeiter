
from django.db import models
import uuid

class Provider(models.Model):
    """Represents a CI/CD provider like GitHub Actions or GitLab CI."""
    name = models.CharField(max_length=100, unique=True, help_text="e.g., GitHub Actions, GitLab CI")
    provider_type = models.CharField(max_length=100, help_text="Type of provider (arbitrary name)")
    api_base_url = models.URLField(
        max_length=255, 
        blank=True, 
        help_text="Base URL for API calls (leave empty for default)"
    )
    
    credentials = models.JSONField(
        default=dict,
        blank=True,
        help_text="Credentials data in JSON format (tokens, keys, etc.)"
    )
    
    settings = models.JSONField(
        default=dict,
        blank=True,
        help_text="Provider-specific settings and configuration"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Provider"
        verbose_name_plural = "Providers"

class PipelineTemplate(models.Model):
    """A reusable template defining a sequence of jobs and their dependencies."""
    name = models.CharField(max_length=200, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Pipeline Template"
        verbose_name_plural = "Pipeline Templates"
        ordering = ['name']

class JobTemplate(models.Model):
    """A reusable template for a single job within a PipelineTemplate."""
    pipeline_template = models.ForeignKey(
        PipelineTemplate,
        on_delete=models.CASCADE,
        related_name="job_templates"
    )
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="job_templates"
    )
    provider_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Provider-specific configuration (e.g., {'workflow': 'ci.yml', 'job_id': 'build'})"
    )
    depends_on = models.ManyToManyField(
        'self',
        blank=True,
        symmetrical=False,
        related_name="required_by",
        help_text="Which other jobs in this template must succeed before this one starts?"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.pipeline_template.name} / {self.name}"

    class Meta:
        verbose_name = "Job Template"
        verbose_name_plural = "Job Templates"
        unique_together = ('pipeline_template', 'name')
        ordering = ['pipeline_template', 'name']

class PipelineInstance(models.Model):
    """An instance of a PipelineTemplate being executed."""
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        RUNNING = 'RUNNING', 'Running'
        SUCCEEDED = 'SUCCEEDED', 'Succeeded'
        FAILED = 'FAILED', 'Failed'
        CANCELLED = 'CANCELLED', 'Cancelled'

    pipeline_template = models.ForeignKey(
        PipelineTemplate,
        on_delete=models.PROTECT,
        related_name="instances"
    )
    trigger_parameters = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    def __str__(self):
        return f"{self.pipeline_template.name} - Run #{self.id}"

    class Meta:
        verbose_name = "Pipeline Instance"
        verbose_name_plural = "Pipeline Instances"
        ordering = ['-created_at']

class JobInstance(models.Model):
    """An instance of a JobTemplate being executed within a PipelineInstance."""
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        QUEUED = 'QUEUED', 'Queued'
        RUNNING = 'RUNNING', 'Running'
        SUCCEEDED = 'SUCCEEDED', 'Succeeded'
        FAILED = 'FAILED', 'Failed'
        CANCELLED = 'CANCELLED', 'Cancelled'
        SKIPPED = 'SKIPPED', 'Skipped'

    pipeline_instance = models.ForeignKey(
        PipelineInstance,
        on_delete=models.CASCADE,
        related_name="job_instances"
    )
    job_template = models.ForeignKey(
        JobTemplate,
        on_delete=models.PROTECT,
        related_name="instances"
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True
    )
    callback_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    execution_parameters = models.JSONField(default=dict, blank=True)
    external_job_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    results = models.JSONField(default=dict, blank=True, null=True)
    logs_url = models.URLField(max_length=1024, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    queued_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.pipeline_instance} / {self.job_template.name} - Job #{self.id}"

    class Meta:
        verbose_name = "Job Instance"
        verbose_name_plural = "Job Instances"
        ordering = ['pipeline_instance', 'created_at']
        unique_together = ('pipeline_instance', 'job_template')
