import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from omni import triage
from omni.triage import Decision, Mail, classify

UNSUB = "<https://example.com/unsubscribe>"


def make(sender="Alice <alice@example.com>", subject="Hello", body="Hi there", html=None, **headers) -> Mail:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "me@example.org"
    message["Subject"] = subject
    for key, value in headers.items():
        message[key.replace("_", "-")] = value
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")
    return triage.parse(bytes(message))


def category(mail, **kwargs) -> str:
    return classify(mail, **kwargs).category


class ParseTest(unittest.TestCase):
    def test_headers_snippet_and_encoded_subject(self):
        mail = make("=?utf-8?q?Caf=C3=A9_Bot?= <Bot@Example.com>", "=?utf-8?q?Caf=C3=A9_news?=", "Line one\n\nLine two",
                    List_Id="<news.example.com>")
        self.assertEqual(mail.sender, "bot@example.com")
        self.assertEqual(mail.name, "Café Bot")
        self.assertEqual(mail.subject, "Café news")
        self.assertEqual(mail.snippet, "Line one Line two")
        self.assertEqual(mail.headers["list-id"], "<news.example.com>")
        self.assertEqual(mail.domain, "example.com")

    def test_html_only_body(self):
        message = EmailMessage()
        message["From"] = "shop@example.com"
        message.set_content("<html><style>p{}</style><p>Your bill &amp; statement</p></html>", subtype="html")
        self.assertEqual(triage.parse(bytes(message)).snippet, "Your bill & statement")


class RulesTest(unittest.TestCase):
    def test_google_security_alert_is_urgent(self):
        mail = make("Google <no-reply@accounts.google.com>", "Security alert",
                    "A new sign-in on Linux. We noticed a new sign-in to your Google Account.")
        decision = classify(mail)
        self.assertEqual((decision.category, decision.rule), ("urgent", "security"))
        self.assertIn("security alert", decision.reason)

    def test_github_oauth_app_is_urgent(self):
        mail = make("GitHub <noreply@github.com>", "[GitHub] A third-party OAuth application has been added to your account",
                    "Hey there! A third-party OAuth application (Example) was recently authorized.", List_Unsubscribe=UNSUB)
        self.assertEqual(category(mail), "urgent")

    def test_security_newsletter_is_not_urgent(self):
        mail = make("Weekly <news@security.example.com>", "Security alert: five suspicious activity patterns",
                    "This week in security", List_Id="<weekly.security.example.com>", List_Unsubscribe=UNSUB)
        self.assertEqual(category(mail), "digest")
        promo = make("Shop <deals@example.com>", "New device deals: 30% off", "Big sale", List_Unsubscribe=UNSUB)
        self.assertEqual(category(promo), "digest")

    def test_sign_in_codes_notify_without_being_urgent(self):
        mail = make("Example <no-reply@example.com>", "Security alert: your one-time sign in code is 123456")
        self.assertEqual(classify(mail).rule, "code")
        self.assertEqual(category(mail), "today")
        self.assertEqual(category(make("App <no-reply@example.com>", "Reset your Example password")), "today")

    def test_correspondent_is_today(self):
        mail = make("Bob <bob@example.net>", "Lunch Friday?", "Are you free?")
        self.assertEqual(category(mail), "unsure")
        decision = classify(mail, correspondents={"bob@example.net"})
        self.assertEqual((decision.category, decision.rule), ("today", "correspondent"))

    def test_reply_in_nicks_thread_is_today(self):
        mail = make("Carol <carol@example.net>", "Re: Contract")
        mail.in_thread = True
        self.assertEqual(category(mail), "today")

    def test_correspondent_mailing_list_is_bulk(self):
        mail = make("Bob <bob@example.net>", "Monthly update", List_Id="<updates.example.net>", List_Unsubscribe=UNSUB)
        self.assertEqual(category(mail, correspondents={"bob@example.net"}), "digest")

    def test_job_ad_newsletter_is_digest(self):
        mail = make('"Indeed" <donotreply@match.example.com>', "Field Inspection Engineer @ Example Corp",
                    "Apply now. 1099 contractor role, jury of peers.", List_Unsubscribe=UNSUB)
        decision = classify(mail)
        self.assertEqual((decision.category, decision.rule), ("digest", "bulk"))

    def test_dmv_reminder_from_bulk_sender_is_rescued(self):
        mail = make("MVA <mva-no-reply@example.gov>", "It is time to renew your vehicle registration",
                    "Your registration expires on 10/31.",
                    Received="from o1.ptr1234.sendgrid.net (o1.ptr1234.sendgrid.net [192.0.2.1])")
        decision = classify(mail)
        self.assertEqual((decision.category, decision.rule), ("unsure", "rescue"))
        self.assertIn("may need action", decision.reason)

    def test_car_inspection_reminder_is_rescued(self):
        mail = make("Motor Vehicles <notice@example.gov>", "Reminder: vehicle inspection due by October 31",
                    List_Unsubscribe=UNSUB)
        self.assertEqual(classify(mail).rule, "rescue")

    def test_bill_in_snippet_is_rescued(self):
        mail = make("Bank <alerts@notify.example.com>", "Account notice", "Your payment is due on 10/18.",
                    List_Unsubscribe=UNSUB)
        self.assertEqual(classify(mail).rule, "rescue")

    def test_promotion_stays_in_digest(self):
        mail = make("Store <news@example.com>", "Sale ends Friday!", "Your coupon expires soon. Renew your look.",
                    List_Unsubscribe=UNSUB)
        self.assertEqual(category(mail), "digest")
        mail = make("Store <news@example.com>", "Your reward expires tonight", List_Unsubscribe=UNSUB)
        self.assertEqual(category(mail), "digest")

    def test_link_expiry_in_body_is_not_a_rescue(self):
        mail = make("App <no-reply@example.com>", "You're almost there!", "This link expires in 24 hours.")
        self.assertEqual(category(mail), "digest")

    def test_stranger_is_unsure_and_stranger_promo_is_digest(self):
        self.assertEqual(classify(make("Dan <dan@example.org>", "Question about your car")).rule, "stranger")
        self.assertEqual(category(make("Dan <dan@example.org>", "Save 50% on dental care")), "digest")

    def test_own_mail_is_digest(self):
        mail = make("Me <me@example.org>", "Note to self")
        self.assertEqual(classify(mail, mine={"me@example.org"}).rule, "self")

    def test_bulk_signals(self):
        self.assertEqual(triage.bulk_signal(make(List_Unsubscribe=UNSUB)), "has an unsubscribe header")
        self.assertEqual(triage.bulk_signal(make(Precedence="bulk")), "marked bulk")
        self.assertEqual(triage.bulk_signal(make(Auto_Submitted="auto-generated")), "automated")
        self.assertEqual(triage.bulk_signal(make("x <no_reply@example.com>")), "sent from a no-reply address")
        self.assertIn("mailgun", triage.bulk_signal(make(Return_Path="<bounce@mg.example.com>",
                                                         Received="from m1.mailgun.net")))
        self.assertEqual(triage.bulk_signal(make("Alice <alice@example.com>")), "")


