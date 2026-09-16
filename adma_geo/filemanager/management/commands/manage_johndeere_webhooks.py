"""
Manage John Deere Data Subscription Service subscriptions.

Usage:
    python manage.py manage_johndeere_webhooks --create
    python manage.py manage_johndeere_webhooks --set-auth
    python manage.py manage_johndeere_webhooks --list
    python manage.py manage_johndeere_webhooks --show <sub_id>
    python manage.py manage_johndeere_webhooks --delete <sub_id>

Reads configuration from Django settings (env-driven):
    JD_CLIENT_ID, JD_CLIENT_SECRET, JD_REFRESH_TOKEN, JD_ORG_ID
    JD_WEBHOOK_CALLBACK_URL, JD_WEBHOOK_USERNAME, JD_WEBHOOK_PASSWORD
"""
import base64

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from filemanager.johndeere_client import JohnDeereClient
from filemanager.models import JohnDeereSubscription


# DSS publishes one coarse event type per resource; there is no separate
# created/updated/deleted type. See the Event Types table in the Operations
# Center - Webhook docs.
DEFAULT_EVENT_TYPE_IDS = ['field', 'boundary', 'fieldOperation']


class Command(BaseCommand):
    help = 'Create, list, or delete John Deere DSS subscriptions.'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--create', action='store_true',
                           help='Create one subscription per event type using settings values.')
        group.add_argument('--set-auth', action='store_true', dest='set_auth',
                           help="Register the callback's Basic credentials with JD "
                                "and print the resulting delivery settings.")
        group.add_argument('--list', action='store_true',
                           help='List all subscriptions (local + remote).')
        group.add_argument('--show', metavar='SUB_ID',
                           help='Show one subscription by JD subscription id.')
        group.add_argument('--delete', metavar='SUB_ID',
                           help='Delete a subscription remotely and locally.')

    def handle(self, *args, **options):
        client = self._build_client()

        if options['create']:
            self._create(client)
        elif options['set_auth']:
            self._set_auth(client)
        elif options['list']:
            self._list(client)
        elif options['show']:
            self._show(client, options['show'])
        elif options['delete']:
            self._delete(client, options['delete'])

    def _build_client(self):
        for key in ('JD_CLIENT_ID', 'JD_CLIENT_SECRET', 'JD_REFRESH_TOKEN'):
            if not getattr(settings, key, None):
                raise CommandError(f"{key} is not set")
        client = JohnDeereClient(
            client_id=settings.JD_CLIENT_ID,
            client_secret=settings.JD_CLIENT_SECRET,
            refresh_token=settings.JD_REFRESH_TOKEN,
        )
        self.stdout.write(f"John Deere API: {client.API_BASE_URL}")
        return client

    def _require_webhook_settings(self):
        missing = [
            k for k in ('JD_WEBHOOK_CALLBACK_URL',
                        'JD_WEBHOOK_USERNAME',
                        'JD_WEBHOOK_PASSWORD')
            if not getattr(settings, k, None)
        ]
        if missing:
            raise CommandError(
                "Missing required settings: " + ", ".join(missing)
            )

    def _basic_header(self):
        token = base64.b64encode(
            f"{settings.JD_WEBHOOK_USERNAME}:{settings.JD_WEBHOOK_PASSWORD}".encode()
        ).decode()
        return f"Basic {token}"

    def _set_auth(self, client):
        """Register the Authorization header DSS sends us.

        DSS carries no per-subscription credentials: one header value, set on
        the client's delivery settings, is sent with every event — including
        the subscriptionVerification probe that decides whether a new
        subscription is accepted. So this must succeed before --create runs.
        """
        self._require_webhook_settings()
        result = client.update_delivery(
            authorizationHeaderValue=self._basic_header()
        )
        configured = bool(result.get('authorizationHeaderValue')) if result else None
        self.stdout.write(self.style.SUCCESS(
            "Delivery authorization header registered"
            + (" (confirmed set)" if configured else "")
        ))
        return result

    def _create(self, client):
        self._require_webhook_settings()
        callback_url = settings.JD_WEBHOOK_CALLBACK_URL

        # Order matters: without the header in place, JD's verification POST
        # arrives unauthenticated and our receiver — correctly — rejects it.
        self._set_auth(client)

        self.stdout.write(f"Creating subscriptions \u2192 {callback_url}")
        created = []
        for event_type_id in DEFAULT_EVENT_TYPE_IDS:
            result = client.create_subscription(
                event_type_id=event_type_id,
                target_uri=callback_url,
                org_id=settings.JD_ORG_ID,
            )
            jd_sub_id = result.get('id')
            if not jd_sub_id:
                raise CommandError(f"JD response missing id: {result}")

            JohnDeereSubscription.objects.update_or_create(
                jd_subscription_id=jd_sub_id,
                defaults={
                    'org_id': str(settings.JD_ORG_ID),
                    'event_type_ids': [event_type_id],
                    'client_endpoint': callback_url,
                },
            )
            created.append((event_type_id, jd_sub_id))
            self.stdout.write(self.style.SUCCESS(
                f"  {event_type_id}: {jd_sub_id}"
            ))

        self.stdout.write(self.style.SUCCESS(
            f"{len(created)} subscription(s) created"
        ))

    def _list(self, client):
        remote = client.list_subscriptions()
        local = {
            s.jd_subscription_id: s
            for s in JohnDeereSubscription.objects.all()
        }
        self.stdout.write(f"Remote subscriptions: {len(remote)}")
        for sub in remote:
            sid = sub.get('id', '?')
            has_local = 'LOCAL' if sid in local else 'REMOTE-ONLY'
            self.stdout.write(f"  [{has_local}] {sid}")
        orphan_local = [sid for sid in local if not any(
            r.get('id') == sid for r in remote
        )]
        for sid in orphan_local:
            self.stdout.write(f"  [LOCAL-ONLY] {sid}")

    def _show(self, client, subscription_id):
        remote = client.list_subscriptions()
        for sub in remote:
            if sub.get('id') == subscription_id:
                self.stdout.write(str(sub))
                return
        raise CommandError(f"Subscription {subscription_id} not found remotely")

    def _delete(self, client, subscription_id):
        ok = client.delete_subscription(subscription_id)
        if not ok:
            raise CommandError(
                f"Remote delete failed for {subscription_id}; aborting local delete"
            )
        deleted = JohnDeereSubscription.objects.filter(
            jd_subscription_id=subscription_id
        ).delete()
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {subscription_id} (local rows removed: {deleted[0]})"
        ))
