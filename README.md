# IMOTH Cancellations

A small, standalone web app for one staff member to cancel insurance covers. You email the insurer with a stated reason, track the insurer's reply, and record the outcome. It is separate from any commission or payout system.

**Stack:** Django 5.2 LTS, PostgreSQL, server-rendered templates (a few lines of plain JS, no build step), **Zoho Mail** for email (SMTP to send, IMAP to read replies), and S3-compatible object storage (DigitalOcean Spaces). All configuration comes from environment variables. Postmark and Mailgun are also supported as alternatives.

## Contents

- [How it works](#how-it-works)
- [Local setup](#local-setup)
- [Running tests](#running-tests)
- [Users and the cancellations role](#users-and-the-cancellations-role)
- [Zoho Mail setup](#zoho-mail-setup)
- [DNS records (SPF, DKIM, DMARC)](#dns-records-spf-dkim-dmarc)
- [Alternative providers: Postmark or Mailgun](#alternative-providers-postmark-or-mailgun)
- [Environment variables](#environment-variables)
- [Deploying to DigitalOcean App Platform](#deploying-to-digitalocean-app-platform)

---

## How it works

```
DRAFT ──send──▶ SENT ──staff review──▶ CONFIRMED | REJECTED | QUERY ──▶ CLOSED
                                              QUERY ──▶ CONFIRMED | REJECTED
(+ STALE flag, Phase 2)
```

1. **Create.** You pick an existing cover or enter a new one (client, policy number, insurer, vehicle/asset, premium, dates). You also choose a reason code, set the effective date (it must fall inside the cover period) and attach the supporting document. The case is saved as **DRAFT**. A cover can have only one open case.
2. **Send.** The app renders the insurer's own template, or the default one. It sends from `cancellation@imoth.co.ke` to the insurer's cancellation address, with the insurer's CC list copied in and **all** the case's client and staff documents attached. Each email carries:
   - the subject `Cancellation request IMOTH-CXL-000123 – Policy … – Client`
   - a `Reply-To` of `cancellation+CXL000123@imoth.co.ke`
   - a unique `Message-ID`, which is stored on the case

   Sending is **blocked** when any of these is missing: the reason, the effective date, a field the reason needs, a document the reason needs, or the insurer's signed form when that insurer requires one. The database records are written first and the email is sent last, all in one transaction. If sending fails, everything rolls back, the case stays DRAFT and a `SEND_FAILED` event is logged.
3. **Track.** A background worker (`manage.py poll_mailbox --loop`) reads new mail from `cancellation@imoth.co.ke` over IMAP every minute. It opens the mailbox **read-only**, so messages you haven't read in Zoho stay unread. The app tries to match each reply to a case, in this order:
   1. the plus address `cancellation+CXL000123@…` in To, Cc or Delivered-To
   2. the reference `IMOTH-CXL-000123` in the subject
   3. `In-Reply-To` or `References` pointing at a Message-ID we sent
   4. the policy number in the body, but only when exactly **one** open (SENT/QUERY) case has that number

   The app never guesses. Anything unmatched or ambiguous goes to the **Unmatched inbox**, where you link it to a case by hand or dismiss it. Every email and attachment is stored on the case.
4. **Confirm.** You review each reply and mark it Confirmed, Rejected, Query or Information only. **Nothing is ever confirmed automatically.**

**Sender verification.** A matched reply is attached to its case only when both of these are true:

- the parsed `From` address is one of the insurer's known addresses: its cancellation email, its CC list, or an entry in *known sender addresses* (a full address or `@domain`)
- Zoho's receiving server recorded an SPF or DMARC **pass** for it

A reply that fails either check goes to the Unmatched inbox, with the likely case shown as a suggestion. To link an unknown sender by hand, you have to tick "I have checked this sender".

Authentication results come only from the verdict Zoho's receiving server stamps on the message:

- the app reads the **topmost** `Authentication-Results` header (configurable via `INBOUND_AUTH_HEADER`)
- it uses that header only when the authserv-id is `mx.zohomail.com` (`INBOUND_AUTHSERV_ID`)

A sender can't place a header above Zoho's, so lower copies are ignored. A pass means DMARC passed, or SPF passed for an envelope sender aligned with the From domain. Any fail or softfail rejects the reply.

**Why the strict check stays on.** A confirmation leads to a refund and a cancelled cover, and a From address on its own is trivial to fake. If the check fails, the reply simply waits in the Unmatched inbox with the right case suggested, and you link it in one click. `INBOUND_REQUIRE_SENDER_AUTH=false` turns it off, but that isn't recommended.

**Idempotency.** `case_email.message_id` is unique. When an email has no `Message-ID`, the key falls back to a hash of the content. If the same email is read twice (a retry, a crash, or Zoho renumbering the folder), the second copy is ignored and no duplicate documents or events are created. Polling progress is stored per folder by IMAP UID. A message that fails 3 polls in a row is skipped, and the error is shown on the Unmatched inbox page.

**Audit log.** `case_event` is append-only. The model refuses updates and deletes, and so does a database trigger (Postgres and SQLite), which also blocks `QuerySet.update()` and `delete()`. The log records every create, edit, document upload, send, send failure, received email, manual link, review decision and close.

**Files.** Uploads are checked three ways:

- the extension must be one of pdf, png, jpg/jpeg, tif/tiff, doc, docx or xlsx
- the content must match that type (its leading "magic" bytes)
- the size must be within `MAX_UPLOAD_MB`

Inbound attachments go through the same checks. One that fails is not stored, and it is listed on the email as skipped. Files are kept in private object storage (Spaces) or in a local folder outside the web root. Nothing is ever served by URL: downloads go through an authenticated view.

### Reason codes

| Code | Requires |
|---|---|
| `CLIENT_REQUEST` | Signed client request (document) |
| `NON_PAYMENT` | Outstanding amount + notice date (fields) |
| `ASSET_DISPOSED` | Proof of sale (document) |
| `REPLACED` | New policy number (field) |
| `MISREPRESENTATION` | Evidence (document) |
| `DUPLICATE` | Original cover reference (field) |
| `INSURER_INITIATED` | Insurer's notice (document) |

If the insurer has `requires_signed_form` set, its signed cancellation form is also required. The list lives in `cancellations/reasons.py`.

---

## Local setup

You need Python 3.12 or newer and PostgreSQL 14 or newer. If `DATABASE_URL` is unset, the app falls back to SQLite, which is enough for a quick look.

```powershell
git clone <repo> imoth-cancellations; cd imoth-cancellations
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create the database. The `psql` commands below create the role and database:

```powershell
psql -U postgres -c "CREATE ROLE imoth LOGIN PASSWORD 'imoth';" -c "CREATE DATABASE imoth_cancellations OWNER imoth;"
```

Set the environment for the current PowerShell session:

```powershell
$env:DEBUG = "true"
$env:SECRET_KEY = "dev-only-secret"
$env:DATABASE_URL = "postgres://imoth:imoth@localhost:5432/imoth_cancellations"
$env:EMAIL_PROVIDER = "console"      # outgoing mail is printed to the terminal
$env:MAIL_DOMAIN = "imoth.co.ke"
$env:CANCELLATIONS_MAILBOX = "cancellation"
```

Then migrate, create a user, give them the role, and run:

```powershell
python manage.py migrate
python manage.py createsuperuser
python manage.py grant_cancellations_role <username>
python manage.py runserver
```

Open http://127.0.0.1:8000/admin/ and add at least one **Insurer** (name, cancellation email, CC list, known sender addresses, SLA hours, template). Then use the app at http://127.0.0.1:8000/.

To read real replies locally, set `MAILBOX_PASSWORD` (see [Zoho Mail setup](#zoho-mail-setup)) and run `python manage.py poll_mailbox`. It reads the live mailbox read-only. `cancellations/tests/test_mailbox.py` shows sample raw messages.

## Running tests

```powershell
python manage.py test cancellations --settings=config.settings_test
```

The test settings use SQLite, the in-memory mail backend and a fake IMAP server, so they need no environment variables and no network. To run the tests against Postgres, set `DATABASE_URL` first. The suite covers:

- each of the four matching methods, plus the unmatched and ambiguous cases
- sender verification, including spoofed display names and forged auth headers
- IMAP polling: only new UIDs are fetched, a UIDVALIDITY reset is handled, a failing message is retried then skipped, and fetches never mark mail as read
- send-blocking validation
- idempotency, at both the IMAP and webhook level
- status transitions (every disallowed pair is tested)
- the append-only trigger
- uploads and the role check

## Users and the cancellations role

- The app is open only to members of the **`cancellations`** group, which a migration creates. Superusers are **not** exempt.
- There is no second approver. The member who creates a case also sends and reviews it.
- Grant or revoke the role with `python manage.py grant_cancellations_role <username> [--revoke]`, or in the admin.
- The Django admin (`/admin/`) is for setting up insurers and covers. Cases, emails, documents and events are **read-only** there, so every change goes through the logged workflow.

---

## Zoho Mail setup

The app logs in to the mailbox **`cancellation@imoth.co.ke`**. It sends through SMTP and reads replies over IMAP. Because the domain already receives mail at Zoho, **no MX change is needed**.

1. **Enable IMAP** for the mailbox: Zoho Mail → Settings → Mail Accounts → IMAP Access → enable.
2. **Create an app-specific password.** This is required when two-factor authentication is on, and recommended either way. Zoho Accounts → Security → App Passwords → generate one called "IMOTH Cancellations". Put it in `MAILBOX_PASSWORD`. The app never uses the account's real password.
3. **Check that plus addressing reaches the mailbox.** From an outside address, send a test email to `cancellation+test@imoth.co.ke`. It should land in the inbox. If it doesn't, replies still match by the reference in the subject and by thread headers, but the Reply-To should then be changed to the plain mailbox. That's a code change to discuss first.
4. **Use the servers for your Zoho data centre.** Organisation (custom-domain) accounts use `smtppro.zoho.com` and `imappro.zoho.com`. If your Zoho admin URL ends in `.eu`, `.in` or `.com.au`, use the matching host, e.g. `smtppro.zoho.eu`. SMTP uses port 465 (SSL) or 587 (STARTTLS); IMAP uses 993.
5. **Confirm the sender-check settings against real mail.** Ask someone outside IMOTH to reply to a test message, then run:

   ```powershell
   python manage.py check_mailbox --count 3
   ```

   It shows the authentication headers Zoho stamped and whether each message would count as verified. Nothing in the mailbox changes. If Zoho's verdict is in `ARC-Authentication-Results` rather than `Authentication-Results`, set `INBOUND_AUTH_HEADER=ARC-Authentication-Results`. If the authserv-id isn't `mx.zohomail.com`, set `INBOUND_AUTHSERV_ID` to what it shows.
6. **Run the worker.** In production it's the `mail-worker` component in `.do/app.yaml`, which runs `python manage.py poll_mailbox --loop`. Run **one** instance only. A second one would be harmless, but it would duplicate the work.

Mailbox habits: keep insurer replies in `INBOX` (`IMAP_FOLDER`) until they appear on the case. The worker reads only that folder, so a Zoho filter that moves replies into another folder would hide them from the app. Reading, flagging or archiving later is fine, because the app keeps its own copy of each email and attachment.

### Insurer configuration

For each insurer, set **known sender addresses** to every address or domain its claims and cancellations teams reply from, for example `@insurer.co.ke, cancellations@insurer-group.com`. A reply from anywhere else is never attached automatically.

---

## DNS records (SPF, DKIM, DMARC)

Mail for `imoth.co.ke` already runs through Zoho, so the MX records stay as they are. What matters is that mail the app sends from `cancellation@imoth.co.ke` passes SPF, DKIM and DMARC. That way insurers' spam filters accept it, and nobody can easily forge a cancellation request in IMOTH's name. Check these in the Zoho Mail Admin Console → Domains → imoth.co.ke → Email Configuration:

| Purpose | Type | Host | Value |
|---|---|---|---|
| SPF | TXT | `@` (imoth.co.ke) | `v=spf1 include:zohomail.com ~all` (EU data centre: `include:zoho.eu`). Merge into any existing SPF record; never publish two |
| DKIM | TXT | `<selector>._domainkey` (e.g. `zmail._domainkey`) | the public key generated in Zoho Admin → DKIM, then click **Verify** |
| DMARC | TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:dmarc-reports@imoth.co.ke; adkim=r; aspf=r` |
| MX | MX | `@` | unchanged: Zoho's `mx`, `mx2`, `mx3` hosts for your region |

- Start DMARC at `p=none`, read the reports for a couple of weeks, then move to `p=quarantine` and finally `p=reject`.
- Check the records with `nslookup -type=TXT imoth.co.ke` and `nslookup -type=TXT _dmarc.imoth.co.ke`.
- The app sends only as `cancellation@imoth.co.ke`, the logged-in account. Zoho rejects any other From address unless it's an alias of that account.

---

## Alternative providers: Postmark or Mailgun

Set `EMAIL_PROVIDER=postmark` or `mailgun` to send through their API and receive replies through their inbound webhook instead of IMAP:

- The webhook URL is `https://webhookuser:password@<app-host>/email/postmark/inbound/`, or `/email/mailgun/inbound_mime/` set up as a Mailgun route.
- It is protected by `INBOUND_WEBHOOK_SECRET`.
- These providers need their own DNS records (DKIM, Return-Path, and MX for a reply sub-domain) from the provider's dashboard.
- Set `INBOUND_AUTHSERV_ID` to the provider's authserv-id. Postmark's `Received-SPF` or Mailgun's `X-Mailgun-Spf` is read automatically.

---

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SECRET_KEY` | yes (prod) | – | Django secret |
| `DEBUG` | no | `false` | Never `true` in production |
| `ALLOWED_HOSTS` | yes (prod) | – | Comma-separated host names |
| `CSRF_TRUSTED_ORIGINS` | yes (prod) | – | e.g. `https://cancellations.imoth.co.ke` |
| `DATABASE_URL` | yes (prod) | SQLite file | `postgres://user:pass@host:port/db` |
| `DB_SSL_REQUIRE` | no | `false` | `true` on DigitalOcean Managed Postgres |
| `DB_CONN_MAX_AGE` | no | `60` | Persistent connection seconds |
| `TIME_ZONE` | no | `Africa/Nairobi` | Display time zone |
| `EMAIL_PROVIDER` | yes | `console` | `zoho` (production), `postmark`, `mailgun` or `console` |
| `MAIL_DOMAIN` | yes | `example.com` | `imoth.co.ke`: domain for Reply-To plus addresses and Message-IDs |
| `CANCELLATIONS_MAILBOX` | yes | `cancellations` | `cancellation`: local part of the mailbox |
| `DEFAULT_FROM_EMAIL` | no | `IMOTH Cancellations <cancellation@imoth.co.ke>` | From header; must be the mailbox or its alias |
| `MAILBOX_USER` | no | `CANCELLATIONS_MAILBOX@MAIL_DOMAIN` | Zoho login (SMTP and IMAP) |
| `MAILBOX_PASSWORD` | Zoho | – | Zoho **app-specific** password |
| `SMTP_HOST` / `SMTP_PORT` | no | `smtppro.zoho.com` / `465` | Use 587 for STARTTLS; regional host if not `.com` |
| `IMAP_HOST` / `IMAP_PORT` | no | `imappro.zoho.com` / `993` | Regional host if not `.com` |
| `IMAP_FOLDER` | no | `INBOX` | Folder the worker reads |
| `IMAP_LOOKBACK_DAYS` | no | `14` | How far back to read on the first run or after a renumbering |
| `MAIL_POLL_INTERVAL` | no | `60` | Seconds between polls |
| `INBOUND_AUTH_HEADER` | no | `Authentication-Results` | Header carrying the receiving server's SPF/DMARC verdict |
| `INBOUND_AUTHSERV_ID` | no | `mx.zohomail.com` with Zoho | authserv-id that header must carry |
| `INBOUND_REQUIRE_SENDER_AUTH` | no | `true` | Require an SPF/DMARC pass before attaching a reply |
| `INBOUND_WEBHOOK_SECRET` | Postmark/Mailgun | – | `user:password` Basic auth on the inbound webhook |
| `POSTMARK_SERVER_TOKEN` | Postmark | – | Server API token |
| `MAILGUN_API_KEY` / `MAILGUN_WEBHOOK_SIGNING_KEY` | Mailgun | – | API and webhook signing keys |
| `MAILGUN_SENDER_DOMAIN` / `MAILGUN_API_URL` | no | `MAIL_DOMAIN` / US | EU: `https://api.eu.mailgun.net/v3` |
| `AWS_STORAGE_BUCKET_NAME` | yes (prod) | – | Spaces bucket; when unset, files go to local disk |
| `AWS_S3_ENDPOINT_URL` | with bucket | – | e.g. `https://fra1.digitaloceanspaces.com` |
| `AWS_S3_REGION_NAME` | with bucket | – | e.g. `fra1` |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | with bucket | – | Spaces access keys |
| `AWS_LOCATION` | no | `case-documents` | Key prefix inside the bucket |
| `PRIVATE_MEDIA_ROOT` | no | `./private_media` | Local file folder (dev only) |
| `MAX_UPLOAD_MB` | no | `10` | Per-file limit (uploads and inbound attachments) |
| `SECURE_SSL_REDIRECT` | no | `false` | App Platform already redirects to HTTPS |
| `SECURE_HSTS_SECONDS` | no | `0` | Set e.g. `31536000` once HTTPS is confirmed |
| `LOG_LEVEL` | no | `INFO` | |

---

## Deploying to DigitalOcean App Platform

1. Push this repository to GitHub.
2. Create a **private** Spaces bucket and an access key pair.
3. Edit `.do/app.yaml`: set the repo, the domain and your values. Then run `doctl apps create --spec .do/app.yaml`, or create the app in the UI with the same components:
   - a **web** service: build `python manage.py collectstatic --noinput`, run `gunicorn config.wsgi --bind 0.0.0.0:8080 --workers 2`
   - a **mail-worker** worker: `python manage.py poll_mailbox --loop`, with exactly 1 instance
   - a **pre-deploy job**: `python manage.py migrate --noinput`
   - a Managed Postgres database
4. Enter the secrets (`SECRET_KEY`, `MAILBOX_PASSWORD` and the Spaces keys) as encrypted env vars on both the web service and the worker.
5. After the first deploy, open the console and run `python manage.py createsuperuser`, then `python manage.py grant_cancellations_role <username>`.
6. Run `python manage.py check_mailbox` in the console. It confirms the IMAP login and shows the auth headers. Then check that the Unmatched inbox page shows "Mailbox INBOX last checked … ago". The health check is at `/healthz`.

App Platform's filesystem is ephemeral, so production **must** set `AWS_STORAGE_BUCKET_NAME`.
