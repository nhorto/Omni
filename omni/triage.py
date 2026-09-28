"""Email triage, step 1: rules only.

The notmuch post-new hook hands new inbox message ids to omnid (`mail.intake`), or runs
`python3 -m omni.triage intake` when omnid is down. Each message gets one category, a
notmuch tag `triage/<category>`, and a reason kept in `mail.sqlite`:

  urgent   security and account alerts, or Nick's own `urgent` rule
  today    people Nick has written to, or senders he marked important
  unsure   the rules cannot tell (a stranger, or bulk mail that mentions a bill, a
           deadline, or an appointment); treated like today until the AI sort (step 2)
  digest   bulk mail; listed in the morning and evening digest, never notified
  ignore   Nick's `ignore` rule; not even in the digest

The first rule that matches wins: Nick's per-sender corrections, his own mail, sign-in
codes, security alerts, correspondents, bulk (with the rescue pass), then everything else
is unsure. docs/email.md has the details.
Mail text is untrusted: nothing here acts on it; it only sorts and notifies.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from pathlib import Path

from . import config

CATEGORIES = ("urgent", "today", "unsure", "digest", "ignore")
LOOK = ("urgent", "today", "unsure")  # the ones that notify
RULES = ("important", "urgent", "digest", "ignore")
READ_LIMIT = 256 * 1024
SNIPPET = 600
BATCH_LIMIT = 25  # more than this in one intake gives one quiet summary
BACKFILL_DAYS = 14

# Sign-in codes, magic links, and password-reset links: Nick is usually waiting for one, so
# they notify as `today` rather than `urgent`.
CODES = [
    r"(verification|sign[- ]?in|log[- ]?in|security|one[- ]time|confirmation|access) code", r"\bpasscode\b", r"\botp\b",
    r"(secure|magic|sign[- ]?in|log[- ]?in) link", r"reset your .{0,20}password", r"password reset",
]

# Security and account alerts. Phishing looks the same, so these notify but nothing acts on them.
SECURITY = [
    r"new sign[- ]?in", r"sign[- ]?in (attempt|from a new|on a new|detected)", r"new (log[- ]?in|device)",
    r"log[- ]?in (attempt|from a new|detected)", r"security (alert|notice|warning|notification)",
    r"(unusual|suspicious) (sign[- ]?in|log[- ]?in|activity|attempt|transaction)",
    r"password (was |has been )?(changed|reset|updated)",
    r"(verify|confirm) it'?s (really )?you",
    r"(2-step|two-step|two-factor|2fa|multi-factor|mfa)\b.{0,30}\b(turned off|turned on|disabled|enabled|changed|removed|added|updated|configured|set up|registered)",
    r"(oauth|third-party) (app|application)",
    r"(app|application|personal access token|access token|api key|ssh key|deploy key|passkey|security key)s? "
    r"(was |were |has been |have been )?(added|authorized|created|granted)",
    r"account (is |has been |was )?(locked|suspended|disabled|compromised|restricted|deactivated)",
    r"recovery (email|phone|number)", r"someone (has |may have )?(accessed|tried|signed|logged)",
    r"(credit |debit )?card (was |has been )?added to",
]

# Bulk mail that mentions one of these is rescued to `unsure` instead of the digest.
RESCUE = {
    "a due date": r"past due|overdue|(is|are|was|now|payment|bill|balance|amount|renewal|fee|taxes|rent) due\b"
                  r"|\bdue (on|by|date|soon|today|tomorrow|in \d+)",
    "an expiry": r"expir(e|es|ed|ing|ation|y)\b",
    "a renewal": r"renew(al|als|s|ed)?\b",
    "a payment problem": r"payment (failed|declined|unsuccessful|was unsuccessful|reminder|is due|past due)"
                         r"|(card|payment) (was |has been )?declined|autopay",
    "a bill": r"(bill|invoice) (is )?(ready|available|due)|your (bill|invoice)\b",
    "a statement": r"statements?\b",
    "a registration": r"registration\b",
    "an inspection": r"inspection",
    "an appointment": r"appointment",
    "a reservation": r"reservation",
    "a confirmation": r"confirm your",
    "a delivery problem": r"delivery (exception|attempt|failed|problem)|(missed|failed) delivery",
    "jury duty": r"jury\b",
    "a court notice": r"court (date|notice|hearing|appearance)|summons",
    "taxes": r"\birs\b|tax (return|refund|bill|payment|notice|form|document)s?\b|\b1099\b|\bw-2\b",
    "the DMV": r"\bdmv\b|\bmva\b|motor vehicles?\b",
    "a recall": r"recall\b",
    "a policy": r"policy (renewal|change|number|document|cancel\w*|lapse\w*|expir\w*)|your (insurance )?policy\b",
    "a claim": r"(your|insurance) claim\b|claim (number|status|update|#)",
    "a usage limit": r"(hit|reached|used|exceeded) (\d+% of )?(your )?(budget|quota|limit|spending limit)"
                     r"|\d+% of (the |your )?[\w ]{0,20}(budget|quota|minutes|storage)|storage is full|(is|now) \d+% full",
    "a booking": r"thanks for booking|booking (confirm\w*|is confirmed)|your (trip|flight) (details|itinerary)"
                 r"|itinerary|boarding pass|check in for your",
    "a cancellation": r"(has been|was|is) cancel+ed|cancellation",
    "a deadline": r"deadline|final notice|(?<!no )action (is )?required|respond by|\bcobra\b",
    "a credit alert": r"(received|new) (a )?credit alerts?|alerts? on your .{0,30}credit",
}
# Only the subject counts for these: "this link expires in 24 hours" is in half of all automated mail.
SUBJECT_ONLY = {"an expiry", "a confirmation", "a renewal", "a registration", "a statement"}
# Promotions mention expiry and renewals too; these words keep them in the digest.
PROMO = (r"\bsale\b|\bdeals?\b|\bcoupons?\b|\bshop(ping)?\b|\bdiscount|\bpromo\b|\bbogo\b|\bclearance\b"
         r"|free shipping|limited[- ]time|\bsave\b|\d+ ?% off|\boffers?\b|\bexclusive\b|\bgift\b|\bpoints\b"
         r"|\bmiles\b|\breward|\bperks?\b|\bwin\b|\bsweepstakes\b|\bticket|\bpresale\b|\bfree\b(?! trial)"
         r"|running out|last chance|\bhurry\b|don'?t miss"
         r"|\bstory\b|\bhow was\b|\bwebinar\b")
# Job ads are the biggest pile of bulk mail and name every industry's words (inspection, 1099, DMV).
JOBS = r"\bjobs?\b|\bhiring\b|\bcareers?\b|recruit|\bopportunit(y|ies)\b| @ |\bposition\b"
JOB_SENDERS = r"job|career|indeed|ziprecruiter|glassdoor|dice\.com|talent|recruit|hiring"
# Sending services behind newsletters and marketing, seen in Received, Return-Path, or DKIM.
ESP = (r"sendgrid|mailchimp|mcsv\.net|mcdlv\.net|mandrillapp|amazonses|sparkpost|mailgun|exacttarget|salesforce"
       r"|hubspot|constantcontact|klaviyo|braze|iterable|sailthru|responsys|marketo|mktomail|mailjet|sendinblue"
       r"|brevo|customer\.io|cmail\d*\.com|createsend|emarsys|bluehornet|epsilon|cheetahmail|rsgsv|beehiiv"
       r"|substack|convertkit|campaign-archive|listrak|bronto")
NOREPLY = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|^(mailer-daemon|bounces?|notifications?|alerts?|"
                     r"news(letter)?s?|marketing|info|updates?|hello|team|promo)([-+_.]|$)")

CODES_RE = re.compile("|".join(CODES), re.IGNORECASE)
SECURITY_RE = re.compile("|".join(SECURITY), re.IGNORECASE)
RESCUE_RE = {name: re.compile(r"\b(" + pattern + ")", re.IGNORECASE) for name, pattern in RESCUE.items()}
PROMO_RE = re.compile(PROMO, re.IGNORECASE)
JOBS_RE = re.compile(JOBS, re.IGNORECASE)
JOB_SENDERS_RE = re.compile(JOB_SENDERS, re.IGNORECASE)
ESP_RE = re.compile(ESP, re.IGNORECASE)


@dataclass
class Mail:
    sender: str = ""
    name: str = ""
    subject: str = ""
    headers: dict = field(default_factory=dict)  # lowercase name -> value, repeats joined by newlines
    snippet: str = ""
    id: str = ""
    account: str = ""
    unread: bool = True
    date: int = 0
    in_thread: bool = False  # a thread Nick has written in

    @property
    def domain(self) -> str:
        return self.sender.rpartition("@")[2]

    @property
    def who(self) -> str:
        return self.name or self.sender or "(unknown sender)"


@dataclass
class Decision:
    category: str
    reason: str
    rule: str  # which step decided: correction, self, code, security, correspondent, rescue, bulk, stranger


# ---- parsing -------------------------------------------------------------------------------

def decoded(value) -> str:
    if value is None:
        return ""
    try:
        return " ".join(str(make_header(decode_header(str(value)))).split())
    except (LookupError, UnicodeError, ValueError, TypeError):
        return " ".join(str(value).split())


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(style|script|head)\b.*?</\1>|<!--.*?-->", " ", text)
    return html.unescape(re.sub(r"<[^>]+>", " ", text))


def snippet_of(message) -> str:
    plain = rich = ""
    for part in message.walk():
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except (LookupError, AssertionError):
            continue
        if part.get_content_subtype() == "plain" and not plain:
            plain = text
        elif part.get_content_subtype() == "html" and not rich:
            rich = strip_html(text)
    if re.search(r"</(p|div|td|a|span)>", plain, re.IGNORECASE):  # HTML sent as text/plain
        plain = strip_html(plain)
    return " ".join((plain or rich).split())[:SNIPPET]


def parse(data: bytes) -> Mail:
    """Headers and a short body snippet from a raw message (possibly cut off after READ_LIMIT bytes)."""
    message = email.message_from_bytes(data, policy=email.policy.compat32)
    headers: dict[str, str] = {}
    for key, value in message.items():
        key = key.lower()
        headers[key] = f"{headers[key]}\n{value}" if key in headers else str(value)
    name, address = parseaddr(decoded(message.get("From")))
    return Mail(sender=address.lower(), name=name.strip(), subject=decoded(message.get("Subject")),
                headers=headers, snippet=snippet_of(message))


# ---- rules ---------------------------------------------------------------------------------

def rule_for(mail: Mail, rules: dict[str, str]) -> tuple[str, str] | None:
    """Nick's correction for the sender's address, else its domain or a parent domain."""
    if mail.sender in rules:
        return mail.sender, rules[mail.sender]
    parts = mail.domain.split(".")
    for i in range(len(parts) - 1):
        domain = ".".join(parts[i:])
        if domain in rules:
            return domain, rules[domain]
    return None


def is_list(mail: Mail) -> bool:
    """Newsletter or mailing list, as opposed to one automated message about Nick's account."""
    return bool(mail.headers.get("list-id")) or \
        mail.headers.get("precedence", "").strip().lower() in ("bulk", "list", "junk")


