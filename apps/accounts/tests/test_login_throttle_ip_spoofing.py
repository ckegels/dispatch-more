"""LoginRateThrottle identity resists X-Forwarded-For spoofing.

nginx appends the real connecting peer to X-Forwarded-For rather than
replacing it, so any prefix on that header is attacker controlled. The
login throttle must key off the resolved trusted-proxy-aware client IP
(get_client_ip), not the raw header, or an attacker can mint a fresh
throttle bucket on every request by varying the prefix and never get
rate limited.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()


class LoginThrottleSpoofingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()
        User.objects.create_superuser(
            username="spoofvictim", password="correct-password", user_level=10
        )

    def tearDown(self):
        cache.clear()

    def _post_token(self, forwarded_for):
        return self.api.post(
            "/api/accounts/token/",
            {"username": "spoofvictim", "password": "wrong-password"},
            format="json",
            HTTP_X_FORWARDED_FOR=forwarded_for,
        )

    def test_varying_forwarded_for_prefix_does_not_reset_the_budget(self):
        # REMOTE_ADDR is the test client's loopback default (the trusted
        # nginx hop). The rightmost address, 203.0.113.7, is the real peer
        # nginx appended; everything to its left is attacker-suppliable.
        for i in range(3):
            response = self._post_token(f"{i}.{i}.{i}.{i}, 203.0.113.7")
            self.assertNotEqual(response.status_code, 429)

        response = self._post_token("9.9.9.9, 203.0.113.7")
        self.assertEqual(response.status_code, 429)

    def test_different_real_peers_get_independent_budgets(self):
        for i in range(3):
            response = self._post_token(f"noise, 203.0.113.{i}")
            self.assertNotEqual(response.status_code, 429)

        # A fourth distinct real peer is unaffected by the other three.
        response = self._post_token("noise, 203.0.113.42")
        self.assertNotEqual(response.status_code, 429)


class LoginThrottleFailuresOnlyTests(TestCase):
    """Dispatch More: while Watch together is on, correct logins never use up the budget."""

    def setUp(self):
        from apps.proxy.live_proxy import app_devices

        cache.clear()
        app_devices._HELD.update(at=0.0, value=None)
        self.addCleanup(app_devices._HELD.update, at=0.0, value=None)
        self.api = APIClient()
        User.objects.create_superuser(username="tv", password="right", user_level=10)

    def tearDown(self):
        cache.clear()

    def _login(self, password):
        return self.api.post("/api/accounts/token/", {"username": "tv", "password": password}, format="json")

    def test_with_watch_together_off_every_login_counts_as_in_stock(self):
        for _ in range(3):
            self.assertEqual(self._login("right").status_code, 200)
        self.assertEqual(self._login("right").status_code, 429)

    def test_with_watch_together_on_correct_logins_never_lock_the_address_out(self):
        from apps.proxy.live_proxy import app_devices

        app_devices.save_settings({"devices": True, "link_devices": True})
        for _ in range(6):
            self.assertEqual(self._login("right").status_code, 200)

    def test_with_watch_together_on_three_wrong_passwords_still_lock_it(self):
        from apps.proxy.live_proxy import app_devices

        app_devices.save_settings({"devices": True, "link_devices": True})
        for _ in range(3):
            self.assertEqual(self._login("wrong").status_code, 401)
        self.assertEqual(self._login("wrong").status_code, 429)
        # ...and a correct one from there too, until the minute is over
        self.assertEqual(self._login("right").status_code, 429)
