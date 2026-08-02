"""Tests for the activation flow: pairing file -> code -> long-lived token.

The property worth protecting here is that the artifact an owner downloads is
never itself the credential, and that the credential it buys is subject to
exactly the same one-machine binding as one configured by hand. Both are easy
to regress silently -- a leaked-but-inert installer and a leaked-and-live one
look identical from the dashboard.
"""

import json
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import throttle
from .models import AgentActivationCode, AgentEnrollment, Membership, Tenant, User

HOST = "app.bytebalancetech.com"

#: Every request here is made over HTTPS against the platform host, because
#: with DJANGO_DEBUG=0 the settings turn on SECURE_SSL_REDIRECT and ALLOWED_HOSTS
#: -- plain HTTP would 301 and a bare "testserver" would 400, and both look like
#: the view being tested is broken. Passed explicitly rather than overridden
#: away, so these run against the same configuration production does.
REQUEST = {"HTTP_HOST": HOST, "secure": True}


class ActivationTestCase(TestCase):
    """Shared fixture: one shop, one owner, one pending enrollment."""

    def setUp(self):
        # The cache backing the throttle is file-based and shared, so without
        # this the counter leaks between tests and the order they run in
        # decides whether they pass.
        throttle.clear("agent-activate", "127.0.0.1")
        self.owner = User.objects.create_user(email="owner@example.com", password="pw")
        self.tenant = Tenant.objects.create(name="متجر الاختبار", slug="test-shop")
        Membership.objects.create(
            user=self.owner, tenant=self.tenant, role=Membership.Role.OWNER
        )
        self.enrollment, self.original_token = AgentEnrollment.issue(
            tenant=self.tenant, label="cashier-1", created_by=self.owner
        )

    def activate(self, code):
        return self.client.post(
            reverse("agent_activate"),
            data=json.dumps({"activation_code": code}),
            content_type="application/json",
            **REQUEST,
        )


class ActivationCodeModelTests(ActivationTestCase):
    def test_issuing_a_code_stores_only_a_digest(self):
        code, raw = AgentActivationCode.issue(self.enrollment)
        self.assertNotIn(raw, (code.code_hash, code.code_prefix))
        self.assertEqual(code.code_hash, AgentActivationCode.hash_code(raw))
        self.assertTrue(raw.startswith(code.code_prefix))

    def test_a_second_download_burns_the_first_code(self):
        _, first = AgentActivationCode.issue(self.enrollment)
        AgentActivationCode.issue(self.enrollment)
        self.assertFalse(AgentActivationCode.resolve(first).is_redeemable)

    def test_an_expired_code_is_found_but_not_redeemable(self):
        code, raw = AgentActivationCode.issue(self.enrollment)
        code.expires_at = timezone.now() - timedelta(seconds=1)
        code.save(update_fields=["expires_at"])
        self.assertIsNotNone(AgentActivationCode.resolve(raw))
        self.assertFalse(AgentActivationCode.resolve(raw).is_redeemable)

    def test_consuming_twice_yields_a_token_only_once(self):
        """The single-use guarantee, exercised the way a race would hit it.

        Two independent row objects for the same code, as two workers would
        have. This fails if ``consume`` ever goes back to trusting a flag it
        loaded earlier instead of claiming the row.
        """
        _, raw = AgentActivationCode.issue(self.enrollment)
        first = AgentActivationCode.resolve(raw)
        second = AgentActivationCode.resolve(raw)
        self.assertIsNotNone(first.consume())
        self.assertIsNone(second.consume())

    def test_rotating_a_token_kills_the_old_one_and_keeps_the_binding(self):
        self.enrollment.hostname = "SHOP-PC"
        self.enrollment.save(update_fields=["hostname"])

        new_token = self.enrollment.rotate_token()

        self.assertIsNone(AgentEnrollment.resolve(self.original_token))
        self.assertEqual(AgentEnrollment.resolve(new_token), self.enrollment)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.hostname, "SHOP-PC")