def bulk_signal(mail: Mail) -> str:
    """Why this looks like bulk mail, or '' if it does not."""
    h = mail.headers
    if h.get("list-unsubscribe"):
        return "has an unsubscribe header"
    if h.get("list-id"):
        return "mailing list"
    if h.get("precedence", "").strip().lower() in ("bulk", "list", "junk"):
        return "marked bulk"
    if h.get("auto-submitted", "no").strip().lower() != "no":
        return "automated"
    if NOREPLY.search(mail.sender.partition("@")[0]):
        return "sent from a no-reply address"
    trail = " ".join(h.get(k, "") for k in ("received", "return-path", "dkim-signature", "x-mailer", "feedback-id"))
    trail += " " + " ".join(k for k in h if k.startswith("x-"))
    if found := ESP_RE.search(trail):
        return f"sent through {found.group(0).lower()}"
    return ""


def security_match(mail: Mail) -> str:
    """The security phrase in the subject, or in the snippet of mail without an unsubscribe link."""
    if is_list(mail) or PROMO_RE.search(mail.subject):
        return ""
    found = SECURITY_RE.search(mail.subject)
    if not found and not mail.headers.get("list-unsubscribe"):
        found = SECURITY_RE.search(mail.snippet[:300])
    return found.group(0).lower() if found else ""