class CorrectionsTest(unittest.TestCase):
    security = dict(sender="Google <no-reply@accounts.google.com>", subject="Security alert")

    def test_correction_beats_everything(self):
        rules = {"accounts.google.com": "digest"}
        self.assertEqual(category(make(**self.security), rules=rules), "digest")
        bob = make("Bob <bob@example.net>", "Hi")
        self.assertEqual(category(bob, rules={"bob@example.net": "urgent"}, correspondents={"bob@example.net"}), "urgent")

    def test_important_is_at_least_today(self):
        news = make("News <news@example.com>", "Weekly roundup", List_Unsubscribe=UNSUB)
        decision = classify(news, rules={"example.com": "important"})
        self.assertEqual((decision.category, decision.rule), ("today", "correction"))
        self.assertEqual(category(make(**self.security), rules={"google.com": "important"}), "urgent")

    def test_ignore(self):
        self.assertEqual(category(make("Bob <bob@example.net>"), rules={"bob@example.net": "ignore"}), "ignore")

    def test_domain_rules_match_subdomains_only(self):
        rules = {"example.com": "ignore"}
        self.assertEqual(category(make("x <a@mail.example.com>"), rules=rules), "ignore")
        self.assertEqual(category(make("x <a@example.com>"), rules=rules), "ignore")
        self.assertNotEqual(category(make("x <a@notexample.com>"), rules=rules), "ignore")

    def test_normalize_sender(self):
        self.assertEqual(triage.normalize_sender("Alice <Alice@Example.COM>"), "alice@example.com")
        self.assertEqual(triage.normalize_sender("@Example.com"), "example.com")
        for bad in ("bob", "bob@", "not a domain"):
            with self.assertRaises(ValueError):
                triage.normalize_sender(bad)


