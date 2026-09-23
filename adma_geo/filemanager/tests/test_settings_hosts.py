import importlib
import os

from django.test import SimpleTestCase


class CsvEnvSettingsTests(SimpleTestCase):
    """ALLOWED_HOSTS / CSRF_TRUSTED_ORIGINS are env-driven.

    The demo deployment on adma.aisoup.net used to carry these as an
    uncommitted edit to settings.py that every git pull clobbered.
    """

    def _read(self, env):
        """Reload settings under `env` and return the two lists.

        The values are snapshotted before the environment is restored --
        reloading the module again would overwrite them on the shared module
        object before the caller ever saw them.
        """
        from adma_geo import settings

        previous = {key: os.environ.get(key) for key in env}
        for key, value in env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            importlib.reload(settings)
            return list(settings.ALLOWED_HOSTS), list(settings.CSRF_TRUSTED_ORIGINS)
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            importlib.reload(settings)

    def test_defaults_match_the_previously_hardcoded_lists(self):
        hosts, origins = self._read({
            'DJANGO_ALLOWED_HOSTS': None,
            'DJANGO_CSRF_TRUSTED_ORIGINS': None,
        })
        self.assertEqual(hosts, ['localhost', '127.0.0.1', '0.0.0.0', 'adma.unl.edu'])
        self.assertEqual(origins, [
            'https://adma.unl.edu', 'http://adma.unl.edu',
            'http://localhost', 'https://localhost',
        ])

    def test_env_overrides_and_tolerates_whitespace_and_blanks(self):
        hosts, origins = self._read({
            'DJANGO_ALLOWED_HOSTS': 'adma.aisoup.net, localhost ,',
            'DJANGO_CSRF_TRUSTED_ORIGINS': 'https://adma.aisoup.net',
        })
        self.assertEqual(hosts, ['adma.aisoup.net', 'localhost'])
        self.assertEqual(origins, ['https://adma.aisoup.net'])
