import mimetypes
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import (
    CaseForm, CoverChoiceForm, DocumentForm, FollowUpForm, InsurerForm, LinkEmailForm, ReviewForm, SearchForm,
)
from .models import DEFAULT_EMAIL_TEMPLATE, CancellationCase, CaseDocument, CaseEmail, Insurer, MailboxState
from .permissions import cancellations_role_required
from .reasons import REASONS, DocType, doc_label
from .services.documents import ALLOWED_TYPES, save_uploaded_document
from .services.inbound import dismiss_email, link_email
from .services import portal
from .services.refunds import pro_rata_refund
from .services.sending import (
    SendBlocked, content_blockers, follow_up_defaults, render_email, send_blockers, send_case, send_follow_up,
)
from .services.workflow import S, TransitionError, close_case, close_if_complete, log_event, review_inbound_email

BLOCKING_STATUSES = (S.DRAFT, S.SENT, S.QUERY, S.CONFIRMED)


def _upload_limits():
    """Allowed file types and size, shown on upload controls (the server validates regardless)."""
    return {"allowed_ext": ", ".join(sorted(ALLOWED_TYPES)), "max_upload_mb": settings.MAX_UPLOAD_MB}


def _reason_meta():
    """Reason requirements for the form's show/hide script."""
    return {
        code: {"fields": list(r.required_fields), "documents": [doc_label(d) for d in r.required_documents]}
        for code, r in REASONS.items()
    }


OPEN_STATUSES = (S.DRAFT, S.SENT, S.QUERY)


def _month_start():
    now = timezone.localtime()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _unmatched_qs():
    return CaseEmail.objects.filter(direction=CaseEmail.Direction.INBOUND, case__isnull=True).exclude(
        classification=CaseEmail.Classification.DISMISSED)


def _to_review_qs():
    return CaseEmail.objects.filter(
        direction=CaseEmail.Direction.INBOUND, case__isnull=False, classification=CaseEmail.Classification.UNREVIEWED,
    ).exclude(case__status=S.CLOSED)


def search_cases(q):
    """Policy number, reference (IMOTH-CXL-000123 / 123) or client name."""
    cases = CancellationCase.objects.select_related("cover__insurer")
    q = (q or "").strip()
    if not q:
        return cases
    pk = CancellationCase.pk_from_reference(q.upper())
    filt = Q(cover__policy_number__icontains=q) | Q(cover__client_name__icontains=q)
    if pk:
        filt |= Q(pk=pk)
    elif q.isdigit():
        filt |= Q(pk=int(q))
    return cases.filter(filt)


@cancellations_role_required
def dashboard(request):
    q = request.GET.get("q", "").strip()
    if q:
        matches = list(search_cases(q)[:2])
        if len(matches) == 1:  # exact hit: go straight to the case
            return redirect(matches[0])
        return redirect(f"{reverse('cancellations:case_list')}?{urlencode({'q': q})}")
    cases = CancellationCase.objects.select_related("cover__insurer")
    month_start = _month_start()
    confirmed_month = cases.filter(confirmed_at__gte=month_start)
    missing_certificates = cases.filter(status=S.CONFIRMED).exclude(documents__type=DocType.CERTIFICATE)
    return render(request, "cancellations/dashboard.html", {
        "counts": {
            "open": cases.filter(status__in=OPEN_STATUSES).count(),
            "stale": cases.filter(is_stale=True, status=S.SENT).count(),
            "unmatched": _unmatched_qs().count(),
            "confirmed_month": confirmed_month.count(),
        },
        "month_label": month_start.strftime("%B %Y"),
        "refunds_month": confirmed_month.aggregate(total=Sum("refund_amount"))["total"],
        "currency": settings.CURRENCY,
        "to_review": _to_review_qs().select_related("case__cover")[:8],
        "to_review_count": _to_review_qs().count(),
        "stale_cases": cases.filter(is_stale=True, status=S.SENT).order_by("sent_at")[:8],
        "missing_certificates": missing_certificates[:8],
        "missing_certificates_count": missing_certificates.count(),
        "drafts": cases.filter(status=S.DRAFT).order_by("-created_at")[:8],
        "drafts_count": cases.filter(status=S.DRAFT).count(),
        "recent_confirmed": confirmed_month.order_by("-confirmed_at")[:10],
    })


