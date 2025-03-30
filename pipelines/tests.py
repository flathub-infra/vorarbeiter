import json
import uuid
from unittest.mock import MagicMock, patch, AsyncMock
from django.test import TestCase, Client
from django.db import IntegrityError, transaction
from .models import (
    Provider,
    PipelineTemplate,
    JobTemplate,
    PipelineInstance,
    JobInstance,
)
from .providers import ProviderRegistry
from .providers.base import BaseProvider
from .providers.github_actions import GitHubActionsProvider


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


class ProviderRegistryTest(TestCase):
    def test_provider_registration(self):
        class TestProvider(BaseProvider):
            async def dispatch_job(self, job_instance):
                return "test-id"

            async def cancel_job(self, job_instance):
                return True

        ProviderRegistry.register("test", TestProvider)

        provider_class = ProviderRegistry.get_provider("test")
        self.assertEqual(provider_class, TestProvider)

    def test_unregistered_provider(self):
        with self.assertRaises(ValueError):
            ProviderRegistry.get_provider("nonexistent")

    def test_get_implementation(self):
        provider = Provider.objects.create(
            name="Test GitHub Provider", provider_type="github"
        )

        impl = provider.get_implementation()
        self.assertIsInstance(impl, GitHubActionsProvider)
        self.assertEqual(impl.provider_model, provider)


class GitHubActionsProviderTest(TestCase):
    def setUp(self):
        self.provider = Provider.objects.create(
            name="GitHub",
            provider_type="github",
            credentials={"token": "test-token"},
            settings={
                "owner": "test-owner",
                "repo": "test-repo",
                "callback_base_url": "https://example.com",
            },
        )

        self.pipeline_template = PipelineTemplate.objects.create(
            name="Test Pipeline", version=1
        )

        self.job_template = JobTemplate.objects.create(
            pipeline_template=self.pipeline_template,
            name="Test Job",
            provider=self.provider,
            provider_config={
                "workflow": "test-workflow.yml",
                "inputs": {
                    "param1": "value1",
                    "param2": "{execution_parameters.dynamic_value}",
                },
            },
        )

        self.pipeline_instance = PipelineInstance.objects.create(
            pipeline_template=self.pipeline_template
        )

        self.job_instance = JobInstance.objects.create(
            pipeline_instance=self.pipeline_instance,
            job_template=self.job_template,
            execution_parameters={"dynamic_value": "dynamic-test-value"},
        )

        self.provider_impl = self.provider.get_implementation()

    def test_process_inputs(self):
        inputs = {
            "static": "static-value",
            "dynamic": "{execution_parameters.key1}",
            "nested": "{execution_parameters.nested_key}",
        }

        execution_parameters = {"key1": "value1", "nested_key": "nested-value"}

        result = self.provider_impl._process_inputs(inputs, execution_parameters)

        self.assertEqual(result["static"], "static-value")
        self.assertEqual(result["dynamic"], "value1")
        self.assertEqual(result["nested"], "nested-value")

    def test_payload_construction(self):
        processed_inputs = self.provider_impl._process_inputs(
            self.job_template.provider_config.get("inputs", {}),
            self.job_instance.execution_parameters,
        )

        processed_inputs.pop("callback_url", None)

        self.assertEqual(processed_inputs["param1"], "value1")
        self.assertEqual(processed_inputs["param2"], "dynamic-test-value")


class JobCallbackTest(TestCase):
    def setUp(self):
        self.client = Client()
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

        self.job_instance = JobInstance.objects.create(
            pipeline_instance=self.pipeline_instance,
            job_template=self.job_template,
            status=JobInstance.Status.RUNNING,
        )

        self.callback_url = f"/api/jobs/{self.job_instance.callback_id}/callback"

    @patch("pipelines.services.PipelineRunner._check_pipeline_status")
    def test_success_callback(self, mock_check_pipeline):
        response_data = {
            "status": "success",
            "results": {"message": "Job completed successfully"},
        }

        response = self.client.post(
            self.callback_url,
            data=json.dumps(response_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")

        self.job_instance.refresh_from_db()
        self.assertEqual(self.job_instance.status, JobInstance.Status.SUCCEEDED)
        self.assertEqual(
            self.job_instance.results, {"message": "Job completed successfully"}
        )

        mock_check_pipeline.assert_called_once_with(self.pipeline_instance)

    def test_failure_callback(self):
        response_data = {
            "status": "failure",
            "results": {"message": "Job failed with an error"},
        }

        response = self.client.post(
            self.callback_url,
            data=json.dumps(response_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

        self.job_instance.refresh_from_db()
        self.assertEqual(self.job_instance.status, JobInstance.Status.FAILED)
        self.assertEqual(
            self.job_instance.results, {"message": "Job failed with an error"}
        )

    def test_invalid_status(self):
        response_data = {"status": "invalid-status"}

        response = self.client.post(
            self.callback_url,
            data=json.dumps(response_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_missing_status(self):
        response_data = {"results": {"message": "No status provided"}}

        response = self.client.post(
            self.callback_url,
            data=json.dumps(response_data),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)

    def test_invalid_json(self):
        response = self.client.post(
            self.callback_url, data="not valid json", content_type="application/json"
        )

        self.assertEqual(response.status_code, 400)

    def test_job_not_found_behavior(self):
        self.assertTrue(
            JobInstance.objects.filter(callback_id=uuid.uuid4()).count() == 0,
            "Random UUID should not match any existing job",
        )


class AdminActionsTest(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        self.admin_user = User.objects.create_superuser(
            "admin", "admin@example.com", "password"
        )

        self.provider = Provider.objects.create(
            name="Test Provider",
            provider_type="github",
            credentials={"token": "test_token"},
            settings={"owner": "test-owner", "repo": "test-repo"},
        )

        self.pipeline_template = PipelineTemplate.objects.create(
            name="Test Pipeline",
            version=1,
            description="Test pipeline for admin actions",
        )

        self.job_template = JobTemplate.objects.create(
            pipeline_template=self.pipeline_template,
            name="Test Job",
            provider=self.provider,
            provider_config={"workflow": "test.yml"},
        )

    @patch("pipelines.services.PipelineRunner")
    def test_run_pipeline_action(self, mock_runner):
        from pipelines.admin import PipelineTemplateAdmin
        from django.contrib.admin.sites import AdminSite
        from django.test.client import RequestFactory

        mock_runner.start_pipeline = AsyncMock()

        admin = PipelineTemplateAdmin(model=PipelineTemplate, admin_site=AdminSite())
        admin.message_user = MagicMock()

        request = RequestFactory().get("/")
        request.user = self.admin_user

        queryset = PipelineTemplate.objects.filter(id=self.pipeline_template.id)

        admin.run_pipeline(request, queryset)

        self.assertEqual(PipelineInstance.objects.count(), 1)
        pipeline = PipelineInstance.objects.first()
        self.assertEqual(pipeline.pipeline_template, self.pipeline_template)
        self.assertEqual(pipeline.trigger_parameters["triggered_by"], "admin")
        self.assertEqual(pipeline.trigger_parameters["user"], "admin")

        self.assertEqual(JobInstance.objects.count(), 1)
        job = JobInstance.objects.first()
        self.assertEqual(job.pipeline_instance, pipeline)
        self.assertEqual(job.job_template, self.job_template)

        mock_runner.start_pipeline.assert_called_once()
        admin.message_user.assert_called_once()
