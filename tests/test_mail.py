import shutil
import tempfile
import unittest
from email import message_from_bytes
from pathlib import Path
from unittest.mock import patch

from omni import tools
from omni.policy import ASK, ALLOW, Policy
from omni.tools import mail

MSMTP = """
defaults
auth on

account gmail
host smtp.gmail.com
from me@gmail.example
# a comment
account work
from me@work.example
passwordeval "ortie token show"

account broken
host nowhere

account default : gmail
"""


class MailParsingTest(unittest.TestCase):
    def test_accounts_come_from_msmtp(self):
        self.assertEqual(mail.parse_accounts(MSMTP), {"gmail": "me@gmail.example", "work": "me@work.example"})

    def test_thread_flattening_and_bodies(self):
        root = Path("/home/example/Mail")
        first = {"id": "a@x", "filename": ["/home/example/Mail/gmail/Inbox/cur/1:2,S"], "tags": ["inbox"],
                 "headers": {"From": "Ann <ann@x>", "To": "me@x", "Subject": "Lunch", "Date": "Mon"},
                 "body": [{"content-type": "multipart/alternative", "content": [
                     {"content-type": "text/plain", "content": "Tacos?\r\n"},
                     {"content-type": "text/html", "content": "<p>Tacos?</p>"}]}]}
        reply = {"id": "b@x", "filename": ["/home/example/Mail/gmail/Sent/cur/2:2,S"], "tags": ["unread"],
                 "headers": {"From": "me@x", "To": "ann@x", "Subject": "Re: Lunch", "Cc": "bob@x"},
                 "body": [{"content-type": "multipart/mixed", "content": [
                     {"content-type": "text/html", "content": "<div>Yes!</div><div>On Mon, Ann wrote:</div>"},
                     {"content-type": "application/pdf", "filename": "menu.pdf"}]}]}
        messages = mail.flatten([[first, [[reply, []]]]])
        self.assertEqual([m["id"] for m in messages], ["a@x", "b@x"])
        one = mail.summarize(first, root, trim=True)
        self.assertEqual((one["account"], one["folder"], one["body"], one["unread"]), ("gmail", "Inbox", "Tacos?", False))
        two = mail.summarize(reply, root, trim=True)
        self.assertEqual(two["attachments"], ["menu.pdf"])
        self.assertEqual(two["cc"], "bob@x")
        self.assertTrue(two["body"].startswith("Yes!"))
        self.assertIn("[quoted text trimmed]", two["body"])

    def test_trim_keeps_messages_that_are_only_quotes(self):
        self.assertEqual(mail.trim_quoted("On Mon, Ann wrote:\n> hi"), "On Mon, Ann wrote:\n> hi")
        self.assertEqual(mail.trim_quoted("Sure.\n\n________________________________\nFrom: Ann"),
                         "Sure.\n[quoted text trimmed]")

    def test_identifiers(self):
        self.assertEqual(mail.as_query("0000000000001a2b"), "thread:0000000000001a2b")
        self.assertEqual(mail.as_query("<abc@mail.example>"), "id:abc@mail.example")
        self.assertEqual(mail.as_query("thread:12"), "thread:12")


class MailWritingTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        for folder in ("tmp", "cur", "new"):
            (self.root / "gmail" / "Drafts" / folder).mkdir(parents=True)
        patches = [patch.object(mail, "accounts", return_value={"gmail": "me@gmail.example"}),
                   patch.object(mail, "mail_root", return_value=self.root),
                   patch.object(mail, "sync_in_background")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_compose_validates(self):
        with self.assertRaises(ValueError):
            mail.compose("yahoo", "a@b.example", "Hi", "text")
        with self.assertRaises(ValueError):
            mail.compose("gmail", "not-an-address", "Hi", "text")
        with self.assertRaises(ValueError):
            mail.compose("gmail", "", "Hi", "text")
        with self.assertRaises(ValueError):
            mail.compose("gmail", "a@b.example", "Hi", "   ")
        message = mail.compose("gmail", "Ann <ann@b.example>, bob@b.example", "", "Hello")
        self.assertEqual(message["To"], "ann@b.example, bob@b.example")
        self.assertEqual(message["From"], "me@gmail.example")
        self.assertEqual(message["Subject"], "(no subject)")
        self.assertTrue(message["Message-ID"].endswith("@gmail.example>"))

    def test_reply_threads_and_quotes(self):
        headers = {"To": "ann@b.example", "Subject": "Re: Lunch", "In-reply-to": "<a@x>", "References": "<a@x>"}
        with patch.object(mail, "reply_headers", return_value=(headers, "\n\nOn Mon, Ann wrote:\n> Tacos?")):
            message = mail.compose("gmail", "", "", "Yes!", reply_to="a@x")
        self.assertEqual((message["To"], message["Subject"], message["In-Reply-To"]), ("ann@b.example", "Re: Lunch", "<a@x>"))
        self.assertIn("> Tacos?", message.get_content())

    def test_draft_lands_in_the_drafts_maildir(self):
        result = tools.load_all()["mail_draft"].handler(tools.Context(), account="gmail", to="ann@b.example",
                                                         subject="Hi", body="Draft text")
        files = list((self.root / "gmail" / "Drafts" / "cur").iterdir())
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.endswith(":2,DS"))
        saved = message_from_bytes(files[0].read_bytes())
        self.assertEqual(saved["Message-ID"], result["draft"])
        self.assertEqual(list((self.root / "gmail" / "Drafts" / "tmp").iterdir()), [])
        with self.assertRaises(RuntimeError):
            mail.deliver("gmail", "Sent", saved, "S")


class MailApprovalTest(unittest.TestCase):
    def test_send_always_asks_with_the_whole_message(self):
        spec = tools.load_all()["mail_send"]
        self.assertTrue(spec.ask)
        body = "Line one.\n" + "x" * 300
        text = tools.approval_text(spec, {"account": "gmail", "to": "ann@b.example", "cc": "bob@b.example",
                                          "subject": "Lunch", "body": body})
        self.assertTrue(text.startswith("send an email from gmail to ann@b.example\n"))
        self.assertIn("Cc: bob@b.example", text)
        self.assertIn("Subject: Lunch", text)
        self.assertIn(body, text)
        long = tools.approval_text(spec, {"account": "gmail", "to": "a@b.example", "body": "y" * 5000})
        self.assertIn("more characters", long)
        self.assertFalse(tools.load_all()["mail_draft"].ask)

    def test_only_the_first_line_is_spoken(self):
        from omni.daemon import spoken_prompt
        text = tools.approval_text(tools.load_all()["mail_send"], {"account": "gmail", "to": "ann@b.example",
                                                                   "subject": "Lunch", "body": "A long body."})
        self.assertEqual(spoken_prompt("approval", text), "Should I send an email from gmail to ann@b.example?")
        self.assertEqual(spoken_prompt("approval", "run sudo (pacman -Syu)"), "Should I run sudo (pacman -Syu)?")
        self.assertEqual(spoken_prompt("question", "Which one?\nDetails"), "Which one?")

    def test_plain_ask_tools_keep_the_short_summary(self):
        spec = tools.Tool("thing", "a tool for testing", {}, lambda ctx: None, ask=True)
        self.assertEqual(tools.approval_text(spec, {"a": "b" * 200}), "thing: a=" + "b" * 80)

    def test_shell_sending_asks(self):
        policy = Policy(home=Path("/var/tmp"))
        for command in ("msmtp -a gmail -t < m.eml", "sendmail ann@b.example < m.eml",
                        "himalaya message send < m.eml", "himalaya -a gmail message delete 4"):
            self.assertEqual(policy.command(command).action, ASK, command)
        for command in ("himalaya envelope list --json", "notmuch search tag:unread", "mail-sync gmail"):
            self.assertEqual(policy.command(command).action, ALLOW, command)


if __name__ == "__main__":
    unittest.main()