@cancellations_role_required
def case_list(request):
    form = SearchForm(request.GET or None)
    cases = CancellationCase.objects.select_related("cover__insurer")
    if form.is_valid():
        cases = search_cases(form.cleaned_data["q"])
        if form.cleaned_data["status"]:
            cases = cases.filter(status=form.cleaned_data["status"])
        view = form.cleaned_data["view"]
        if view == "open":
            cases = cases.filter(status__in=OPEN_STATUSES)
        elif view == "stale":
            cases = cases.filter(is_stale=True, status=S.SENT)
        elif view == "confirmed_month":
            cases = cases.filter(confirmed_at__gte=_month_start()).order_by("-confirmed_at")
    if not cases.query.order_by:
        cases = cases.order_by("-updated_at", "-pk")  # most recently active first
    page_obj = Paginator(cases, 25).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "cancellations/case_list.html", {
        "form": form, "page_obj": page_obj, "cases": page_obj.object_list,
        "query_string": f"{params.urlencode()}&" if params else "",
        "filtered": any(request.GET.get(k) for k in ("q", "status", "view")),
        "unmatched_count": _unmatched_qs().count(), "to_review": _to_review_qs().count(),
    })


def _bind_create_forms(request):
    """Bind and validate the three create-form parts. Returns (forms, cover_or_None, error_or_None)."""
    cover_form = CoverChoiceForm(request.POST or None, prefix="cover")
    doc_form = DocumentForm(request.POST or None, request.FILES or None, prefix="doc")
    # The effective date starts as today (the day the request is made); staff can change it.
    case_form = CaseForm(request.POST or None, prefix="case", initial={"effective_date": timezone.localdate()})
    forms_ = {"cover_form": cover_form, "case_form": case_form, "doc_form": doc_form}
    if request.method != "POST":
        return forms_, None, None
    if not cover_form.is_valid():
        case_form.is_valid(), doc_form.is_valid()  # report every section's errors at once
        return forms_, None, None
    existing = cover_form.cleaned_data.get("existing_cover")
    cover = existing or _unsaved_cover(cover_form)
    forms_["case_form"] = case_form = CaseForm(request.POST, prefix="case", cover=cover)
    if existing and existing.cases.filter(status__in=BLOCKING_STATUSES).exists():
        open_case = existing.cases.filter(status__in=BLOCKING_STATUSES).first()
        return forms_, cover, f"{existing.policy_number} already has an open case {open_case.reference}."
    if not (case_form.is_valid() and doc_form.is_valid()):
        return forms_, cover, None
    return forms_, cover, ""  # "" = valid, no error


@cancellations_role_required
def case_create(request):
    forms_, cover, error = _bind_create_forms(request)
    if error:
        messages.error(request, error)
    elif error == "":
        cover_form, case_form, doc_form = forms_["cover_form"], forms_["case_form"], forms_["doc_form"]
        with transaction.atomic():
            cover = cover_form.get_or_create_cover()
            case = case_form.save(commit=False)
            case.cover = cover
            case.created_by = request.user
            case.save()
            log_event(case, "CREATED", request.user, "", S.DRAFT,
                      f"Draft created for {cover.policy_number} ({cover.insurer})")
            if doc_form.cleaned_data.get("file"):
                doc = save_uploaded_document(case, doc_form.cleaned_data["file"], doc_form.cleaned_data["doc_type"],
                                             doc_form.cleaned_data["source"], request.user)
                log_event(case, "DOCUMENT_ADDED", request.user, case.status, case.status, str(doc))
        if request.POST.get("action") != "send":
            messages.success(request, f"Saved {case.reference} as a draft.")
            return redirect(case)
        try:
            send_case(case, request.user)
        except SendBlocked as exc:
            messages.warning(request, f"Saved {case.reference} as a draft — not sent yet:")
            for problem in exc.problems:
                messages.error(request, problem)
        except Exception as exc:
            messages.error(request, f"Saved {case.reference} as a draft, but sending failed and nothing was sent: {exc}")
        else:
            messages.success(request, f"{case.reference} sent to {cover.insurer.cancellation_email}.")
        return redirect(case)
    return render(request, "cancellations/case_form.html", {
        **forms_, "reason_meta": _reason_meta(), "creating": True, **_upload_limits(),
        "effective_help": (f"Filled in with today ({timezone.localtime():%d %b %Y}, requested at {timezone.localtime():%H:%M}). "
                           "Change it if the cover should end on another day."),
        "portal_enabled": portal.enabled(), "portal_search_by": portal.SEARCH_BY,
    })