class KeywordTest(unittest.TestCase):
    RESCUE_SAMPLES = {
        "a due date": "Your balance is past due", "an expiry": "Your card expires next month",
        "a renewal": "Policy renewal", "a payment problem": "Payment failed for your account",
        "a bill": "Your bill is ready", "a statement": "Your October statement",
        "a registration": "Vehicle registration", "an inspection": "Inspection reminder",
        "an appointment": "Upcoming appointment", "a reservation": "Your reservation at Example",
        "a confirmation": "Please confirm your visit", "a delivery problem": "Delivery exception for your parcel",
        "jury duty": "Jury summons", "a court notice": "Court date scheduled", "taxes": "IRS notice",
        "the DMV": "DMV reminder", "a recall": "Safety recall for your vehicle",
        "a policy": "Your policy documents", "a claim": "Your claim number",
        "a usage limit": "You've hit 100% of your budget", "a booking": "Thanks for booking",
        "a cancellation": "Your order was cancelled", "a deadline": "Action required: verify details",
        "a credit alert": "You received a credit alert",
    }

    def test_every_rescue_pattern_has_a_working_example(self):
        self.assertEqual(set(self.RESCUE_SAMPLES), set(triage.RESCUE))
        for name, text in self.RESCUE_SAMPLES.items():
            self.assertTrue(triage.RESCUE_RE[name].search(text), name)
        self.assertLessEqual(triage.SUBJECT_ONLY, set(triage.RESCUE))

    def test_rescue_does_not_fire_on_ordinary_words(self):
        for text in ("Due to high demand, shipping is slow", "No action required: new terms",
                     "Basketball court tickets", "Privacy policy link below"):
            hits = [name for name, pattern in triage.RESCUE_RE.items() if pattern.search(text)]
            self.assertEqual(hits, [], text)

    def test_security_phrases(self):
        for text in ("New sign-in to your account", "Your password was changed", "Verify it's you",
                     "2-Step Verification turned off", "A personal access token has been added",
                     "Your account has been locked", "Recovery email changed", "Your card was added to Apple Pay"):
            self.assertTrue(triage.SECURITY_RE.search(text), text)
        for text in ("Sign in to see your offers", "Our new app is here"):
            self.assertIsNone(triage.SECURITY_RE.search(text), text)

    def test_promo_words(self):
        for text in ("Sale ends Friday!", "20% off everything", "Deals of the week", "Free shipping today"):
            self.assertTrue(triage.PROMO_RE.search(text), text)
        self.assertIsNone(triage.PROMO_RE.search("Your free trial ends soon"))


def item(category, subject="Subject", who="Sender", unread=True, rule=""):
    mail = Mail(sender=f"{who.lower()}@example.com", name=who, subject=subject, id=f"{subject}@example.com", unread=unread)
    return mail, Decision(category, f"reason for {category}", rule or category)


