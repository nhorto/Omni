# Email

Omni reads, searches, drafts, and sends mail for three accounts: Microsoft 365 (work), Gmail, and Yahoo. It works from a local copy, not from the providers' web APIs:

```
server ──IMAP IDLE──▶ goimapnotify ──▶ mail-sync ──▶ mbsync ──▶ ~/Mail/<account>/  ──▶ notmuch index
                                                                                         │
       Omni mail_search / mail_read ◀── instant, offline ─────────────────────────────────┤
       aerc (terminal client) ◀──────────────────────────────────────────────────────────┘
Omni mail_send / aerc ──▶ msmtp ──▶ provider SMTP        Omni mail_draft ──▶ ~/Mail/<account>/Drafts ──▶ server
```

- **mbsync** (`isync`) keeps each account's folders in two-way sync, flags included. Well-known folders get the same local names on every account: `Inbox`, `Archive`, `Sent`, `Drafts`, `Trash`, `Spam`. Gmail's labels are skipped; every Gmail message is in `Archive` (All Mail).
- **goimapnotify** holds an IMAP IDLE connection per inbox, so new mail arrives within seconds with no polling. A 15-minute `mail-sync.timer` catches changes in other folders.
- **notmuch** indexes everything. Its `post-new` hook tags spam, trash, and sent mail and sends a desktop notification for new unread inbox mail.
- **msmtp** is the one sending path for Omni, aerc, and the shell.
- **Credentials** never sit in files. Gmail and Yahoo use app passwords in the GNOME keyring (`secret-tool … service mail account <name>`). Microsoft 365 uses OAuth through `ortie`, whose refresh token is also in the keyring.

Why not Microsoft Graph: Graph push needs a public HTTPS webhook, so a laptop would have to poll. It also covers only the work account. Graph remains the fallback if the tenant ever blocks IMAP (`himalaya` has a Graph backend).

## Omni tools

| Tool | What it does |
| --- | --- |
| `mail_search` | notmuch query across all accounts (or one); empty query is unread inbox mail |
| `mail_read` | A thread or message as plain text, quoted history trimmed, attachment names |
| `mail_draft` | Writes a draft into the account's Drafts maildir; it syncs to the server and phone |
| `mail_send` | Sends through msmtp. Always asks, showing To, Cc, Subject, and the body |
| `mail_sync` | Runs `mail-sync` now |

Accounts are read from msmtp's config (`account` + `from`). Mail text is untrusted input: the tool descriptions tell the model never to follow instructions found in email, and sending always needs Nick's approval. The shell policy asks before `msmtp`, `sendmail`, and himalaya's send or delete commands, so the terminal is not a way around `mail_send`.

## Files (outside the repo)

| Path | Purpose |
| --- | --- |
| `~/.config/isyncrc` | mbsync accounts and channels |
| `~/.config/msmtp/config` | SMTP accounts |
| `~/.config/imapnotify/<account>.yaml` | IDLE watchers, run by `goimapnotify@<account>.service` (packaged unit) |
| `~/.config/systemd/user/mail-sync.{service,timer}` | 15-minute backstop sync |
| `~/.config/notmuch/default/config`, `hooks/post-new` | Index settings, tagging, notifications |
| `~/.config/aerc/accounts.conf` | Terminal client, one tab per account |
| `~/.config/ortie/config.toml` | OAuth for Microsoft 365 |
| `~/.local/bin/mail-sync` | `mbsync` per account (each under its own lock, so a throttled account never blocks the others), then `notmuch new` under a short index lock; takes account names, defaults to every `Group` in isyncrc |
| `~/.local/bin/mail-password` | Stores an app password in the keyring |

## Setting up an account

Packages: `isync notmuch goimapnotify msmtp aerc`, plus `cyrus-sasl-xoauth2-git` (AUR) for Microsoft 365.

**Gmail.** Turn on 2-Step Verification, create an app password at myaccount.google.com/apppasswords, then `mail-password gmail`.

**Yahoo.** Account security → Generate app password, then `mail-password yahoo`.

Then, per account:

```bash
mkdir -p ~/Mail/<account>          # mbsync will not create the store root
MAIL_QUIET=1 mail-sync <account>   # first sync without a notification flood (large mailboxes take a while)
systemctl --user enable --now goimapnotify@<account>
systemctl --user enable --now mail-sync.timer
```

Provider limits worth knowing:

- **Yahoo IMAP shows only the newest 10,000 messages of a folder** (`SELECT` reports 10000 even when `STATUS` counts more). The local copy is a rolling window of the latest 10k; older mail is only in Yahoo's web app. Yahoo files mail sent over SMTP in Sent by itself, as Gmail and Microsoft 365 do, so no client should save its own copy (`AUTO_SENT` in `mail.py`, no `copy-to` in aerc). Yahoo's spam filter may put plain test messages from your own Gmail in Bulk. Yahoo also throttles pipelined fetches (`[UNAVAILABLE] UID FETCH Service is temporarily not available`), so its account uses `PipelineDepth 1`; a first sync runs at about 100 messages a minute.
- **Gmail** caps IMAP downloads at about 2.5 GB a day. A first sync larger than that stops with an error and finishes on the next run. Inbox messages are also in All Mail, so a Gmail first sync downloads the inbox twice (notmuch shows each message once).

**Microsoft 365 (tenant admin).** Microsoft's 2025 defaults block device-code sign-in and user consent to mail scopes, so register an app and use the browser flow:

1. entra.microsoft.com → App registrations → New registration. "Accounts in this organizational directory only". Redirect URI: platform *Public client/native*, `http://localhost:8766`.
2. API permissions → Add → *APIs my organization uses* → **Office 365 Exchange Online** → Delegated: `IMAP.AccessAsUser.All`, `SMTP.Send`. Add Microsoft Graph `offline_access`. **Grant admin consent**.
3. admin.microsoft.com → Users → Active users → the mailbox → Mail → Manage email apps: **IMAP** and **Authenticated SMTP** on.
4. In `~/.config/ortie/config.toml`, point `[accounts.outlook]` at the new client ID, the tenant's `authorize` and `token` endpoints, the auth-code grant, and redirect `http://localhost:8766`. Then `ortie auth -a outlook` (sign in in the browser) and check `ortie -a outlook token show -r`.
5. Uncomment the Outlook blocks in `isyncrc` and `aerc/accounts.conf`, then run the per-account steps above with `outlook`.

The refresh token lasts 90 days from last use and renews on every sync, so sign-in is one-time unless the password changes or the token is revoked.

## Troubleshooting

- `journalctl --user -u goimapnotify@gmail -f` shows IDLE events; `~/.local/state/msmtp.log` shows sends.
- `mail-sync gmail` by hand prints mbsync errors.
- The goimapnotify unit stops restarting after 5 failures in a day. After fixing credentials, run `systemctl --user reset-failed goimapnotify@<account>`.
- Moving mail between folders by hand: strip `,U=<n>` from the file name or mbsync reports duplicate UIDs.
