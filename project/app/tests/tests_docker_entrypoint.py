"""The Docker demo image has to serve the committed React bundle."""

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

ENTRYPOINT = Path(settings.BASE_DIR) / "docker" / "entrypoint.sh"


class DockerEntrypointTests(SimpleTestCase):
    def test_the_dev_server_is_told_to_serve_static_files(self):
        """`docker compose up` runs with DEBUG off, and `runserver` then serves
        no static files at all: every page shell loads and renders blank."""
        serve = [
            line
            for line in ENTRYPOINT.read_text().splitlines()
            if "runserver" in line and not line.lstrip().startswith("#")
        ]

        self.assertEqual(len(serve), 1)
        self.assertIn("--insecure", serve[0])