class NotificationTest(unittest.TestCase):
    def test_single_urgent_is_critical_with_reason(self):
        [(urgency, title, body)] = triage.notifications([item("urgent", "Security alert", rule="security")])
        self.assertEqual(urgency, "critical")
        self.assertIn("Security alert", title)
        self.assertIn("reason for urgent", body)
        self.assertIn("not through links", body)

    def test_batches_today_and_unsure_into_one(self):
        batch = [item("today", f"Note {n}") for n in range(4)] + [item("unsure", "Bill", rule="rescue")]
        [(urgency, title, body)] = triage.notifications(batch)
        self.assertEqual((urgency, title), ("normal", "5 new emails"))
        self.assertEqual(body.count("\n"), 3)
        self.assertIn("and 2 more", body)

    def test_read_digest_and_ignore_stay_quiet(self):
        quiet = [item("today", unread=False), item("digest"), item("ignore")]
        self.assertEqual(triage.notifications(quiet), [])

    def test_urgent_and_rest_are_separate(self):
        sent = triage.notifications([item("urgent"), item("urgent", "Other"), item("today")])
        self.assertEqual([u for u, _, _ in sent], ["critical", "normal"])
        self.assertEqual(sent[0][1], "2 urgent emails")
        self.assertEqual(sent[1][1], "New mail")

    def test_flood_is_one_quiet_summary(self):
        sent = triage.notifications([item("today", f"n{n}") for n in range(30)])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "low")
        self.assertIn("30 new emails", sent[0][1])


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = triage.Store(Path(self.folder.name) / "mail.sqlite")

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def run_intake(self, mails, quiet=False):
        sent, tagged = [], []
        with patch.object(triage, "load", return_value=mails), \
                patch.object(triage, "mine", return_value=frozenset({"me@example.org"})), \
                patch.object(triage, "correspondents", return_value=frozenset({"bob@example.net"})), \
                patch.object(triage, "tag", side_effect=tagged.extend), \
                patch.object(triage, "notify_send", side_effect=lambda *a: sent.append(a)):
            result = triage.intake([m.id for m in mails], quiet=quiet, store=self.store)
        return result, sent, tagged

    def mails(self):
        google = make("Google <no-reply@accounts.google.com>", "Security alert", "New sign-in")
        bob = make("Bob <bob@example.net>", "Lunch?")
        news = make("News <news@example.com>", "Weekly", List_Unsubscribe=UNSUB)
        for n, mail in enumerate((google, bob, news)):
            mail.id = f"m{n}@example.com"
        return google, bob, news

    def test_tags_records_and_notifies_in_batches(self):
        result, sent, tagged = self.run_intake(list(self.mails()))
        self.assertEqual(result["counts"], {"urgent": 1, "today": 1, "digest": 1})
        self.assertEqual([d.category for _, d in tagged], ["urgent", "today", "digest"])
        self.assertEqual([s[0] for s in sent], ["critical", "normal"])
        self.assertIn("Bob — Lunch?", sent[1][2])
        self.assertEqual(self.store.reasons(["m1@example.com"]), {"m1@example.com": "someone you have written to"})

    def test_quiet_still_tags(self):
        result, sent, tagged = self.run_intake(list(self.mails()), quiet=True)
        self.assertEqual((len(tagged), sent, result["notified"]), (3, [], 0))

    def test_rules_from_the_store_apply(self):
        self.store.set_rule("example.net", "ignore", "too chatty")
        _, sent, tagged = self.run_intake(list(self.mails()))
        self.assertEqual(tagged[1][1].category, "ignore")
        self.assertEqual([s[0] for s in sent], ["critical"])
        self.assertEqual(self.store.list_rules()[0]["note"], "too chatty")
        self.store.set_rule("example.net", "clear")
        self.assertEqual(self.store.rules(), {})

    def test_set_rule_validates(self):
        with self.assertRaises(ValueError):
            triage.set_rule("example.com", "loud", store=self.store)
        self.assertEqual(triage.set_rule("Alice <Alice@Example.com>", "important", store=self.store), "alice@example.com")

    def test_tag_batch_quotes_ids(self):
        mail = Mail(id='odd"id@example.com')
        with patch.object(triage, "notmuch") as run:
            triage.tag([(mail, Decision("digest", "r", "bulk"))])
        batch = run.call_args.kwargs["input"]
        self.assertIn('+triage/digest -- id:"odd""id@example.com"', batch)
        self.assertIn("-triage/urgent", batch)


class DigestTest(unittest.TestCase):
    def test_text_lists_look_items_then_groups_bulk(self):
        items = [{"category": "digest", "from": "Jobs", "address": "j@example.com", "subject": f"Job {n}",
                  "reason": "bulk", "unread": True} for n in range(3)]
        items += [{"category": "unsure", "from": "DMV", "address": "d@example.gov", "subject": "Renew registration",
                   "reason": "bulk mail that may need action: mentions a renewal", "unread": True},
                  {"category": "urgent", "from": "Google", "address": "g@example.com", "subject": "Security alert",
                   "reason": "security alert (new sign-in)", "unread": False}]
        items.append(dict(items[-1], subject="Other alert", unread=True))
        title, text = triage.digest_text(items, "12h")
        self.assertEqual(title, "Mail since 12h: 2 to look at, 3 in the digest")
        lines = text.splitlines()
        self.assertEqual(lines[0], "To look at")
        self.assertTrue(lines[1].startswith("- urgent: Google — Other alert (security alert"))
        self.assertIn("mentions a renewal", lines[2])
        self.assertEqual(lines[3], "- and 1 you have already read")
        self.assertIn("- Jobs (3): Job 0 · Job 1", text)

    def test_since(self):
        self.assertEqual(triage.since_seconds("12h"), 43200)
        self.assertEqual(triage.since_seconds("2d"), 172800)
        with self.assertRaises(ValueError):
            triage.since_seconds("yesterday")


if __name__ == "__main__":
    unittest.main()
