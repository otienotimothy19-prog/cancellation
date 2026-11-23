# IMOTH Cancellations

[![tests](https://github.com/otienotimothy19-prog/cancellation/actions/workflows/tests.yml/badge.svg)](https://github.com/otienotimothy19-prog/cancellation/actions/workflows/tests.yml)

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
                                              QUERY ──reply to insurer──▶ SENT (SLA restarts)
SENT + no reply past the SLA ──▶ reminder email ──▶ STALE flag (cleared by a reply)
```

1. **Create.** You pick an existing cover, look it up in the Production Portal, or enter a new one (client, **certificate number** (required), insurer, vehicle/asset; optional premium and dates). The policy number is optional and comes from the portal when available. You also choose a reason code, set the effective date (it must fall inside the cover period) and attach the supporting document. A cover can have only one open case.
   - **Save & send** (the usual path) first shows the exact email: To, Cc, subject and body. If anything required is missing, it lists that instead. One click on **Send now** saves and sends.
   - **Save draft** keeps it as **DRAFT** to finish later. If sending turns out to be blocked, the case is saved as a draft with the reasons shown.
2. **Send.** The app renders the insurer's own template, or the default one. It sends from `cancellation@imoth.co.ke` to the insurer's cancellation address, with the insurer's CC list copied in and **all** the case's client and staff documents attached. Each email carries:
   - the subject `Cancellation request IMOTH-CXL-000123 – Policy … – Client`
   - a `Reply-To` of `cancellation+CXL000123@imoth.co.ke`
   - a unique `Message-ID`, which is stored on the case

   Sending is **blocked** when any of these is missing: the reason, the effective date, a field the reason needs, a document the reason needs, or the insurer's signed form when that insurer requires one. The database records are written first and the email is sent last, all in one transaction. If sending fails, everything rolls back, the case stays DRAFT and a `SEND_FAILED` event is logged.
3. **Track.** A background worker (`manage.py run_worker`) reads new mail from `cancellation@imoth.co.ke` over IMAP every minute. It opens the mailbox **read-only**, so messages you haven't read in Zoho stay unread. The app tries to match each reply to a case, in this order:
   1. the plus address `cancellation+CXL000123@…` in To, Cc or Delivered-To
   2. the reference `IMOTH-CXL-000123` in the subject
   3. `In-Reply-To` or `References` pointing at a Message-ID we sent
   4. the certificate number or policy number in the body, but only when exactly **one** open (SENT/QUERY) case has that number

   The app never guesses. Anything unmatched or ambiguous goes to the **Unmatched inbox**, where you link it to a case by hand or dismiss it. Every email and attachment is stored on the case.
4. **Confirm.** You review each reply and mark it Confirmed, Rejected, Query or Information only. **Nothing is ever confirmed automatically.**
   **Answering a query.** When you mark a reply as a Query, the case page shows a **Reply to the insurer** form. The message comes prefilled with the reference and a quote of the insurer's question. You can attach documents already on the case and/or upload a new one. The reply:
   - goes to whoever raised the query, with the insurer's cancellation address and CC list copied in
   - is threaded with `In-Reply-To`/`References`, and keeps the reference in the subject and the same plus Reply-To, so the insurer's next answer matches automatically
   - moves the case back to **SENT** and restarts the SLA clock (`last_sent_at`), while `sent_at` keeps the original request time

   As with the first request, a provider error sends nothing, leaves the case in QUERY and logs `SEND_FAILED`.
5. **Reviewing a reply.** The review box offers only the outcomes the case's status allows. It reads the insurer's
   own new text (the quoted copy of our request and anything after the sign-off are ignored) and, when the wording
   is clear ("done", "has been cancelled", "kindly provide…", "unable to cancel"), **pre-selects** Confirmed, Query or
   Rejected with a note saying so. This is a suggestion only: nothing is recorded until staff press **Record decision**
   (the "never auto-confirm" rule). Mixed wording ("not yet done", "will be cancelled") gets no suggestion.

   **On CONFIRMED** the app uses the case's (locked) effective date as the confirmed date, then:
   - records the **pro-rata refund of unearned premium** when premium and cover dates were given, with the working in the audit log
   - marks the **cover CANCELLED**
   - **closes the case**. Insurers usually just reply "done", so their confirming email (kept on the case) is the proof.
     A cancellation certificate / credit note is optional and can be uploaded at any time, even after closing.

   **Production Portal check.** When the portal is connected, the worker looks up each confirmed case's certificate
   (or policy) number in the Production Portal for `PORTAL_CHECK_DAYS` (default 60) days and records one of:
   *Cancelled there too*, *Still active there* (re-checked hourly; listed on the dashboard under "Confirmed — follow up",
   as the insurer's notice may not have reached the portal), *Not in the portal* (neutral: not every cover is in the
   portal; re-checked every 12 hours in case it is added), or *No number to look up*. Each change is logged on the case,
   and the case page has a **Check now** button. Run `manage.py check_portal` for a one-off pass. The portal is only
   searched (read-only); its notice emails and documents are not fetched.
6. **SLA.** If a SENT case gets no insurer reply within the insurer's `sla_hours`, the staff member who created it gets a **reminder email**. The reminders are bundled into one email per person. If there's still no reply `SLA_STALE_AFTER_REMINDER_HOURS` later (default 24), the case is **flagged STALE**. STALE is a flag, not a status. Any reply linked to the case clears it. The count uses wall-clock hours.
7. **Dashboard** (the home page) shows:
   - counts of open, stale, unmatched and confirmed-this-month cases, plus the month's refund total
   - work lists: replies to review (each with a **Review** button that jumps straight to that reply's decision form), stale cases, "Confirmed — follow up" (still active in the Production Portal, or left at Confirmed from before cases closed on confirmation) and drafts
   - a search box for a certificate number, policy number or an `IMOTH-CXL-…` reference; a single hit opens the case directly

### Refund calculation

```
refund = premium × unearned_days / cover_days        (rounded half-up to 2 decimals)
cover_days    = start_date … end_date, both days included
unearned_days = confirmed effective date … end_date, both days included
```

The confirmed effective date is the **first day without cover**. For example, a cover from 1 Jan to 31 Dec 2026 with a premium of 36,500, cancelled effective 30 Jun 2026, has 185 of 365 days unearned, so the refund is **18,500.00**. A date on or before the start gives a full refund. The logic lives in `cancellations/services/refunds.py`. It's a plain pro-rata figure, with no short-period scale or insurer fees.

### Confirmed cancellations for other systems

- **CSV** for staff: Dashboard → *Download CSV*, or `GET /exports/confirmed-cancellations.csv?confirmed_since=2026-11-01&confirmed_until=2026-11-30`. Values starting with `=`, `+`, `-` or `@` are neutralised so they can't run as spreadsheet formulas.
- **JSON API** for systems such as the commission system:

  ```
  GET /api/confirmed-cancellations/?confirmed_since=2026-11-01&limit=100&offset=0
  Authorization: Bearer <one of EXPORT_API_TOKENS>
  ```

  ```json
  {"count": 1, "limit": 100, "offset": 0, "next_offset": null, "results": [{
    "reference": "IMOTH-CXL-000001", "case_id": 1, "status": "CONFIRMED",
    "policy_number": "MTR-1001", "client_name": "Jane Wanjiru", "insurer": "…", "asset_reference": "KDA 123A",
    "premium": "36500.00", "cover_start_date": "2026-01-01", "cover_end_date": "2026-12-31",
    "requested_effective_date": "2026-06-30", "effective_date": "2026-06-30",
    "refund_amount": "18500.00", "currency": "KES", "reason_code": "CLIENT_REQUEST",
    "confirmed_at": "2026-11-23T10:15:00+03:00"}]}
  ```

  The API includes every case the insurer confirmed, whether it's still CONFIRMED or since CLOSED. Results are ordered by `confirmed_at`. For an incremental sync, store the last `confirmed_at` you received and pass it back as `confirmed_since`. `confirmed_since` and `confirmed_until` accept `YYYY-MM-DD` (whole day, Nairobi time) or a full ISO-8601 datetime. Amounts are strings, to avoid float rounding. Give each consuming system its own token, so you can revoke one without affecting the others.

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

### Production Portal lookup

When `PRODUCTION_PORTAL_URL` and `PRODUCTION_PORTAL_TOKEN` are set, New Cancellation shows a
**Find the certificate in the Production Portal** search (by vehicle registration, policy number,
certificate number or client name). Picking a record fills in the certificate number, policy number (kept, not shown), client, vehicle,
certificate number, insurer (matched by name to an active insurer here) and cover dates, and keeps the
portal record reference on the cover. If the policy is already on file here, the existing cover is selected.

It uses the portal's existing read-only API (`GET /api/underwriting/v1/records`), so the portal itself
needs no changes. Only the search endpoint is called; client phone and email are never fetched.
The portal records every lookup in its audit log, with the staff member's email in `X-Imoth-Staff`.
The portal returns no premium, so the refund is still calculated only when staff enter the premium.
The portal gives this app its own token (`CANCELLATIONS_API_TOKEN` there), which may only search. If the portal sets `CANCELLATIONS_API_IPS`, this app's outbound IP must be listed there.

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

**CI:** `.github/workflows/tests.yml` runs on every push and pull request. It checks that migrations are up to date, runs the migrations and the whole suite against **PostgreSQL 16**, then runs Django's deploy checks. This is the only place the Postgres-specific parts run, including the append-only trigger, so keep it green before deploying.

The test settings use SQLite, the in-memory mail backend and a fake IMAP server, so they need no environment variables and no network. To run the tests against Postgres, set `DATABASE_URL` first. The suite covers:

- each of the four matching methods, plus the unmatched and ambiguous cases
- sender verification, including spoofed display names and forged auth headers
- IMAP polling: only new UIDs are fetched, a UIDVALIDITY reset is handled, a failing message is retried then skipped, and fetches never mark mail as read
- send-blocking validation
- follow-ups to insurer queries: recipients, threading headers, attachments, return to SENT, SLA restart, the next reply matched by thread, send failure leaves QUERY
- the refund calculation (mid-term, inception, last day, after expiry, leap year, rounding) and the confirm flow: cover cancelled and the case closed on confirmation, optional certificate from an attachment or an upload (also after closing), reply-wording suggestions and the Production Portal check
- SLA: no action before the deadline, one reminder, the STALE flag after the grace period, a reply clears it, a failed send is retried, the per-insurer SLA
- the CSV export and the JSON API: auth, filters, pagination, formula neutralising
- dashboard counts and search
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
6. **Run the worker.** In production it's the `worker` component in `.do/app.yaml`, which runs `python manage.py run_worker`. That one process polls the mailbox every `MAIL_POLL_INTERVAL` seconds, runs the SLA check every `SLA_CHECK_INTERVAL` seconds and the Production Portal check every `PORTAL_CHECK_INTERVAL` seconds. Run **one** instance only. A second one would be harmless, but it would duplicate the work. For one-off runs there are `poll_mailbox` and `check_sla`.

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
| `EXPORT_API_TOKENS` | for the API | – | Comma-separated Bearer tokens for `/api/confirmed-cancellations/` (one per consuming system) |
| `CURRENCY` | no | `KES` | Currency code in exports |
| `SLA_ALERT_EMAIL` | no | – | Reminder recipient when the case creator has no email |
| `SLA_STALE_AFTER_REMINDER_HOURS` | no | `24` | Hours between the reminder and the STALE flag (0 = immediately) |
| `SLA_CHECK_INTERVAL` | no | `300` | Seconds between SLA checks in the worker |
| `APP_BASE_URL` | no | – | e.g. `https://cancellations.imoth.co.ke`; adds case links to reminder emails |
| `PRODUCTION_PORTAL_URL` | no | – | Production Portal base URL (no trailing slash). With the token, adds "Find in Production Portal" to New Cancellation |
| `PRODUCTION_PORTAL_TOKEN` | no | – | **Secret.** The portal's `CANCELLATIONS_API_TOKEN` (32–128 chars) |
| `INBOUND_WEBHOOK_SECRET` | Postmark/Mailgun | – | `user:password` Basic auth on the inbound webhook |
| `POSTMARK_SERVER_TOKEN` | Postmark | – | Server API token |
| `MAILGUN_API_KEY` / `MAILGUN_WEBHOOK_SIGNING_KEY` | Mailgun | – | API and webhook signing keys |
| `MAILGUN_SENDER_DOMAIN` / `MAILGUN_API_URL` | no | `MAIL_DOMAIN` / US | EU: `https://api.eu.mailgun.net/v3` |
| `AWS_STORAGE_BUCKET_NAME` | yes (prod) | – | Spaces bucket; when unset, files go to local disk |
| `AWS_S3_ENDPOINT_URL` | with bucket | – | e.g. `https://sfo3.digitaloceanspaces.com` (region address, not the bucket URL) |
| `AWS_S3_REGION_NAME` | with bucket | – | e.g. `sfo3` |
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
   - a **worker**: `python manage.py run_worker` (mailbox polling, SLA checks, Production Portal check), with exactly 1 instance
   - a **pre-deploy job**: `python manage.py migrate --noinput`
   - a Managed Postgres database
4. Enter the secrets (`SECRET_KEY`, `MAILBOX_PASSWORD`, `EXPORT_API_TOKENS` and the Spaces keys) as encrypted env vars on both the web service and the worker.
   Give each staff user an email address (in the admin), or set `SLA_ALERT_EMAIL`, so SLA reminders have somewhere to go.
5. After the first deploy, open the console and run `python manage.py createsuperuser`, then `python manage.py grant_cancellations_role <username>`.
6. Run `python manage.py check_mailbox` in the console. It confirms the IMAP login and shows the auth headers. Then check that the Unmatched inbox page shows "Mailbox INBOX last checked … ago". The health check is at `/healthz`.

App Platform's filesystem is ephemeral, so production **must** set `AWS_STORAGE_BUCKET_NAME`.
