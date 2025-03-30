from django.test import TestCase
from django.db import IntegrityError, transaction
from pipelines.models import (
    Provider,
    PipelineTemplate,
    JobTemplate,
    PipelineInstance,
    JobInstance,
)


class ProviderModelTest(TestCase):
    def test_provider_creation(self):
        provider = Provider.objects.create(
            name="Test Provider",
            provider_type="github",
            api_base_url="https://api.github.com",
            credentials={"token": "test_token"},
            settings={"owner": "test", "repo": "test-repo"},
        )
        self.assertEqual(provider.name, "Test Provider")
        self.assertEqual(provider.provider_type, "github")
        self.assertEqual(provider.credentials["token"], "test_token")
        self.assertEqual(provider.settings["owner"], "test")

    def test_provider_str(self):
        provider = Provider.objects.create(name="Test Provider", provider_type="github")
        self.assertEqual(str(provider), "Test Provider")


class PipelineTemplateModelTest(TestCase):
    def test_pipeline_template_creation(self):
        template = PipelineTemplate.objects.create(
            name="Test Pipeline", version=1, description="Test pipeline description"
        )
        self.assertEqual(template.name, "Test Pipeline")
        self.assertEqual(template.version, 1)
        self.assertEqual(template.description, "Test pipeline description")

    def test_pipeline_template_str(self):
        template = PipelineTemplate.objects.create(name="Test Pipeline", version=1)
        self.assertEqual(str(template), "Test Pipeline (v1)")

    def test_pipeline_template_versioning(self):
        # First version
        PipelineTemplate.objects.create(name="Versioned Pipeline", version=1)

        # Second version
        PipelineTemplate.objects.create(
            name="Versioned Pipeline", version=2, description="Updated version"
        )

        # Should have two versions
        templates = PipelineTemplate.objects.filter(name="Versioned Pipeline").order_by(
            "version"
        )
        self.assertEqual(templates.count(), 2)
        self.assertEqual(templates[0].version, 1)
        self.assertEqual(templates[1].version, 2)

    def test_unique_together_constraint(self):
        PipelineTemplate.objects.create(name="Unique Test", version=1)

        # Same name and version should raise IntegrityError
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PipelineTemplate.objects.create(name="Unique Test", version=1)

        # Same name but different version should work
        PipelineTemplate.objects.create(name="Unique Test", version=2)


class JobTemplateModelTest(TestCase):
    def setUp(self):
        self.provider = Provider.objects.create(
            name="Test Provider", provider_type="github"
        )
        self.pipeline_template = PipelineTemplate.objects.create(
            name="Test Pipeline", version=1
        )

    def test_job_template_creation(self):
        job = JobTemplate.objects.create(
            pipeline_template=self.pipeline_template,
            name="Test Job",
            provider=self.provider,
            provider_config={"workflow": "test.yml"},
        )
        self.assertEqual(job.name, "Test Job")
        self.assertEqual(job.pipeline_template, self.pipeline_template)
        self.assertEqual(job.provider, self.provider)
        self.assertEqual(job.provider_config["workflow"], "test.yml")

    def test_job_template_dependencies(self):
        job1 = JobTemplate.objects.create(
            pipeline_template=self.pipeline_template,
            name="First Job",
            provider=self.provider,
        )

        job2 = JobTemplate.objects.create(
            pipeline_template=self.pipeline_template,
            name="Second Job",
            provider=self.provider,
        )

        job3 = JobTemplate.objects.create(
            pipeline_template=self.pipeline_template,
            name="Third Job",
            provider=self.provider,
        )

        # Set up dependencies: job3 depends on job2, which depends on job1
        job2.depends_on.add(job1)
        job3.depends_on.add(job2)

        # Check dependencies
        self.assertEqual(list(job3.depends_on.all()), [job2])
        self.assertEqual(list(job2.depends_on.all()), [job1])

        # Check reverse relationship
        self.assertEqual(list(job1.required_by.all()), [job2])
        self.assertEqual(list(job2.required_by.all()), [job3])


class PipelineInstanceModelTest(TestCase):
    def setUp(self):
        self.provider = Provider.objects.create(
            name="Test Provider", provider_type="github"
        )
        self.pipeline_template = PipelineTemplate.objects.create(
            name="Test Pipeline", version=1
        )

    def test_pipeline_instance_creation(self):
        instance = PipelineInstance.objects.create(
            pipeline_template=self.pipeline_template,
            trigger_parameters={"app_id": "org.test.App", "commit_sha": "abc123"},
        )

        self.assertEqual(instance.pipeline_template, self.pipeline_template)
        self.assertEqual(instance.status, PipelineInstance.Status.PENDING)
        self.assertEqual(instance.trigger_parameters["app_id"], "org.test.App")
        self.assertIsNone(instance.started_at)
        self.assertIsNone(instance.finished_at)

    def test_pipeline_status_transitions(self):
        instance = PipelineInstance.objects.create(
            pipeline_template=self.pipeline_template
        )

        # Initial status is PENDING
        self.assertEqual(instance.status, PipelineInstance.Status.PENDING)

        # Update to RUNNING
        instance.status = PipelineInstance.Status.RUNNING
        instance.save()
        instance.refresh_from_db()
        self.assertEqual(instance.status, PipelineInstance.Status.RUNNING)

        # Update to SUCCEEDED
        instance.status = PipelineInstance.Status.SUCCEEDED
        instance.save()
        instance.refresh_from_db()
        self.assertEqual(instance.status, PipelineInstance.Status.SUCCEEDED)


class JobInstanceModelTest(TestCase):
    def setUp(self):
        self.provider = Provider.objects.create(
            name="Test Provider", provider_type="github"
        )
        self.pipeline_template = PipelineTemplate.objects.create(
            name="Test Pipeline", version=1
        )
        self.job_template = JobTemplate.objects.create(
            pipeline_template=self.pipeline_template,
            name="Test Job",
            provider=self.provider,
        )
        self.pipeline_instance = PipelineInstance.objects.create(
            pipeline_template=self.pipeline_template
        )

    def test_job_instance_creation(self):
        job_instance = JobInstance.objects.create(
            pipeline_instance=self.pipeline_instance,
            job_template=self.job_template,
            execution_parameters={"param1": "value1"},
        )

        self.assertEqual(job_instance.pipeline_instance, self.pipeline_instance)
        self.assertEqual(job_instance.job_template, self.job_template)
        self.assertEqual(job_instance.status, JobInstance.Status.PENDING)
        self.assertEqual(job_instance.execution_parameters["param1"], "value1")
        self.assertIsNotNone(job_instance.callback_id)

    def test_unique_constraint(self):
        JobInstance.objects.create(
            pipeline_instance=self.pipeline_instance, job_template=self.job_template
        )

        # Same pipeline_instance and job_template should raise IntegrityError
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                JobInstance.objects.create(
                    pipeline_instance=self.pipeline_instance,
                    job_template=self.job_template,
                )

    def test_job_status_transitions(self):
        job_instance = JobInstance.objects.create(
            pipeline_instance=self.pipeline_instance, job_template=self.job_template
        )

        # Test status transitions
        statuses = [
            JobInstance.Status.PENDING,
            JobInstance.Status.QUEUED,
            JobInstance.Status.RUNNING,
            JobInstance.Status.SUCCEEDED,
        ]

        for status in statuses:
            job_instance.status = status
            job_instance.save()
            job_instance.refresh_from_db()
            self.assertEqual(job_instance.status, status)
