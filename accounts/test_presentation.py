"""Tests for what the dashboard actually says, as opposed to what it stores.

Two things here are easy to get wrong in ways no exception ever reports: Arabic
plural agreement, which is silently wrong rather than broken, and the moment an
agent stops counting as reachable, which decides whether a shop owner sees a
green light next to a machine that is switched off.
"""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import AgentEnrollment, Membership, Tenant, User
from .templatetags.ar_time import ar_timesince
from .views import MAX_OWNED_SHOPS, _overview, _shop_rows

#: DJANGO_DEBUG=0 turns on SECURE_SSL_REDIRECT and ALLOWED_HOSTS, so plain HTTP
#: would 301 and a bare "testserver" would 400 -- both look like a broken view.
REQUEST = {"HTTP_HOST": "app.bytebalancetech.com", "secure": True}


class ArabicRelativeTimeTests(TestCase):
    """Every boundary where Arabic changes the form of the noun.

    Django's own timesince gets 2 and 11 wrong in Arabic (it has two plural
    forms to work with and Arabic needs four), which is the entire reason this
    filter exists -- so those two counts are the ones worth pinning.
    """

    def assertSince(self, delta, expected):
        now = timezone.now()
        self.assertEqual(ar_timesince(now - delta, now=now), expected)

    def test_moments_ago_when_under_the_floor(self):
        self.assertSince(timedelta(seconds=0), "منذ لحظات")
        self.assertSince(timedelta(seconds=44), "منذ لحظات")

    def test_singular_and_dual_carry_no_numeral(self):
        # "منذ 1 دقيقة" is the machine-sounding phrasing this filter exists to
        # avoid: Arabic marks one and two in the noun, not with a digit.
        self.assertSince(timedelta(minutes=1), "منذ دقيقة")
        self.assertSince(timedelta(minutes=2), "منذ دقيقتين")
        self.assertSince(timedelta(hours=1), "منذ ساعة")
        self.assertSince(timedelta(hours=2), "منذ ساعتين")
        self.assertSince(timedelta(days=1), "منذ يوم")
        self.assertSince(timedelta(days=2), "منذ يومين")

    def test_three_to_ten_take_the_broken_plural(self):
        self.assertSince(timedelta(minutes=3), "منذ 3 دقائق")
        self.assertSince(timedelta(minutes=10), "منذ 10 دقائق")
        self.assertSince(timedelta(hours=4), "منذ 4 ساعات")
        self.assertSince(timedelta(days=5), "منذ 5 أيام")

    def test_eleven_and_up_take_the_accusative_singular(self):
        self.assertSince(timedelta(minutes=11), "منذ 11 دقيقة")
        self.assertSince(timedelta(minutes=59), "منذ 59 دقيقة")
        self.assertSince(timedelta(hours=23), "منذ 23 ساعة")
        # Days and months differ from their own singular here (يوم -> يوماً),
        # which is why the table carries a fourth column at all.
        self.assertSince(timedelta(days=12), "منذ 12 يوماً")

    def test_larger_units_win_over_smaller_ones(self):
        self.assertSince(timedelta(days=45), "منذ شهر")
        self.assertSince(timedelta(days=400), "منذ سنة")

    def test_none_renders_as_nothing_so_templates_can_branch(self):
        self.assertEqual(ar_timesince(None), "")

    def test_a_clock_skewed_into_the_future_does_not_produce_a_negative(self):
        now = timezone.now()
        self.assertEqual(ar_timesince(now + timedelta(minutes=5), now=now), "منذ لحظات")


class AgentLivenessTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name="متجر", slug="shop")
        self.enrollment, _ = AgentEnrollment.issue(tenant=self.tenant)

    def test_never_seen_is_not_online(self):
        self.assertIsNone(self.enrollment.last_seen_at)
        self.assertFalse(self.enrollment.is_online)

    def test_a_recent_heartbeat_is_online(self):
        self.enrollment.last_seen_at = timezone.now() - timedelta(seconds=30)
        self.assertTrue(self.enrollment.is_online)

    def test_the_window_is_three_missed_beats_not_one(self):
        # One dropped request on a shop's mobile connection must not flicker
        # the light, so a two-minute gap is still online.
        self.enrollment.last_seen_at = timezone.now() - timedelta(minutes=2)
        self.assertTrue(self.enrollment.is_online)

        self.enrollment.last_seen_at = timezone.now() - timedelta(minutes=4)
        self.assertFalse(self.enrollment.is_online)

    def test_liveness_is_independent_of_status(self):
        """A stopped agent keeps checking in -- that is what makes stop undoable.

        Folding status into is_online would make the dashboard claim a machine
        that is plainly answering is dark.
        """
        self.enrollment.last_seen_at = timezone.now()
        self.enrollment.stop()
        self.assertTrue(self.enrollment.is_online)


