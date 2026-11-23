import mimetypes

from django.contrib import messages
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import CaseForm, CoverChoiceForm, DocumentForm, LinkEmailForm, ReviewForm, SearchForm
from .models import CancellationCase, CaseDocument, CaseEmail
from .permissions import cancellations_role_required
from .reasons import REASONS, doc_label
from .services.documents import save_uploaded_document
from .services.inbound import dismiss_email, link_email
from .services.sending import SendBlocked, render_email, send_blockers, send_case
from .services.workflow import S, TransitionError, close_case, log_event, review_inbound_email

BLOCKING_STATUSES = (S.DRAFT, S.SENT, S.QUERY, S.CONFIRMED)


def _reason_meta():
    """Reason requirements for the form's show/hide script."""
    return {
        code: {"fields": list(r.required_fields), "documents": [doc_label(d) for d in r.required_documents]}
        for code, r in REASONS.items()
    }


@cancellations_role_required
def case_list(request):
    form = SearchForm(request.GET or None)
    cases = CancellationCase.objects.select_related("cover__insurer")
    if form.is_valid():
        q = form.cleaned_data["q"].strip()
        if q:
            pk = CancellationCase.pk_from_reference(q.upper())
            filt = Q(cover__policy_number__icontains=q) | Q(cover__client_name__icontains=q)
            if pk:
                filt |= Q(pk=pk)
            elif q.isdigit():
                filt |= Q(pk=int(q))
            cases = cases.filter(filt)
        if form.cleaned_data["status"]:
            cases = cases.filter(status=form.cleaned_data["status"])
    unmatched_count = CaseEmail.objects.filter(direction=CaseEmail.Direction.INBOUND, case__isnull=True).exclude(
        classification=CaseEmail.Classification.DISMISSED).count()
    to_review = CaseEmail.objects.filter(
        direction=CaseEmail.Direction.INBOUND, case__isnull=False, classification=CaseEmail.Classification.UNREVIEWED
    ).count()
    return render(request, "cancellations/case_list.html", {
        "form": form, "cases": cases[:200], "unmatched_count": unmatched_count, "to_review": to_review,
    })


@cancellations_role_required
def case_create(request):
    cover_form = CoverChoiceForm(request.POST or None, prefix="cover")
    doc_form = DocumentForm(request.POST or None, request.FILES or None, prefix="doc")
    case_form = CaseForm(request.POST or None, prefix="case")
    if request.method == "POST" and cover_form.is_valid():
        existing = cover_form.cleaned_data.get("existing_cover")
        case_form = CaseForm(request.POST, prefix="case", cover=existing or _unsaved_cover(cover_form))
        if existing and existing.cases.filter(status__in=BLOCKING_STATUSES).exists():
            open_case = existing.cases.filter(status__in=BLOCKING_STATUSES).first()
            messages.error(request, f"{existing.policy_number} already has an open case {open_case.reference}.")
        elif case_form.is_valid() and doc_form.is_valid():
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
            messages.success(request, f"Saved {case.reference} as a draft.")
            return redirect(case)
    return render(request, "cancellations/case_form.html", {
        "cover_form": cover_form, "case_form": case_form, "doc_form": doc_form, "reason_meta": _reason_meta(),
        "creating": True,
    })


def _unsaved_cover(cover_form):
    from .models import Cover

    d = cover_form.cleaned_data
    return Cover(start_date=d.get("start_date"), end_date=d.get("end_date"))


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
        "case": case, "case_form": form, "reason_meta": _reason_meta(), "creating": False,
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
    })


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
    form = ReviewForm(request.POST)
    if form.is_valid():
        try:
            review_inbound_email(email, form.cleaned_data["classification"], request.user, form.cleaned_data["note"])
            messages.success(request, f"Reply recorded; case is now {email.case.get_status_display()}.")
        except TransitionError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Choose an outcome for the reply.")
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


@cancellations_role_required
def unmatched_list(request):
    emails = CaseEmail.objects.filter(direction=CaseEmail.Direction.INBOUND, case__isnull=True).exclude(
        classification=CaseEmail.Classification.DISMISSED).select_related("suggested_case")
    return render(request, "cancellations/unmatched_list.html", {"emails": emails})


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
    return render(request, "cancellations/unmatched_detail.html", {
        "email": email, "form": form, "documents": email.documents.all(),
    })