def rescue_match(mail: Mail) -> str:
    """What deadline, money, or appointment bulk mail mentions, unless it reads as a promotion or a job ad."""
    if PROMO_RE.search(mail.subject) or JOBS_RE.search(mail.subject) or JOB_SENDERS_RE.search(mail.sender):
        return ""
    for name, pattern in RESCUE_RE.items():
        if pattern.search(mail.subject):
            return name
    if PROMO_RE.search(mail.snippet):
        return ""
    for name, pattern in RESCUE_RE.items():
        if name not in SUBJECT_ONLY and pattern.search(mail.snippet):
            return name
    return ""


def classify(mail: Mail, rules: dict[str, str] | None = None, correspondents: frozenset | set = frozenset(),
             mine: frozenset | set = frozenset()) -> Decision:
    rules = rules or {}
    if hit := rule_for(mail, rules):
        key, rule = hit
        if rule == "ignore":
            return Decision("ignore", f"you asked to ignore {key}", "correction")
        if rule == "digest":
            return Decision("digest", f"you asked for {key} in the digest", "correction")
        if rule == "urgent":
            return Decision("urgent", f"you marked {key} urgent", "correction")
        rest = classify(mail, {}, correspondents, mine)
        if rest.category == "urgent":
            return rest
        return Decision("today", f"you asked to hear about {key}", "correction")
    if mail.sender in mine:
        return Decision("digest", "sent by you", "self")
    if CODES_RE.search(mail.subject):
        return Decision("today", "a sign-in code or link; if you did not ask for it, check the account", "code")
    if phrase := security_match(mail):
        return Decision("urgent", f"security alert ({phrase})", "security")
    if not is_list(mail):
        if mail.sender in correspondents:
            return Decision("today", "someone you have written to", "correspondent")
        if mail.in_thread:
            return Decision("today", "a reply in a thread you are in", "correspondent")
    if bulk := bulk_signal(mail):
        if what := rescue_match(mail):
            return Decision("unsure", f"bulk mail that may need action: mentions {what}", "rescue")
        return Decision("digest", f"bulk mail ({bulk})", "bulk")
    if PROMO_RE.search(mail.subject):
        return Decision("digest", "looks like a promotion", "bulk")
    return Decision("unsure", "a sender you have not written to", "stranger")