class ActivationEndpointTests(ActivationTestCase):
    def test_a_valid_code_is_exchanged_for_a_working_token(self):
        _, raw = AgentActivationCode.issue(self.enrollment)

        response = self.activate(raw)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tenant_slug"], "test-shop")
        self.assertEqual(body["agent_id"], self.enrollment.pk)
        # Still pending: activation hands over a credential, not permission.
        self.assertEqual(body["status"], AgentEnrollment.Status.PENDING)
        self.assertEqual(
            AgentEnrollment.resolve(body["platform_token"]), self.enrollment
        )

    def test_the_same_code_cannot_be_redeemed_twice(self):
        _, raw = AgentActivationCode.issue(self.enrollment)
        self.assertEqual(self.activate(raw).status_code, 200)
        self.assertEqual(self.activate(raw).status_code, 403)

    def test_unknown_expired_and_used_codes_are_indistinguishable(self):
        """A prober must not learn from the answer that a code once existed."""
        _, spent = AgentActivationCode.issue(self.enrollment)
        self.activate(spent)

        expired, expired_raw = AgentActivationCode.issue(self.enrollment)
        expired.expires_at = timezone.now() - timedelta(seconds=1)
        expired.save(update_fields=["expires_at"])

        bodies = {
            self.activate(candidate).content
            for candidate in ("never-existed", spent, expired_raw)
        }
        self.assertEqual(len(bodies), 1)

    def test_a_revoked_enrollment_cannot_be_activated(self):
        _, raw = AgentActivationCode.issue(self.enrollment)
        self.enrollment.revoke()
        self.assertEqual(self.activate(raw).status_code, 403)

    def test_malformed_json_is_rejected(self):
        response = self.client.post(
            reverse("agent_activate"),
            data="{not json",
            content_type="application/json",
            **REQUEST,
        )
        self.assertEqual(response.status_code, 400)

    def test_repeated_failures_are_throttled(self):
        for _ in range(throttle.MAX_FAILURES):
            self.assertEqual(self.activate("wrong").status_code, 403)
        self.assertEqual(self.activate("wrong").status_code, 429)

    def test_a_valid_code_still_works_below_the_throttle_ceiling(self):
        _, raw = AgentActivationCode.issue(self.enrollment)
        for _ in range(throttle.MAX_FAILURES - 1):
            self.activate("wrong")
        self.assertEqual(self.activate(raw).status_code, 200)


class ActivationPreservesMachineBindingTests(ActivationTestCase):
    """The question this flow had to answer before it could be built.

    An activation-derived token must go through agent_register's one-machine
    check exactly like a hand-configured one. The binding lives on the
    enrollment row, not on the token, so rotating the token must not become a
    way to move an agent to a different PC without anyone noticing.
    """

    def register(self, token, hostname):
        return self.client.post(
            reverse("agent_register"),
            data=json.dumps({"hostname": hostname, "os": "Windows", "db_folder": "D:/"}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
            **REQUEST,
        )

    def test_first_registration_binds_the_machine(self):
        _, raw = AgentActivationCode.issue(self.enrollment)
        token = self.activate(raw).json()["platform_token"]

        self.assertEqual(self.register(token, "SHOP-PC").status_code, 200)
        self.enrollment.refresh_from_db()
        self.assertEqual(self.enrollment.hostname, "SHOP-PC")

    def test_a_reactivated_token_still_cannot_move_to_another_machine(self):
        _, first = AgentActivationCode.issue(self.enrollment)
        self.register(self.activate(first).json()["platform_token"], "SHOP-PC")

        _, second = AgentActivationCode.issue(self.enrollment)
        moved = self.activate(second).json()["platform_token"]

        self.assertEqual(self.register(moved, "THIEF-PC").status_code, 409)
        # ...and the legitimate reinstall on the same PC still goes through.
        self.assertEqual(self.register(moved, "SHOP-PC").status_code, 200)


class PairingDownloadTests(ActivationTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.url = reverse(
            "enrollment_installer", args=[self.tenant.slug, self.enrollment.pk]
        )

    def test_downloading_mints_a_code_and_never_ships_the_token(self):
        response = self.client.post(self.url, **REQUEST)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertIn("PLATFORM_ACTIVATION_CODE=", body)
        self.assertNotIn("PLATFORM_TOKEN=", body)
        self.assertNotIn(self.original_token, body)

        code = body.split("PLATFORM_ACTIVATION_CODE=")[1].splitlines()[0].strip()
        self.assertTrue(AgentActivationCode.resolve(code).is_redeemable)

    def test_the_file_is_named_apart_from_the_hand_configured_env(self):
        response = self.client.post(self.url, **REQUEST)
        self.assertIn("sahlisoft-pairing", response["Content-Disposition"])

    def test_it_can_be_downloaded_again_at_any_time(self):
        """The whole reason this view exists next to enrollment_config."""
        first = self.client.post(self.url, **REQUEST)
        second = self.client.post(self.url, **REQUEST)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.content, second.content)

    def test_a_revoked_agent_gets_no_pairing_file(self):
        self.enrollment.revoke()
        response = self.client.post(self.url, **REQUEST)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(AgentActivationCode.objects.exists())

    def test_a_stranger_cannot_download_another_shops_pairing_file(self):
        other = User.objects.create_user(email="other@example.com", password="pw")
        self.client.force_login(other)
        self.assertEqual(self.client.post(self.url, **REQUEST).status_code, 404)
