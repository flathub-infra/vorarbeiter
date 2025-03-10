import hmac
import json
import logging
from django.http import HttpResponse, HttpResponseForbidden, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings
from .models import GitHubWebhookEvent


logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def github_webhook(request):
    event_type = request.headers.get('X-GitHub-Event')
    
    if event_type not in ['pull_request', 'push', 'issue_comment']:
        return HttpResponseBadRequest(f"Unsupported event type: {event_type}")
    
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON payload")
    
    try:
        repository = payload['repository']['full_name']
        sender = payload['sender']['login']
    except KeyError:
        return HttpResponseBadRequest("Missing required payload fields")
    
    webhook = GitHubWebhookEvent.objects.create(
        event_type=event_type,
        payload=payload,
        repository=repository,
        sender=sender
    )
    
    logger.info(f"Received {event_type} webhook from {repository} by {sender}")
    
    return HttpResponse("Webhook received", status=202)
