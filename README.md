# IMOTH Cancellations

A small, standalone web app for one staff member to cancel insurance covers. You email the insurer with a stated reason, track the insurer's reply, and record the outcome. It is separate from any commission or payout system.

**Stack:** Django 5.2 LTS, PostgreSQL, server-rendered templates (a few lines of plain JS, no build step), django-anymail (Postmark or Mailgun), and S3-compatible object storage (DigitalOcean Spaces). All configuration comes from environment variables.

## Contents

- [How it works](#how-it-works)
- [Local setup](#local-setup)
- [Running tests](#running-tests)
- [Users and the cancellations role](#users-and-the-cancellations-role)
- [Email provider setup](#email-provider-setup)
- [DNS records (SPF, DKIM, DMARC, MX)](#dns-records-spf-dkim-dmarc-mx)
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
2. **Send.** The app renders the insurer's own template, or the default one. The email goes to the insurer's cancellation address, with the insurer's CC list copied in and the case documents attached. Each email carries:
   - the subject `Cancellation request IMOTH-CXL-000123 – Policy … – Client`
   - a `Reply-To` of `cancellations+CXL000123@<MAIL_DOMAIN>`
   - a unique `Message-ID`, which is stored on the case

   Sending is **blocked** when any of these is missing: the reason, the effective date, a field the reason needs, a document the reason needs, or the insurer's signed form when that insurer requires one. The database records are written first and the email is sent last, all in one transaction. If the provider fails, everything rolls back, the case stays DRAFT and a `SEND_FAILED` event is logged.
3. **Track.** Replies come in through the provider's inbound webhook. The app tries to match each one to a case, in this order:
   1. the plus address `cancellations+CXL000123@…` in To, Cc or the envelope recipient
   2. the reference `IMOTH-CXL-000123` in the subject
   3. `In-Reply-To` or `References` pointing at a Message-ID we sent (ours or the provider's)
   4. the policy number in the body, but only when exactly **one** open (SENT/QUERY) case has that number

   The app never guesses. Anything unmatched or ambiguous goes to the **Unmatched inbox**, where you link it to a case by hand or dismiss it. Every email and attachment is stored.
4. **Confirm.** You review each reply and mark it Confirmed, Rejected, Query or Information only. **Nothing is ever confirmed automatically.**

**Sender verification.** A matched reply is attached to its case only when both of these are true:

- the parsed `From` address is one of the insurer's known addresses: its cancellation email, its CC list, or an entry in *known sender addresses* (a full address or `@domain`)
- the receiving provider recorded an SPF or DMARC **pass** for it

A reply that fails either check goes to the Unmatched inbox, with the likely case shown as a suggestion. To link an unknown sender by hand, you have to tick "I have checked this sender".

Authentication results are read only from headers the provider writes:

- the **topmost** `Authentication-Results`, and only when its authserv-id equals `INBOUND_AUTHSERV_ID`
- Postmark's `Received-SPF`
- Mailgun's `X-Mailgun-Spf`

Any fail or softfail rejects the reply.

**Idempotency.** `case_email.message_id` is unique. When an email has no `Message-ID`, the key falls back to the provider's ID or a hash of the content. If the same email is delivered twice, the second copy is ignored and no duplicate documents or events are created.

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
$env:MAIL_DOMAIN = "example.com"
```

Then migrate, create a user, give them the role, and run:

```powershell
python manage.py migrate
python manage.py createsuperuser
python manage.py grant_cancellations_role <username>
python manage.py runserver
```

Open http://127.0.0.1:8000/admin/ and add at least one **Insurer** (name, cancellation email, CC list, known sender addresses, SLA hours, template). Then use the app at http://127.0.0.1:8000/.

To try inbound matching locally, POST a Postmark-style JSON payload to `http://127.0.0.1:8000/email/postmark/inbound/` with Basic auth matching `INBOUND_WEBHOOK_SECRET`. `cancellations/tests/test_idempotency.py` has an example payload.

## Running tests

```powershell
python manage.py test cancellations --settings=config.settings_test
```

The test settings use SQLite and the in-memory mail backend, so they need no environment variables and no network. To run the tests against Postgres, set `DATABASE_URL` first. The suite covers:

- each of the four matching methods, plus the unmatched and ambiguous cases
- sender verification, including spoofed display names and forged auth headers
- send-blocking validation
- idempotency, including a real Postmark webhook delivered twice
- status transitions (every disallowed pair is tested)
- the append-only trigger
- uploads and the role check

## Users and the cancellations role

- The app is open only to members of the **`cancellations`** group, which a migration creates. Superusers are **not** exempt.
- There is no second approver. The member who creates a case also sends and reviews it.
- Grant or revoke the role with `python manage.py grant_cancellations_role <username> [--revoke]`, or in the admin.
- The Django admin (`/admin/`) is for setting up insurers and covers. Cases, emails, documents and events are **read-only** there, so every change goes through the logged workflow.

---

## Email provider setup

The app works with **either** Postmark or Mailgun, chosen by `EMAIL_PROVIDER`. Both send through their API and receive replies through an inbound webhook that django-anymail parses. The webhook is protected by HTTP Basic auth (`INBOUND_WEBHOOK_SECRET="user:password"`). Mailgun webhooks are also signature-checked.

> **Recommended:** use a dedicated sub-domain such as `cxl.imoth.co.ke` for `MAIL_DOMAIN`. You can point its MX records at the provider without touching your normal company mailboxes.

### Postmark

1. Create a Server. Copy its **Server API token** into `POSTMARK_SERVER_TOKEN`.
2. **Sender Signatures → Add Domain** for `MAIL_DOMAIN`. Add the DKIM and Return-Path records it shows (see [DNS](#dns-records-spf-dkim-dmarc-mx)).
3. **Default Inbound Stream → Settings:**
   - **Inbound webhook URL:** `https://webhookuser:long-random-password@<your-app-host>/email/postmark/inbound/`. The credentials must match `INBOUND_WEBHOOK_SECRET`.
   - **Inbound domain:** `MAIL_DOMAIN`. Then add `MX 10 inbound.postmarkapp.com` for that domain.
   - Turn on "Include raw email content in JSON payload" if you can. Headers are more complete that way.
4. Send a test reply. In the stored email's headers (admin → Case emails), check for:
   - `Received-SPF`, which Postmark adds
   - an `Authentication-Results` header; if one is there, put the text before its first `;` into `INBOUND_AUTHSERV_ID`

### Mailgun

1. **Sending → Domains → Add domain** `MAIL_DOMAIN`, then add the SPF, DKIM and MX records it shows. Copy the **API key** into `MAILGUN_API_KEY` and the **HTTP webhook signing key** into `MAILGUN_WEBHOOK_SIGNING_KEY`. EU accounts also set `MAILGUN_API_URL=https://api.eu.mailgun.net/v3`.
2. **Receiving → Create route:**
   - Expression: `match_recipient("cancellations.*@<MAIL_DOMAIN>")`
   - Action: `forward("https://webhookuser:long-random-password@<your-app-host>/email/mailgun/inbound_mime/")` and `stop()`
3. Mailgun adds `X-Mailgun-Spf`, which the app trusts when `EMAIL_PROVIDER=mailgun`.

### Insurer configuration

For each insurer, set **known sender addresses** to every address or domain its claims and cancellations teams reply from, for example `@insurer.co.ke, cancellations@insurer-group.com`. A reply from anywhere else is never attached automatically.

---

## DNS records (SPF, DKIM, DMARC, MX)

These examples use `MAIL_DOMAIN=cxl.imoth.co.ke`. Take the exact values from your provider's dashboard.

| Purpose | Type | Host | Value |
|---|---|---|---|
| SPF (Postmark) | TXT | `cxl` | `v=spf1 include:spf.mtasv.net ~all`, or rely on the Return-Path CNAME below, which Postmark uses for SPF alignment |
| SPF (Mailgun) | TXT | `cxl` | `v=spf1 include:mailgun.org ~all` |
| DKIM | TXT | `<selector>._domainkey.cxl` | the public key shown by the provider (e.g. `20261123pm._domainkey`, `krs._domainkey`) |
| Return-Path (Postmark) | CNAME | `pm-bounces.cxl` | `pm.mtasv.net` |
| DMARC | TXT | `_dmarc.cxl` | `v=DMARC1; p=none; rua=mailto:dmarc-reports@imoth.co.ke; adkim=r; aspf=r` |
| Inbound (Postmark) | MX 10 | `cxl` | `inbound.postmarkapp.com` |
| Inbound (Mailgun) | MX 10 | `cxl` | `mxa.mailgun.org` (EU: `mxa.eu.mailgun.org`) |
| Inbound (Mailgun) | MX 10 | `cxl` | `mxb.mailgun.org` (EU: `mxb.eu.mailgun.org`) |

Notes:

- Publish **one** SPF record per host name. If one already exists, merge the `include:`s into it.
- Start DMARC at `p=none`. Once the reports show that all legitimate mail passes, move to `p=quarantine` and then `p=reject`. This protects insurers from spoofed cancellation requests that claim to come from you.
- Check the setup with the provider's "Verify DNS" button, and with `nslookup -type=TXT _dmarc.cxl.imoth.co.ke`.
- If you *must* use your main domain (`imoth.co.ke`) instead of a sub-domain, do **not** change its MX. Forward `cancellations+*` from your existing mailbox to the provider's inbound address. Plus-addressing has to survive that forwarding.

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
| `EMAIL_PROVIDER` | yes | `console` | `postmark`, `mailgun` or `console` |
| `MAIL_DOMAIN` | yes | `example.com` | Domain for Reply-To plus addresses and Message-IDs |
| `CANCELLATIONS_MAILBOX` | no | `cancellations` | Local part of the reply address |
| `DEFAULT_FROM_EMAIL` | no | `IMOTH Cancellations <cancellations@MAIL_DOMAIN>` | From header |
| `INBOUND_WEBHOOK_SECRET` | yes | – | `user:password` Basic auth on the inbound webhook |
| `INBOUND_AUTHSERV_ID` | no | – | authserv-id of your provider's `Authentication-Results` |
| `INBOUND_REQUIRE_SENDER_AUTH` | no | `true` | Require an SPF/DMARC pass before attaching a reply |
| `POSTMARK_SERVER_TOKEN` | Postmark | – | Server API token |
| `MAILGUN_API_KEY` | Mailgun | – | API key |
| `MAILGUN_SENDER_DOMAIN` | no | `MAIL_DOMAIN` | Mailgun sending domain |
| `MAILGUN_WEBHOOK_SIGNING_KEY` | Mailgun | – | Webhook signing key |
| `MAILGUN_API_URL` | no | US | `https://api.eu.mailgun.net/v3` for EU |
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
3. Edit `.do/app.yaml`: set the repo, the domain and your values. Then run `doctl apps create --spec .do/app.yaml`, or create the app in the UI with the same settings:
   - build: `python manage.py collectstatic --noinput`
   - run: `gunicorn config.wsgi --bind 0.0.0.0:8080 --workers 2`
   - a **pre-deploy job** that runs `python manage.py migrate --noinput`
   - a Managed Postgres database
4. Enter the secrets (`SECRET_KEY`, the provider token, `INBOUND_WEBHOOK_SECRET` and the Spaces keys) as encrypted env vars.
5. After the first deploy, open the console and run `python manage.py createsuperuser`, then `python manage.py grant_cancellations_role <username>`.
6. Point the provider's inbound webhook at `https://<app-host>/email/postmark/inbound/` (or `/email/mailgun/inbound_mime/`). The health check is at `/healthz`.

App Platform's filesystem is ephemeral, so production **must** set `AWS_STORAGE_BUCKET_NAME`.
