"""
Manual verification helper for John Deere webhook plumbing.

Usage:
    python manage.py verify_johndeere_webhook

Prints the 10 most recent events from the DB along with their status and
timestamps, so an operator can confirm whether events are arriving and
being processed end-to-end.
"""
from django.core.management.base import BaseCommand

from filemanager.models import JohnDeereSubscription, JohnDeereWebhookEvent


class Command(BaseCommand):
    help = 'Print recent JD subscriptions and webhook events for verification.'

    def handle(self, *args, **options):
        subs = JohnDeereSubscription.objects.filter(is_active=True)
        self.stdout.write(self.style.SUCCESS(
            f"Active subscriptions: {subs.count()}"
        ))
        for s in subs:
            self.stdout.write(
                f"  {s.jd_subscription_id}  org={s.org_id}  "
                f"endpoint={s.client_endpoint}"
            )

        events = JohnDeereWebhookEvent.objects.order_by('-received_at')[:10]
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Most recent events: {events.count()}"
        ))
        for e in events:
            self.stdout.write(
                f"  [{e.status}] {e.event_type_id}  "
                f"id={e.jd_event_id}  received={e.received_at.isoformat()}  "
                f"error={e.error_message or '-'}"
            )