@cancellations_role_required
def portal_search(request):
    """JSON: certificate records from the Production Portal for the New Cancellation form (read-only)."""
    try:
        items = portal.search(request.GET.get("by", ""), request.GET.get("q", ""), request.user.email)
    except portal.PortalError as exc:
        return JsonResponse({"ok": False, "error": str(exc)}, status=200)
    return JsonResponse({"ok": True, "items": items})


@require_POST
@cancellations_role_required
def case_preview(request):
    """JSON preview for the create form's "Save & send": the exact email, or why it can't be sent."""
    forms_, cover, error = _bind_create_forms(request)
    if error is None or error:
        problems = [error] if error else _form_problems(forms_.values())
        return JsonResponse({"ok": False, "problems": problems or ["Check the form for errors."], "form_errors": True})
    case = forms_["case_form"].save(commit=False)
    case.cover = cover
    doc_type = forms_["doc_form"].cleaned_data.get("doc_type")
    has_file = request.POST.get("doc-has_file") == "1"
    problems = content_blockers(case, {doc_type} if (has_file and doc_type) else set())
    insurer = cover.insurer
    subject, body = render_email(case, reference="IMOTH-CXL-NNNNNN (number assigned on save)")
    return JsonResponse({
        "ok": not problems, "problems": problems, "to": insurer.cancellation_email,
        "cc": insurer.cc_addresses, "subject": subject, "body": body,
    })


def _form_problems(forms):
    """Flatten form errors into "Field: message" lines."""
    problems = []
    for form in forms:
        for field, errors in form.errors.items():
            label = "" if field == "__all__" else f"{form.fields[field].label or field.replace('_', ' ').capitalize()}: "
            problems.extend(f"{label}{e}" for e in errors)
    return problems


def _unsaved_cover(cover_form):
    from .models import Cover

    d = cover_form.cleaned_data
    return Cover(client_name=d.get("client_name") or "", policy_number=(d.get("policy_number") or "").strip(),
                 insurer=d.get("insurer"), asset_reference=d.get("asset_reference") or "", premium=d.get("premium"),
                 start_date=d.get("start_date"), end_date=d.get("end_date"),
                 certificate_number=(d.get("certificate_number") or "").strip())


@cancellations_role_required
def case_edit(request, pk):
    case = get_object_or_404(CancellationCase.objects.select_related("cover__insurer"), pk=pk)
    if case.status != S.DRAFT:
        messages.error(request, "Only drafts can be edited.")
        return redirect(case)
    form = CaseForm(request.POST or None, instance=case, prefix="case")
    if request.method == "POST" and form.is_valid():
        changed = ", ".join(form.changed_data) or "nothing"
        form.save()
        log_event(case, "UPDATED", request.user, case.status, case.status, f"Changed: {changed}")
        messages.success(request, "Draft updated.")
        return redirect(case)
    return render(request, "cancellations/case_form.html", {
        "case": case, "case_form": form, "reason_meta": _reason_meta(), "creating": False, **_upload_limits(),
    })


@cancellations_role_required
def case_detail(request, pk):
    case = get_object_or_404(CancellationCase.objects.select_related("cover__insurer", "created_by"), pk=pk)
    preview_subject, preview_body = render_email(case) if case.status == S.DRAFT else ("", "")
    present = set(case.documents.values_list("type", flat=True))
    return render(request, "cancellations/case_detail.html", {
        "case": case,
        "blockers": send_blockers(case) if case.status == S.DRAFT else [],
        "required_docs": [(doc_label(t), t in present) for t in case.required_document_types()],
        "documents": case.documents.all(),
        "emails": case.emails.prefetch_related("documents").order_by("received_at"),
        "events": case.events.select_related("actor"),
        "doc_form": DocumentForm(prefix="doc", required=True),
        "review_form": ReviewForm(),
        "preview_subject": preview_subject,
        "preview_body": preview_body,
        "can_close": case.status in (S.CONFIRMED, S.REJECTED, S.QUERY),
        "progress": _progress(case),
        **_upload_limits(),
        "follow_up_form": _follow_up_form(case) if case.status == S.QUERY else None,
        "requested_refund": (
            pro_rata_refund(case.cover.premium, case.cover.start_date, case.cover.end_date, case.effective_date)
            if case.effective_date and case.cover.can_calculate_refund else None
        ),
    })