# ---- notmuch -------------------------------------------------------------------------------

def notmuch(*args: str, input: str | None = None) -> str:
    try:
        done = subprocess.run(["notmuch", *args], input=input, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError("notmuch is not installed, so there is no local mail index") from None
    if done.returncode:
        raise RuntimeError(done.stderr.strip() or f"notmuch {args[0]} failed")
    return done.stdout


def flatten(nodes: list) -> list[dict]:
    out = []
    for node in nodes:
        message, replies = node
        if message:
            out.append(message)
        out.extend(flatten(replies))
    return out


def quote_id(ident: str) -> str:
    ident = ident.strip().removeprefix("id:").strip("<>")
    return 'id:"' + ident.replace('"', '""') + '"'


def mine() -> frozenset:
    addresses = set()
    for key in ("user.primary_email", "user.other_email"):
        try:
            value = notmuch("config", "get", key)
        except RuntimeError:
            continue
        addresses.update(a.strip().lower() for a in re.split(r"[;\n]", value) if a.strip())
    return frozenset(addresses)


_known: dict = {"key": None, "addresses": frozenset()}
_known_lock = threading.Lock()


def correspondents(own: frozenset) -> frozenset:
    """Everyone Nick has sent mail to (To, Cc, Bcc), rebuilt only when his sent mail changes."""
    query = " or ".join(["tag:sent", *(f"from:{a}" for a in sorted(own))])
    with _known_lock:
        key = (query, notmuch("count", "--exclude=false", query).strip())
        if key != _known["key"]:
            found = json.loads(notmuch("address", "--format=json", "--output=recipients", "--deduplicate=address",
                                       "--exclude=false", query) or "[]")
            _known["addresses"] = frozenset(a["address"].lower() for a in found if a.get("address")) - own
            _known["key"] = key
        return _known["addresses"]


def mail_root() -> Path:
    return Path(notmuch("config", "get", "database.path").strip())


def inboxes(root: Path | None = None) -> list[str]:
    root = root or mail_root()
    return sorted(p.parent.name for p in root.glob("*/Inbox") if p.is_dir())


def inbox_query(root: Path | None = None) -> str:
    names = inboxes(root)
    if not names:
        raise RuntimeError("No account has a synced Inbox folder yet (see docs/email.md)")
    return "(" + " or ".join(f"folder:{name}/Inbox" for name in names) + ")"


def load(ids: list[str], own: frozenset = frozenset()) -> list[Mail]:
    """Parse each message's file for headers and a snippet; two notmuch calls per 100 ids."""
    root = mail_root()
    nick = "tag:sent" + "".join(f" or from:{a}" for a in sorted(own))
    out = []
    for start in range(0, len(ids), 100):
        query = " or ".join(quote_id(i) for i in ids[start:start + 100])
        threaded = {i.removeprefix("id:") for i in notmuch(
            "search", "--output=messages", "--exclude=false", f'({query}) and thread:"{{{nick}}}"').split()}
        shown = json.loads(notmuch("show", "--format=json", "--body=false", "--entire-thread=false",
                                   "--exclude=false", query) or "[]")
        for found in (m for thread in shown for m in flatten(thread) if m.get("match", True)):
            files = found.get("filename") or []
            path = next((f for f in files if "/Inbox/" in f), files[0] if files else "")
            try:
                with open(path, "rb") as handle:
                    mail = parse(handle.read(READ_LIMIT))
            except OSError:
                headers = found.get("headers", {})
                name, address = parseaddr(headers.get("From", ""))
                mail = Mail(sender=address.lower(), name=name, subject=headers.get("Subject", ""))
            try:
                mail.account = Path(path).relative_to(root).parts[0]
            except (ValueError, IndexError):
                pass
            mail.id = found["id"]
            mail.unread = "unread" in found.get("tags", [])
            mail.date = int(found.get("timestamp") or 0)
            mail.in_thread = mail.id in threaded
            out.append(mail)
    return out


def tag(decided: list[tuple[Mail, Decision]]) -> None:
    clear = " ".join(f"-triage/{c}" for c in CATEGORIES)
    batch = "".join(f"{clear} +triage/{d.category} -- {quote_id(m.id)}\n" for m, d in decided if m.id)
    if batch:
        notmuch("tag", "--batch", input=batch)


# ---- the sender table and reasons ----------------------------------------------------------

class Store:
    """mail.sqlite: Nick's per-sender rules, the reason behind each triage tag, and small state."""

    def __init__(self, path: Path | None = None):
        path = path or config.DATA / "mail.sqlite"
        config.private_dir(path.parent)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS rules (key TEXT PRIMARY KEY, rule TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                                              created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, category TEXT NOT NULL, reason TEXT NOT NULL,
                                                  rule TEXT NOT NULL, decided REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        path.chmod(0o600)

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def rules(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT key, rule FROM rules"))

    def list_rules(self) -> list[dict]:
        rows = self.db.execute("SELECT key, rule, note, updated FROM rules ORDER BY key")
        return [{"sender": k, "rule": r, "note": n, "updated": time.strftime("%Y-%m-%d", time.localtime(u))}
                for k, r, n, u in rows]

    def set_rule(self, key: str, rule: str, note: str = "") -> None:
        now = time.time()
        with self.db:
            if rule == "clear":
                self.db.execute("DELETE FROM rules WHERE key = ?", (key,))
            else:
                self.db.execute("INSERT INTO rules VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                                "rule = excluded.rule, note = excluded.note, updated = excluded.updated",
                                (key, rule, note, now, now))

    def record(self, decided: list[tuple[Mail, Decision]]) -> None:
        now = time.time()
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO decisions VALUES (?, ?, ?, ?, ?)",
                                [(m.id, d.category, d.reason, d.rule, now) for m, d in decided if m.id])

    def reasons(self, ids: list[str]) -> dict[str, str]:
        out = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            out.update(self.db.execute(f"SELECT id, reason FROM decisions WHERE id IN ({marks})", chunk))
        return out

    def get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def put(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))


def normalize_sender(sender: str) -> str:
    """An address or a domain, as stored in the rules table."""
    _, address = parseaddr(sender.strip())
    key = (address or sender).strip().strip("<>").lower().lstrip("@")
    if "@" in key:
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", key):
            raise ValueError(f"{sender!r} is not a complete email address")
    elif not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", key):
        raise ValueError(f"{sender!r} is neither an email address nor a domain like example.com")
    return key


def set_rule(sender: str, rule: str, note: str = "", store: Store | None = None) -> str:
    if rule not in (*RULES, "clear"):
        raise ValueError(f"rule must be one of: {', '.join(RULES)}, or clear")
    key = normalize_sender(sender)
    own = store or Store()
    try:
        own.set_rule(key, rule, note)
    finally:
        if store is None:
            own.close()
    return key


# ---- intake and notifications --------------------------------------------------------------

def short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def line(mail: Mail, decision: Decision) -> str:
    text = f"{short(mail.who, 30)} — {short(mail.subject or '(no subject)', 60)}"
    return text + (" (may need action)" if decision.rule == "rescue" else "")


def notifications(decided: list[tuple[Mail, Decision]]) -> list[tuple[str, str, str]]:
    """(urgency, title, body) for one intake. Only unread urgent, today, and unsure mail notifies."""
    loud = [(m, d) for m, d in decided if m.unread and d.category in LOOK]
    urgent = [(m, d) for m, d in loud if d.category == "urgent"]
    rest = [(m, d) for m, d in loud if d.category != "urgent"]
    if len(loud) > BATCH_LIMIT:
        return [("low", f"{len(loud)} new emails",
                 f"{len(urgent)} urgent, {len(rest)} to look at. `omni mail digest --since 1d` lists them.")]
    out = []
    if len(urgent) == 1:
        mail, decision = urgent[0]
        body = f"From {mail.who}\n{decision.reason}"
        if decision.rule == "security":
            body += "\nIf it was not you, check the account directly, not through links in the email."
        out.append(("critical", f"Urgent email: {short(mail.subject or '(no subject)', 70)}", body))
    elif urgent:
        body = [f"{line(m, d)}\n  {d.reason}" for m, d in urgent[:3]]
        if len(urgent) > 3:
            body.append(f"and {len(urgent) - 3} more")
        out.append(("critical", f"{len(urgent)} urgent emails", "\n".join(body)))
    if rest:
        body = [line(m, d) for m, d in rest[:3]]
        if len(rest) > 3:
            body.append(f"and {len(rest) - 3} more")
        out.append(("normal", "New mail" if len(rest) == 1 else f"{len(rest)} new emails", "\n".join(body)))
    return out


def notify_send(urgency: str, title: str, body: str) -> None:
    if not shutil.which("notify-send"):
        return
    icon = "dialog-warning" if urgency == "critical" else "mail-unread"
    subprocess.run(["notify-send", "--app-name=Mail", f"--urgency={urgency}", f"--icon={icon}", title, body],
                   capture_output=True, timeout=10)


def intake(ids: list[str], quiet: bool = False, store: Store | None = None) -> dict:
    """Sort new messages, tag them, record the reasons, and notify (unless quiet)."""
    ids = [i.strip().removeprefix("id:") for i in ids if i.strip()]
    if not ids:
        return {"counts": {}, "notified": 0}
    own = mine()
    known = correspondents(own)
    db = store or Store()
    try:
        rules = db.rules()
        decided = [(m, classify(m, rules, known, own)) for m in load(ids, own)]
        tag(decided)
        db.record(decided)
    finally:
        if store is None:
            db.close()
    sent = [] if quiet else notifications(decided)
    for urgency, title, body in sent:
        notify_send(urgency, title, body)
    counts: dict[str, int] = {}
    for _, d in decided:
        counts[d.category] = counts.get(d.category, 0) + 1
    return {"counts": counts, "notified": len(sent)}


def since_seconds(text: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([mhdw])\s*", text or "")
    if not match:
        raise ValueError(f"{text!r} is not a duration like 30m, 12h, 2d, or 1w")
    return int(match.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]


def untriaged(since: int) -> list[str]:
    tagged = " or ".join(f"tag:triage/{c}" for c in CATEGORIES)
    query = f"date:@{since}.. and {inbox_query()} and not ({tagged})"
    return notmuch("search", "--output=messages", query).split()


def backfill(days: int = BACKFILL_DAYS, redo: bool = False) -> dict:
    """Quietly sort recent inbox mail: all of it with redo, else only what has no triage tag."""
    since = int(time.time()) - days * 86400
    ids = (notmuch("search", "--output=messages", f"date:@{since}.. and {inbox_query()}").split() if redo
           else untriaged(since))
    return intake(ids, quiet=True)


def retriage_sender(key: str, days: int = BACKFILL_DAYS) -> dict:
    """Re-sort a sender's recent inbox mail after Nick changes their rule."""
    since = int(time.time()) - days * 86400
    ids = notmuch("search", "--output=messages", f"date:@{since}.. and {inbox_query()} and from:{key}").split()
    return intake(ids, quiet=True)


# ---- reading it back -----------------------------------------------------------------------

def triaged(since: int, categories=CATEGORIES) -> list[dict]:
    """Sorted mail since a Unix time, newest first, with its category, reason, and read state."""
    query = f"date:@{since}.. and (" + " or ".join(f"tag:triage/{c}" for c in categories) + ")"
    shown = json.loads(notmuch("show", "--format=json", "--body=false", "--entire-thread=false", query) or "[]")
    found = sorted((m for thread in shown for m in flatten(thread) if m.get("match", True)),
                   key=lambda m: m.get("timestamp", 0), reverse=True)
    with Store() as db:
        reasons = db.reasons([m["id"] for m in found])
    out = []
    for m in found:
        tags = m.get("tags", [])
        category = next((c for c in CATEGORIES if f"triage/{c}" in tags), "")
        name, address = parseaddr(m.get("headers", {}).get("From", ""))
        out.append({"id": m["id"], "category": category, "reason": reasons.get(m["id"], ""),
                    "from": name or address, "address": address.lower(),
                    "subject": m.get("headers", {}).get("Subject", ""), "date": m.get("date_relative", ""),
                    "unread": "unread" in tags})
    return out


def digest_text(items: list[dict], label: str) -> tuple[str, str]:
    look = sorted((i for i in items if i["category"] in LOOK and i["unread"]), key=lambda i: LOOK.index(i["category"]))
    seen = sum(i["category"] in LOOK and not i["unread"] for i in items)
    bulk = [i for i in items if i["category"] == "digest"]
    title = f"Mail since {label}: {len(look)} to look at, {len(bulk)} in the digest"
    lines = []
    if look or seen:
        lines.append("To look at")
        for item in look:
            mark = "urgent: " if item["category"] == "urgent" else ""
            lines.append(f"- {mark}{short(item['from'], 30)} — {short(item['subject'] or '(no subject)', 60)}"
                         f" ({item['reason'] or item['category']})")
        if seen:
            lines.append(f"- and {seen} you have already read")
    if bulk:
        groups: dict[str, list[dict]] = {}
        for item in bulk:
            groups.setdefault(item["from"] or item["address"], []).append(item)
        ranked = sorted(groups.items(), key=lambda g: -len(g[1]))
        lines.append(f"Digest ({len(bulk)} from {len(groups)} senders)")
        for sender, group in ranked[:15]:
            subjects = " · ".join(short(i["subject"] or "(no subject)", 50) for i in group[:2])
            lines.append(f"- {short(sender, 30)} ({len(group)}): {subjects}")
        if len(ranked) > 15:
            lines.append(f"- and {len(ranked) - 15} more senders")
    return title, "\n".join(lines) or "No new mail."


def digest(since: str = "", notify: bool = True) -> dict:
    """The morning/evening summary. Without `since` it covers the time since the last digest (at most a day)."""
    now = int(time.time())
    with Store() as db:
        last = int(float(db.get("last_digest", "0")))
    if since:
        start, label = now - since_seconds(since), since
    else:
        start = max(last, now - 86400) if last else now - 12 * 3600
        label = time.strftime("%a %H:%M", time.localtime(start))
    if ids := untriaged(start):  # nothing slips through if the hook ever missed a message
        intake(ids, quiet=True)
    items = triaged(start, ("urgent", "today", "unsure", "digest"))
    title, text = digest_text(items, label)
    if notify and items:
        body = text.splitlines()
        body = "\n".join(body[:12] + ([f"… {len(body) - 12} more lines (omni mail digest)"] if len(body) > 12 else []))
        notify_send("normal", title, body[:1500])
    if notify and not since:
        with Store() as db:
            db.put("last_digest", str(now))
    return {"title": title, "text": text}


# ---- command line (the hook's fallback when omnid is down) ---------------------------------

def dry_run(days: int, sample: int) -> None:
    since = int(time.time()) - days * 86400
    ids = notmuch("search", "--output=messages", f"date:@{since}.. and {inbox_query()}").split()
    own = mine()
    known = correspondents(own)
    with Store() as db:
        rules = db.rules()
    decided = [(m, classify(m, rules, known, own)) for m in load([i.removeprefix("id:") for i in ids], own)]
    counts: dict[str, int] = {}
    for _, d in decided:
        counts[f"{d.category}/{d.rule}"] = counts.get(f"{d.category}/{d.rule}", 0) + 1
    print(json.dumps(dict(sorted(counts.items())), indent=1))
    for mail, decision in decided[:sample] if sample else decided:
        print(f"{decision.category:7} {short(mail.who, 28):28} {short(mail.subject, 60):60} | {decision.reason}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m omni.triage", description="Email triage rules without omnid")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("intake", help="sort message ids given as arguments or one per line on stdin")
    p.add_argument("ids", nargs="*")
    p.add_argument("--quiet", action="store_true", help="tag only, no notifications (also MAIL_QUIET=1)")
    p = sub.add_parser("backfill", help="quietly sort recent inbox mail")
    p.add_argument("--days", type=int, default=BACKFILL_DAYS)
    p.add_argument("--redo", action="store_true", help="re-sort mail that is already tagged")
    p = sub.add_parser("dry-run", help="print decisions for recent inbox mail without tagging")
    p.add_argument("--days", type=int, default=BACKFILL_DAYS)
    p.add_argument("--sample", type=int, default=0)
    p = sub.add_parser("digest")
    p.add_argument("--since", default="")
    p.add_argument("--no-notify", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == "intake":
            ids = args.ids or ([] if sys.stdin.isatty() else sys.stdin.read().split())
            quiet = args.quiet or bool(os.environ.get("MAIL_QUIET"))
            print(json.dumps(intake(ids, quiet=quiet)))
        elif args.command == "backfill":
            print(json.dumps(backfill(args.days, args.redo)))
        elif args.command == "dry-run":
            dry_run(args.days, args.sample)
        else:
            result = digest(args.since, notify=not args.no_notify)
            print(result["title"] + "\n\n" + result["text"])
    except (RuntimeError, ValueError) as exc:
        print(f"omni.triage: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
