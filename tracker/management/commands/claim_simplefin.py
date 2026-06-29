from django.core.management.base import BaseCommand

from tracker.connectors.simplefin import claim_access_url
from tracker.models import ConnectionCredential, ConnectorType


class Command(BaseCommand):
    help = 'Exchange a one-time SimpleFIN setup token for a durable access URL and store it encrypted.'

    def add_arguments(self, parser):
        parser.add_argument('setup_token', help='Base64 setup token from SimpleFIN Bridge.')

    def handle(self, *args, **opts):
        access_url = claim_access_url(opts['setup_token'])
        cred, _ = ConnectionCredential.objects.get_or_create(
            connector_type=ConnectorType.SIMPLEFIN, institution=None)
        cred.set_secret(access_url)
        cred.status = 'ok'
        cred.save()
        self.stdout.write(self.style.SUCCESS('Stored SimpleFIN access URL (encrypted).'))