class DashboardOverviewTests(TestCase):
    """The four tiles above the table, and the table, must agree.

    They are computed from one list of rows precisely so they cannot drift;
    these pin that they actually do.
    """

    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com", password="pw")
        self.tenant = Tenant.objects.create(name="متجر", slug="shop")
        Membership.objects.create(
            user=self.owner, tenant=self.tenant, role=Membership.Role.OWNER
        )
        now = timezone.now()

        online, _ = AgentEnrollment.issue(tenant=self.tenant, label="كاشير")
        online.approve()
        online.last_seen_at = now - timedelta(seconds=20)
        online.save()

        offline, _ = AgentEnrollment.issue(tenant=self.tenant, label="مستودع")
        offline.approve()
        offline.last_seen_at = now - timedelta(hours=5)
        offline.save()

        AgentEnrollment.issue(tenant=self.tenant, label="بانتظار")

        stopped, _ = AgentEnrollment.issue(tenant=self.tenant, label="متوقف")
        stopped.stop()

    def test_counts(self):
        overview = _overview(_shop_rows(self.owner))
        self.assertEqual(overview["shops"], 1)
        self.assertEqual(overview["agents"], 4)
        self.assertEqual(overview["online"], 1)
        self.assertEqual(overview["pending"], 1)
        self.assertEqual(overview["inactive"], 1)

    def test_a_shop_with_no_agents_still_counts_as_a_shop(self):
        empty = Tenant.objects.create(name="فارغ", slug="empty")
        Membership.objects.create(
            user=self.owner, tenant=empty, role=Membership.Role.OWNER
        )
        overview = _overview(_shop_rows(self.owner))
        self.assertEqual(overview["shops"], 2)
        self.assertEqual(overview["agents"], 4)

    def test_the_page_renders_the_tiles_and_the_live_indicator(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("dashboard"), **REQUEST)
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('class="tiles"', html)
        # Exactly one agent is reachable, so exactly one pulsing dot.
        self.assertEqual(html.count("dot dot-live"), 1)
        # The pending tile is the only tinted one, and only while something is
        # actually pending. Matched on the rendered attribute, not the bare
        # class name: the stylesheet is inline, so ".tile-act" is on every page
        # whether or not anything uses it.
        self.assertIn('class="tile tile-act"', html)

    def test_the_pending_tile_stops_shouting_once_nothing_is_pending(self):
        AgentEnrollment.objects.filter(
            status=AgentEnrollment.Status.PENDING
        ).update(status=AgentEnrollment.Status.ACTIVE)

        self.client.force_login(self.owner)
        html = self.client.get(reverse("dashboard"), **REQUEST).content.decode()
        self.assertNotIn('class="tile tile-act"', html)

    def test_the_shop_ceiling_is_reported_from_one_place(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("dashboard"), **REQUEST).content.decode()
        self.assertIn(f"من أصل {MAX_OWNED_SHOPS} مسموح بها", html)


class MessageLevelTests(TestCase):
    """Success and failure must not render as the same object on the page.

    Before this, every message went into one neutral grey box, so "تم الحذف"
    and "تعذّر الحذف" were distinguishable only by reading them.
    """

    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com", password="pw")
        self.tenant = Tenant.objects.create(name="متجر", slug="shop")
        Membership.objects.create(
            user=self.owner, tenant=self.tenant, role=Membership.Role.OWNER
        )
        self.client.force_login(self.owner)

    def test_success_and_error_carry_different_classes(self):
        pending, _ = AgentEnrollment.issue(tenant=self.tenant, label="جهاز")
        approve = reverse("enrollment_approve", args=[self.tenant.slug, pending.pk])

        # Matched on the rendered attribute rather than the bare class name:
        # the stylesheet is inline, so every level's rule is present in the
        # markup of every page and a substring test would always pass.
        html = self.client.post(approve, follow=True, **REQUEST).content.decode()
        self.assertIn('class="msg msg-success"', html)
        self.assertNotIn('class="msg msg-error"', html)

        # Approving an already-active agent is an informational no-op, not a
        # success -- and must not be dressed as one.
        html = self.client.post(approve, follow=True, **REQUEST).content.decode()
        self.assertIn('class="msg msg-info"', html)
        self.assertNotIn('class="msg msg-success"', html)

    def test_a_refused_delete_renders_as_an_error(self):
        active, _ = AgentEnrollment.issue(tenant=self.tenant, label="جهاز")
        active.approve()
        delete = reverse("enrollment_delete", args=[self.tenant.slug, active.pk])

        html = self.client.post(delete, follow=True, **REQUEST).content.decode()
        self.assertIn('class="msg msg-error"', html)
        self.assertIn("أوقف العميل أولاً قبل حذفه.", html)
        # And the refusal has to be real, not just styled.
        self.assertTrue(AgentEnrollment.objects.filter(pk=active.pk).exists())

    def test_messages_are_announced_politely_and_can_be_dismissed(self):
        pending, _ = AgentEnrollment.issue(tenant=self.tenant, label="جهاز")
        html = self.client.post(
            reverse("enrollment_stop", args=[self.tenant.slug, pending.pk]),
            follow=True,
            **REQUEST,
        ).content.decode()
        self.assertIn('aria-live="polite"', html)
        self.assertIn("data-msg-close", html)
