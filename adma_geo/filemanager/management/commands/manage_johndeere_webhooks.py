"""
Manage John Deere Data Subscription Service subscriptions.

Usage:
    python manage.py manage_johndeere_webhooks --create
    python manage.py manage_johndeere_webhooks --list
    python manage.py manage_johndeere_webhooks --show <sub_id>
    python manage.py manage_johndeere_webhooks --delete <sub_id>

Reads configuration from Django settings (env-driven):
    JD_CLIENT_ID, JD_CLIENT_SECRET, JD_REFRESH_TOKEN, JD_ORG_ID
    JD_WEBHOOK_CALLBACK_URL, JD_WEBHOOK_USERNAME, JD_WEBHOOK_PASSWORD
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from filemanager.johndeere_client import JohnDeereClient
from filemanager.models import JohnDeereSubscription


DEFAULT_EVENT_TYPE_IDS = [
    'fieldCreated', 'fieldUpdated', 'fieldArchived', 'fieldDeleted',
    'boundaryCreated', 'boundaryUpdated',
    'fieldOperationCreated', 'fieldOperationUpdated',
]


class Command(BaseCommand):
    help = 'Create, list, or delete John Deere DSS subscriptions.'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--create', action='store_true',
                           help='Create a new subscription using settings values.')
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
        return JohnDeereClient(
            client_id=settings.JD_CLIENT_ID,
            client_secret=settings.JD_CLIENT_SECRET,
            refresh_token=settings.JD_REFRESH_TOKEN,
        )

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

    def _create(self, client):
        self._require_webhook_settings()
        callback_url = settings.JD_WEBHOOK_CALLBACK_URL
        self.stdout.write(f"Creating subscription → {callback_url}")

        result = client.create_subscription(
            client_endpoint=callback_url,
            username=settings.JD_WEBHOOK_USERNAME,
            password=settings.JD_WEBHOOK_PASSWORD,
            event_type_ids=DEFAULT_EVENT_TYPE_IDS,
            org_id=settings.JD_ORG_ID,
        )
        jd_sub_id = result.get('id')
        if not jd_sub_id:
            raise CommandError(f"JD response missing id: {result}")

        JohnDeereSubscription.objects.create(
            jd_subscription_id=jd_sub_id,
            org_id=str(settings.JD_ORG_ID),
            event_type_ids=DEFAULT_EVENT_TYPE_IDS,
            client_endpoint=callback_url,
        )
        self.stdout.write(self.style.SUCCESS(
            f"Subscription created: {jd_sub_id}"
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
