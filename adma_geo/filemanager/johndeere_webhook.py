"""
John Deere Data Subscription Service webhook receiver.

Authenticates incoming events via HTTP Basic Auth, dedups on the JD event id,
persists the event, acks 200, and dispatches to a Celery task for processing.
"""
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def johndeere_webhook_receiver(request):
    """Placeholder — real behavior added in Task 3."""
    return JsonResponse({"status": "not_implemented"}, status=501)
