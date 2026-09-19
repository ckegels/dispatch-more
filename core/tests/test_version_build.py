"""The version the page shows says when this is a modified build, not official Dispatcharr."""

from django.test import TestCase
from rest_framework.test import APIClient

import version


class VersionBuildTests(TestCase):
    def test_the_build_is_said(self):
        data = APIClient().get("/api/core/version/").json()
        self.assertEqual(data["version"], version.__version__)
        self.assertTrue(data["build"].startswith("Dispatch More"))

    def test_the_version_itself_is_the_official_one(self):
        """It goes to providers in the User-Agent and is compared with official releases."""
        self.assertRegex(version.__version__, r"^\d+\.\d+\.\d+$")