def _progress(case):
    """Read-only progress tracker derived from the case status (no extra state)."""
    order = {S.DRAFT: 0, S.SENT: 1, S.QUERY: 2, S.CONFIRMED: 3, S.REJECTED: 3, S.CLOSED: 4}
    current = order[case.status]
    labels = ["Draft", "Sent to insurer", "Insurer replied", "Confirmed", "Closed"]
    not_confirmed = case.status == S.REJECTED or (case.status == S.CLOSED and not case.confirmed_at)
    if not_confirmed:
        labels[3] = "Rejected" if case.status == S.REJECTED else "Not confirmed"
    steps = []
    for i, label in enumerate(labels):
        if i == 3 and not_confirmed:
            state = "bad"
        elif i < current or case.status == S.CLOSED or (case.status == S.CONFIRMED and i == current):
            state = "done"
        elif i == current:
            state = "current"
        else:
            state = "todo"
        steps.append({"label": label, "state": state})
    return steps


def _follow_up_form(case):
    subject, body = follow_up_defaults(case)
    return FollowUpForm(case=case, initial={"subject": subject, "body": body})


@require_POST
@cancellations_role_required
def case_send(request, pk):
    case = get_object_or_404(CancellationCase, pk=pk)
    try:
        send_case(case, request.user)
    except SendBlocked as exc:
        for problem in exc.problems:
            messages.error(request, problem)
    except Exception as exc:  # provider error; case stays DRAFT and SEND_FAILED is logged
        messages.error(request, f"Sending failed and nothing was sent: {exc}")
    else:
        messages.success(request, f"{case.reference} sent to {case.cover.insurer.cancellation_email}.")
    return redirect(case)


@require_POST
@cancellations_role_required
def case_follow_up(request, pk):
    case = get_object_or_404(CancellationCase.objects.select_related("cover__insurer"), pk=pk)
    form = FollowUpForm(request.POST, request.FILES, case=case)
    if not form.is_valid():
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
        return redirect(case)
    data = form.cleaned_data
    if case.status != S.QUERY:
        messages.error(request, f"{case.reference} is {case.get_status_display()}; follow-ups answer an insurer query.")
        return redirect(case)
    # A new upload is kept on the case even if sending then fails (so it is not lost), and
    # send_follow_up logs SEND_FAILED itself, so no outer transaction here.
    docs = list(data["documents"])
    if data["new_file"]:
        new_doc = save_uploaded_document(case, data["new_file"], data["new_doc_type"], "staff", request.user)
        log_event(case, "DOCUMENT_ADDED", request.user, case.status, case.status, str(new_doc))
        docs.append(new_doc)
    try:
        send_follow_up(case, request.user, data["body"], docs, subject=data["subject"])
    except SendBlocked as exc:
        for problem in exc.problems:
            messages.error(request, problem)
    except Exception as exc:  # provider error: nothing sent, case stays QUERY
        messages.error(request, f"Sending failed and nothing was sent: {exc}")
    else:
        messages.success(request, f"Reply sent to the insurer; {case.reference} is SENT again and the SLA has restarted.")
    return redirect(case)


@require_POST
@cancellations_role_required
def document_upload(request, pk):
    case = get_object_or_404(CancellationCase, pk=pk)
    if case.status == S.CLOSED:
        messages.error(request, "Closed cases cannot be changed.")
        return redirect(case)
    form = DocumentForm(request.POST, request.FILES, prefix="doc", required=True)
    if form.is_valid():
        doc = save_uploaded_document(case, form.cleaned_data["file"], form.cleaned_data["doc_type"],
                                     form.cleaned_data["source"], request.user)
        log_event(case, "DOCUMENT_ADDED", request.user, case.status, case.status, str(doc))
        messages.success(request, f"Uploaded {doc.original_name}.")
        if doc.type == DocType.CERTIFICATE and close_if_complete(case, request.user):
            messages.success(request, f"{case.reference} is complete and has been closed.")
    else:
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
    return redirect(case)


@cancellations_role_required
def document_download(request, doc_id):
    doc = get_object_or_404(CaseDocument, pk=doc_id)
    try:
        fh = doc.file.open("rb")
    except FileNotFoundError:
        raise Http404("File missing from storage")
    content_type = doc.content_type or mimetypes.guess_type(doc.original_name)[0] or "application/octet-stream"
    inline = content_type in ("application/pdf", "image/png", "image/jpeg")
    response = FileResponse(fh, content_type=content_type, as_attachment=not inline, filename=doc.original_name)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    return response


