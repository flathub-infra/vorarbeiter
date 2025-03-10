import hmac
import hashlib
import json
import logging
from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from .models import GitHubWebhookEvent


logger = logging.getLogger(__name__)


def verify_github_signature(request):
    if not settings.GITHUB_WEBHOOK_SECRET:
        logger.warning("GitHub webhook secret not configured, rejecting webhook")
        return False

    signature_header = request.headers.get("X-Hub-Signature-256")
    if not signature_header:
        logger.warning("No X-Hub-Signature-256 header in request")
        return False

    payload = request.body
    expected_signature = (
        "sha256="
        + hmac.new(
            settings.GITHUB_WEBHOOK_SECRET.encode(), payload, hashlib.sha256
        ).hexdigest()
    )

    return hmac.compare_digest(signature_header, expected_signature)


@csrf_exempt
@require_POST
def github_webhook(request):
    if not verify_github_signature(request):
        return HttpResponseForbidden("Invalid signature")

    event_type = request.headers.get("X-GitHub-Event")

    if event_type == "ping":
        logger.info("Received ping event from GitHub")
        return HttpResponse("Webhook configured successfully", status=200)

    if event_type not in ["pull_request", "push", "issue_comment"]:
        return HttpResponseBadRequest(f"Unsupported event type: {event_type}")

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON payload")

    try:
        repository = payload["repository"]["full_name"]
        sender = payload["sender"]["login"]
    except KeyError:
        return HttpResponseBadRequest("Missing required payload fields")

    delivery_id = request.headers.get("X-GitHub-Delivery")
    if not delivery_id:
        return HttpResponseBadRequest("Missing X-GitHub-Delivery header")

    try:
        GitHubWebhookEvent.objects.create(
            id=delivery_id,
            event_type=event_type,
            payload=payload,
            repository=repository,
            sender=sender,
        )
        logger.info(
            f"Received {event_type} webhook from {repository} by {sender} (delivery: {delivery_id})"
        )
    except Exception as e:
        logger.warning(f"Failed to create webhook event: {e}")
        return HttpResponse("Webhook already processed", status=200)

    return HttpResponse("Webhook received", status=202)
