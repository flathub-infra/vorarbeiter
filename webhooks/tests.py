import json
from django.test import TestCase, Client
from django.urls import reverse
from .models import GitHubWebhookEvent


class GitHubWebhookEventModelTest(TestCase):
    def test_string_representation(self):
        webhook = GitHubWebhookEvent(
            event_type='pull_request',
            repository='test/repo',
            sender='testuser',
            payload={}
        )
        self.assertEqual(str(webhook), 'pull_request from test/repo by testuser')


class GitHubWebhookViewTest(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = reverse('github_webhook')
        
    def test_requires_post(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        
    def test_unsupported_event_type(self):
        headers = {'HTTP_X_GITHUB_EVENT': 'unsupported_event'}
        response = self.client.post(
            self.url, 
            data=json.dumps({}), 
            content_type='application/json',
            **headers
        )
        self.assertEqual(response.status_code, 400)
        
    def test_invalid_json(self):
        headers = {'HTTP_X_GITHUB_EVENT': 'pull_request'}
        response = self.client.post(
            self.url, 
            data='invalid json', 
            content_type='application/json',
            **headers
        )
        self.assertEqual(response.status_code, 400)
        
    def test_missing_payload_fields(self):
        headers = {'HTTP_X_GITHUB_EVENT': 'pull_request'}
        response = self.client.post(
            self.url, 
            data=json.dumps({}), 
            content_type='application/json',
            **headers
        )
        self.assertEqual(response.status_code, 400)
        
    def test_pull_request_webhook(self):
        payload = {
            'repository': {'full_name': 'test/repo'},
            'sender': {'login': 'testuser'},
            'action': 'opened',
            'pull_request': {'number': 1}
        }
        headers = {'HTTP_X_GITHUB_EVENT': 'pull_request'}
        
        response = self.client.post(
            self.url, 
            data=json.dumps(payload), 
            content_type='application/json',
            **headers
        )
        
        self.assertEqual(response.status_code, 202)
        self.assertEqual(GitHubWebhookEvent.objects.count(), 1)
        
        webhook = GitHubWebhookEvent.objects.first()
        self.assertEqual(webhook.event_type, 'pull_request')
        self.assertEqual(webhook.repository, 'test/repo')
        self.assertEqual(webhook.sender, 'testuser')
        
    def test_push_webhook(self):
        payload = {
            'repository': {'full_name': 'test/repo'},
            'sender': {'login': 'testuser'},
            'ref': 'refs/heads/main'
        }
        headers = {'HTTP_X_GITHUB_EVENT': 'push'}
        
        response = self.client.post(
            self.url, 
            data=json.dumps(payload), 
            content_type='application/json',
            **headers
        )
        
        self.assertEqual(response.status_code, 202)
        self.assertEqual(GitHubWebhookEvent.objects.count(), 1)
        
        webhook = GitHubWebhookEvent.objects.first()
        self.assertEqual(webhook.event_type, 'push')
        
    def test_issue_comment_webhook(self):
        payload = {
            'repository': {'full_name': 'test/repo'},
            'sender': {'login': 'testuser'},
            'action': 'created',
            'issue': {'number': 1},
            'comment': {'body': 'Test comment'}
        }
        headers = {'HTTP_X_GITHUB_EVENT': 'issue_comment'}
        
        response = self.client.post(
            self.url, 
            data=json.dumps(payload), 
            content_type='application/json',
            **headers
        )
        
        self.assertEqual(response.status_code, 202)
        self.assertEqual(GitHubWebhookEvent.objects.count(), 1)
        
        webhook = GitHubWebhookEvent.objects.first()
        self.assertEqual(webhook.event_type, 'issue_comment')
