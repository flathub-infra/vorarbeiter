import json
import hmac
import hashlib
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from .models import GitHubWebhookEvent


class GitHubWebhookEventModelTest(TestCase):
    def test_string_representation(self):
        webhook = GitHubWebhookEvent(
            event_type="pull_request",
            repository="test/repo",
            sender="testuser",
            payload={},
        )
        self.assertEqual(str(webhook), "pull_request from test/repo by testuser")


class GitHubWebhookViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("github_webhook")

    def _generate_signature(self, payload_string, secret="test_secret"):
        """Helper method to generate a valid signature for tests."""
        signature = hmac.new(
            secret.encode(), payload_string.encode(), hashlib.sha256
        ).hexdigest()
        return f"sha256={signature}"

    def test_requires_post(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    @override_settings(GITHUB_WEBHOOK_SECRET="test_secret")
    def test_unsupported_event_type(self):
        payload = json.dumps({})
        headers = {
            "HTTP_X_GITHUB_EVENT": "unsupported_event",
            "HTTP_X_HUB_SIGNATURE_256": self._generate_signature(payload),
        }
        response = self.client.post(
            self.url, data=payload, content_type="application/json", **headers
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(GITHUB_WEBHOOK_SECRET="test_secret")
    def test_invalid_json(self):
        # For invalid JSON, we need to generate a valid signature for exactly that invalid JSON string
        invalid_data = "invalid json"
        headers = {
            "HTTP_X_GITHUB_EVENT": "pull_request",
            "HTTP_X_HUB_SIGNATURE_256": self._generate_signature(invalid_data),
        }
        response = self.client.post(
            self.url, data=invalid_data, content_type="application/json", **headers
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(GITHUB_WEBHOOK_SECRET="test_secret")
    def test_missing_payload_fields(self):
        payload = json.dumps({})
        headers = {
            "HTTP_X_GITHUB_EVENT": "pull_request",
            "HTTP_X_HUB_SIGNATURE_256": self._generate_signature(payload),
        }
        response = self.client.post(
            self.url, data=payload, content_type="application/json", **headers
        )
        self.assertEqual(response.status_code, 400)

    @override_settings(GITHUB_WEBHOOK_SECRET="test_secret")
    def test_pull_request_webhook(self):
        payload = {
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "testuser"},
            "action": "opened",
            "pull_request": {"number": 1},
        }
        payload_string = json.dumps(payload)
        headers = {
            "HTTP_X_GITHUB_EVENT": "pull_request",
            "HTTP_X_HUB_SIGNATURE_256": self._generate_signature(payload_string),
        }

        response = self.client.post(
            self.url,
            data=payload_string,
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(GitHubWebhookEvent.objects.count(), 1)

        webhook = GitHubWebhookEvent.objects.first()
        self.assertEqual(webhook.event_type, "pull_request")
        self.assertEqual(webhook.repository, "test/repo")
        self.assertEqual(webhook.sender, "testuser")

    @override_settings(GITHUB_WEBHOOK_SECRET="test_secret")
    def test_push_webhook(self):
        payload = {
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "testuser"},
            "ref": "refs/heads/main",
        }
        payload_string = json.dumps(payload)
        headers = {
            "HTTP_X_GITHUB_EVENT": "push",
            "HTTP_X_HUB_SIGNATURE_256": self._generate_signature(payload_string),
        }

        response = self.client.post(
            self.url,
            data=payload_string,
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(GitHubWebhookEvent.objects.count(), 1)

        webhook = GitHubWebhookEvent.objects.first()
        self.assertEqual(webhook.event_type, "push")

    @override_settings(GITHUB_WEBHOOK_SECRET="test_secret")
    def test_issue_comment_webhook(self):
        payload = {
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "testuser"},
            "action": "created",
            "issue": {"number": 1},
            "comment": {"body": "Test comment"},
        }
        payload_string = json.dumps(payload)
        headers = {
            "HTTP_X_GITHUB_EVENT": "issue_comment",
            "HTTP_X_HUB_SIGNATURE_256": self._generate_signature(payload_string),
        }

        response = self.client.post(
            self.url,
            data=payload_string,
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(GitHubWebhookEvent.objects.count(), 1)

        webhook = GitHubWebhookEvent.objects.first()
        self.assertEqual(webhook.event_type, "issue_comment")


class GitHubWebhookSignatureTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse("github_webhook")
        self.payload = {
            "repository": {"full_name": "test/repo"},
            "sender": {"login": "testuser"},
            "action": "opened",
            "pull_request": {"number": 1},
        }
        self.payload_string = json.dumps(self.payload)
        self.headers = {"HTTP_X_GITHUB_EVENT": "pull_request"}

    @override_settings(GITHUB_WEBHOOK_SECRET="")
    def test_missing_secret_setting(self):
        """Test webhook is rejected when no secret is configured."""
        with self.settings(GITHUB_WEBHOOK_SECRET=""):
            response = self.client.post(
                self.url,
                data=self.payload_string,
                content_type="application/json",
                **self.headers,
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(GitHubWebhookEvent.objects.count(), 0)

    @override_settings(GITHUB_WEBHOOK_SECRET="test_secret")
    def test_missing_signature_header(self):
        """Test webhook is rejected when signature header is missing."""
        response = self.client.post(
            self.url,
            data=self.payload_string,
            content_type="application/json",
            **self.headers,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(GitHubWebhookEvent.objects.count(), 0)

    @override_settings(GITHUB_WEBHOOK_SECRET="test_secret")
    def test_invalid_signature(self):
        """Test webhook is rejected when signature is invalid."""
        headers = {
            **self.headers,
            "HTTP_X_HUB_SIGNATURE_256": "sha256=invalid_signature",
        }
        response = self.client.post(
            self.url,
            data=self.payload_string,
            content_type="application/json",
            **headers,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(GitHubWebhookEvent.objects.count(), 0)

    @override_settings(GITHUB_WEBHOOK_SECRET="test_secret")
    def test_valid_signature(self):
        """Test webhook is accepted with valid signature."""
        # Generate a valid signature
        signature = hmac.new(
            b"test_secret", self.payload_string.encode(), hashlib.sha256
        ).hexdigest()

        headers = {**self.headers, "HTTP_X_HUB_SIGNATURE_256": f"sha256={signature}"}

        response = self.client.post(
            self.url,
            data=self.payload_string,
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(GitHubWebhookEvent.objects.count(), 1)