@require_POST
@cancellations_role_required
def email_review(request, email_id):
    email = get_object_or_404(CaseEmail.objects.select_related("case"), pk=email_id, case__isnull=False)
    form = ReviewForm(request.POST, request.FILES)
    if form.is_valid():
        data = form.cleaned_data
        cert_doc = None
        if data["certificate_doc"]:
            cert_doc = email.documents.filter(pk=data["certificate_doc"]).first()
        try:
            case = review_inbound_email(
                email, data["classification"], request.user, data["note"],
                confirmed_effective_date=data["confirmed_effective_date"],
                certificate_doc=cert_doc, certificate_upload=data["certificate_file"],
            )
            case.refresh_from_db()
            msg = f"Reply recorded; case is now {case.get_status_display()}."
            if case.confirmed_at and case.status in (S.CONFIRMED, S.CLOSED):
                msg += " Cover cancelled" + (
                    f"; pro-rata refund {case.refund_amount}." if case.refund_amount is not None else ".")
                if case.status == S.CONFIRMED:
                    msg += " Upload the cancellation certificate / credit note to close it."
                else:
                    msg += " Certificate stored, so the case is closed."
            messages.success(request, msg)
        except TransitionError as exc:
            messages.error(request, str(exc))
    else:
        for errors in form.errors.values():
            for e in errors:
                messages.error(request, e)
    return redirect(email.case)


@require_POST
@cancellations_role_required
def case_close(request, pk):
    case = get_object_or_404(CancellationCase, pk=pk)
    try:
        close_case(case, request.user, request.POST.get("note", ""))
        messages.success(request, f"{case.reference} closed.")
    except TransitionError as exc:
        messages.error(request, str(exc))
    return redirect(case)


def _unmatched_inbox(request, email=None, form=None):
    """Two-panel inbox: the unmatched list, plus the selected email and its link form."""
    emails = (_unmatched_qs().select_related("suggested_case")
              .prefetch_related("documents").order_by("-received_at"))
    return render(request, "cancellations/unmatched_inbox.html", {
        "emails": emails, "email": email, "form": form,
        "documents": email.documents.all() if email else [],
        "mailboxes": MailboxState.objects.order_by("folder"),
    })


@cancellations_role_required
def unmatched_list(request):
    return _unmatched_inbox(request)


@cancellations_role_required
def unmatched_detail(request, email_id):
    email = get_object_or_404(CaseEmail, pk=email_id, direction=CaseEmail.Direction.INBOUND, case__isnull=True)
    initial = {"case": email.suggested_case_id} if email.suggested_case_id else None
    form = LinkEmailForm(request.POST or None, initial=initial)
    if request.method == "POST":
        if "dismiss" in request.POST:
            dismiss_email(email, request.user)
            messages.success(request, "Email dismissed.")
            return redirect("cancellations:unmatched_list")
        if form.is_valid():
            try:
                link_email(email, form.cleaned_data["case"], request.user, form.cleaned_data["sender_checked"])
            except ValueError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Linked to {form.cleaned_data['case'].reference}.")
                return redirect(form.cleaned_data["case"])
    return _unmatched_inbox(request, email, form)


@cancellations_role_required
def insurer_list(request):
    insurers = Insurer.objects.annotate(
        cover_count=Count("covers", distinct=True),
        open_count=Count("covers__cases", filter=Q(covers__cases__status__in=OPEN_STATUSES), distinct=True),
    ).order_by("-active", "name")
    return render(request, "cancellations/insurer_list.html", {"insurers": insurers})


def _insurer_form(request, insurer=None):
    form = InsurerForm(request.POST or None, instance=insurer)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, f"{saved.name} {'updated' if insurer else 'added'}.")
        return redirect("cancellations:insurer_list")
    return render(request, "cancellations/insurer_form.html", {
        "form": form, "insurer": insurer, "default_template": DEFAULT_EMAIL_TEMPLATE,
        "template_variables": InsurerForm.TEMPLATE_VARIABLES,
    })


@cancellations_role_required
def insurer_create(request):
    return _insurer_form(request)


@cancellations_role_required
def insurer_edit(request, pk):
    return _insurer_form(request, get_object_or_404(Insurer, pk=pk))
