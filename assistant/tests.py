"""Tests for the assistant.

Three things here are worth protecting, and none of them are the model:

* **Rendering is an injection boundary.** The assistant's answers are built
  from rows read out of a shop's own database, so a material name is untrusted
  input that reaches an HTML template by an entirely ordinary path.
* **A conversation can read a shop's whole books**, so the tenant gate on these
  views has to hold exactly as hard as the one on issuing a credential.
* **The heartbeat pacing** is what makes the feature usable at all; a
  regression there turns a ten-second answer back into a three-minute one.
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from accounts.api_views import BUSY_POLL_SECONDS, IDLE_POLL_SECONDS, _poll_after
from accounts.models import AgentCommand, AgentEnrollment, Membership, Tenant, User

from . import claude, markup
from .models import Conversation, Message, Run

REQUEST = {"HTTP_HOST": "app.bytebalancetech.com", "secure": True}


class MarkupEscapingTests(TestCase):
    """Escape first, then add markup. Everything else here depends on it."""

    def test_shop_data_cannot_become_markup(self):
        # A material named this way in a real inventory is all it takes.
        evil = '<img src=x onerror="alert(1)">'
        out = markup.render(f"أكثر مادة مبيعاً: {evil}")
        self.assertNotIn("<img", out)
        self.assertIn("&lt;img", out)

    def test_script_tags_inside_a_table_cell_are_escaped(self):
        out = markup.render(
            "| المادة | الكمية |\n|---|---|\n| <script>x</script> | 5 |"
        )
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        # ...and the cell is still a real cell.
        self.assertIn("<td>", out)

    def test_quotes_in_data_cannot_break_out_of_an_attribute(self):
        out = markup.render('اسم: " onmouseover="alert(1)')
        self.assertNotIn('onmouseover="alert(1)"', out)

    def test_a_markdown_table_becomes_a_table(self):
        out = markup.render(
            "| الشهر | المبيعات |\n|---|---|\n| كانون الثاني | 1,200 |\n"
            "| شباط | 3,400 |"
        )
        self.assertIn("<table>", out)
        self.assertIn("<th>الشهر</th>", out)
        self.assertEqual(out.count("<tr>"), 3)  # header + two rows

    def test_bold_and_inline_code(self):
        out = markup.render("المجموع **1,200** ليرة عبر `BillItems`")
        self.assertIn("<strong>1,200</strong>", out)
        self.assertIn("<code>BillItems</code>", out)

    def test_unsupported_markdown_stays_literal_rather_than_half_rendered(self):
        out = markup.render("# عنوان\n\n[رابط](https://example.com)")
        self.assertNotIn("<h1", out)
        self.assertNotIn("<a ", out)
        self.assertIn("# عنوان", out)

    def test_empty_input(self):
        self.assertEqual(markup.render(""), "")
        self.assertEqual(markup.render(None), "")


class MessageBlockTests(TestCase):
    def setUp(self):
        tenant = Tenant.objects.create(name="متجر", slug="shop")
        user = User.objects.create_user(email="o@example.com", password="pw")
        self.conversation = Conversation.objects.create(tenant=tenant, user=user)

    def test_flatten_keeps_text_and_drops_thinking(self):
        """Thinking is the model's scratchpad, not the answer.

        It has to be stored (it goes back to the API unchanged on the next
        turn) but printing it next to the answer would show the owner
        reasoning that was never meant to be shown.
        """
        blocks = [
            {"type": "thinking", "thinking": "let me check the sites table"},
            {"type": "text", "text": "مبيعاتك 1,200"},
        ]
        self.assertEqual(Message.flatten(blocks), "مبيعاتك 1,200")

    def test_tool_uses_are_exposed_for_the_audit_panel(self):
        message = Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.ASSISTANT,
            blocks=[
                {"type": "text", "text": "لحظة"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "query",
                    "input": {"sql": 'select 1 from "Sites"'},
                },
            ],
        )
        self.assertEqual(len(message.tool_uses), 1)
        self.assertEqual(message.tool_uses[0]["name"], "query")

    def test_a_tool_result_turn_is_recognised_so_it_can_be_hidden(self):
        message = Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.USER,
            blocks=[{"type": "tool_result", "tool_use_id": "toolu_1", "content": "[]"}],
        )
        self.assertTrue(message.is_tool_result)

    def test_a_typed_question_is_not_a_tool_result(self):
        message = Message.objects.create(
            conversation=self.conversation,
            role=Message.Role.USER,
            blocks=[{"type": "text", "text": "كم مبيعاتي؟"}],
            text="كم مبيعاتي؟",
        )
        self.assertFalse(message.is_tool_result)


class ChatAccessTests(TestCase):
    """The tenant gate. A conversation can read a shop's entire books."""

    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com", password="pw")
        self.tenant = Tenant.objects.create(name="متجري", slug="mine")
        Membership.objects.create(
            user=self.owner, tenant=self.tenant, role=Membership.Role.OWNER
        )

        self.stranger = User.objects.create_user(email="x@example.com", password="pw")
        self.other = Tenant.objects.create(name="متجر آخر", slug="theirs")
        Membership.objects.create(
            user=self.stranger, tenant=self.other, role=Membership.Role.OWNER
        )

    def test_owner_reaches_their_own_shop(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("assistant_chat", args=[self.tenant.slug]), follow=True, **REQUEST
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("المساعد الذكي", response.content.decode())

    def test_a_stranger_gets_404_not_403(self):
        """404, so the response does not confirm the shop exists."""
        self.client.force_login(self.stranger)
        response = self.client.get(
            reverse("assistant_chat", args=[self.tenant.slug]), **REQUEST
        )
        self.assertEqual(response.status_code, 404)

    def test_a_conversation_cannot_be_read_across_shops(self):
        self.client.force_login(self.owner)
        mine = Conversation.objects.create(tenant=self.tenant, user=self.owner)

        self.client.force_login(self.stranger)
        response = self.client.get(
            reverse("assistant_chat_detail", args=[self.tenant.slug, mine.pk]),
            **REQUEST,
        )
        self.assertEqual(response.status_code, 404)

    def test_a_staff_member_is_not_allowed_in(self):
        """Seeing that an agent is online is not the same capability as
        querying the books through it."""
        staff = User.objects.create_user(email="staff@example.com", password="pw")
        Membership.objects.create(
            user=staff, tenant=self.tenant, role=Membership.Role.STAFF
        )
        self.client.force_login(staff)
        response = self.client.get(
            reverse("assistant_chat", args=[self.tenant.slug]), **REQUEST
        )
        self.assertEqual(response.status_code, 404)

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(
            reverse("assistant_chat", args=[self.tenant.slug]), **REQUEST
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])


class AskTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com", password="pw")
        self.tenant = Tenant.objects.create(name="متجري", slug="mine")
        Membership.objects.create(
            user=self.owner, tenant=self.tenant, role=Membership.Role.OWNER
        )
        self.conversation = Conversation.objects.create(
            tenant=self.tenant, user=self.owner
        )
        self.client.force_login(self.owner)
        self.url = reverse(
            "assistant_ask", args=[self.tenant.slug, self.conversation.pk]
        )

        # Drive the turn inline instead of on a thread. Not a convenience:
        # TestCase wraps each test in a transaction that is never committed,
        # so a background thread on its own connection cannot see the
        # conversation this test just created, and would fail for a reason
        # that has nothing to do with what is being tested. This runs the same
        # _worker body the thread would.
        patcher = patch.object(
            claude, "start", side_effect=lambda run: claude._worker(run.pk)
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_a_question_with_no_agent_fails_with_an_actionable_message(self):
        """The shop has no approved agent, so there is nothing to query.

        The run has to land in `failed` with Arabic the owner can act on --
        a run stuck in `running` would spin the page's poller forever.
        """
        self.client.post(self.url, {"question": "كم مبيعاتي؟"}, **REQUEST)

        run = self.conversation.runs.first()
        self.assertIsNotNone(run)
        self.assertEqual(run.status, Run.Status.FAILED)
        self.assertIn("لا يوجد جهاز مفعّل", run.error)

    def test_the_question_is_recorded_even_when_the_run_fails(self):
        self.client.post(self.url, {"question": "كم مبيعاتي؟"}, **REQUEST)
        message = self.conversation.messages.first()
        self.assertEqual(message.role, Message.Role.USER)
        self.assertEqual(message.text, "كم مبيعاتي؟")
        self.assertEqual(message.blocks, [{"type": "text", "text": "كم مبيعاتي؟"}])

    def test_the_first_question_titles_the_conversation(self):
        self.client.post(self.url, {"question": "كم مبيعاتي؟"}, **REQUEST)
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.title, "كم مبيعاتي؟")

    def test_an_empty_question_does_nothing(self):
        self.client.post(self.url, {"question": "   "}, **REQUEST)
        self.assertEqual(self.conversation.messages.count(), 0)
        self.assertEqual(self.conversation.runs.count(), 0)

    def test_a_failed_run_is_shown_on_the_page(self):
        """Otherwise the page reloads, finds no active run, and says nothing."""
        self.client.post(self.url, {"question": "كم مبيعاتي؟"}, **REQUEST)
        html = self.client.get(
            reverse("assistant_chat_detail", args=[self.tenant.slug, self.conversation.pk]),
            **REQUEST,
        ).content.decode()
        self.assertIn("msg msg-error", html)
        self.assertIn("لا يوجد جهاز مفعّل", html)


class HeartbeatPacingTests(TestCase):
    """What makes the assistant usable: the agent comes back fast when asked."""

    def setUp(self):
        self.tenant = Tenant.objects.create(name="متجر", slug="shop")
        self.enrollment, _ = AgentEnrollment.issue(tenant=self.tenant)
        self.enrollment.approve()

    def test_an_idle_agent_is_told_to_take_its_time(self):
        self.assertEqual(_poll_after(self.enrollment), IDLE_POLL_SECONDS)

    def test_queued_work_pulls_the_agent_in(self):
        AgentCommand.objects.create(enrollment=self.enrollment, kind="ping")
        self.assertEqual(_poll_after(self.enrollment), BUSY_POLL_SECONDS)

    def test_a_sent_command_still_counts_as_outstanding(self):
        """SENT means the agent owes us a result -- that is exactly when the
        caller is blocked waiting, so it is not the moment to slow down."""
        command = AgentCommand.objects.create(enrollment=self.enrollment, kind="ping")
        command.mark_sent()
        command.save()
        self.assertEqual(_poll_after(self.enrollment), BUSY_POLL_SECONDS)

    def test_a_finished_command_does_not_keep_the_agent_hot(self):
        command = AgentCommand.objects.create(enrollment=self.enrollment, kind="ping")
        command.status = AgentCommand.Status.DONE
        command.save()
        self.assertEqual(_poll_after(self.enrollment), IDLE_POLL_SECONDS)

    def test_a_stopped_agent_is_never_hurried(self):
        self.enrollment.stop()
        AgentCommand.objects.create(enrollment=self.enrollment, kind="ping")
        self.assertEqual(_poll_after(self.enrollment), IDLE_POLL_SECONDS)
