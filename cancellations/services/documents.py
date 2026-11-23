"""File validation and storage for case documents (uploads and inbound attachments)."""

import os

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from ..models import CaseDocument

# extension -> (canonical content type, accepted leading "magic" byte signatures)
ALLOWED_TYPES = {
    "pdf": ("application/pdf", (b"%PDF-",)),
    "png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    "jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
    "tif": ("image/tiff", (b"II*\x00", b"MM\x00*")),
    "tiff": ("image/tiff", (b"II*\x00", b"MM\x00*")),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", (b"PK\x03\x04",)),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", (b"PK\x03\x04",)),
    "doc": ("application/msword", (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)),
}


def max_upload_bytes():
    return settings.MAX_UPLOAD_MB * 1024 * 1024


def validate_file(name, head_bytes, size):
    """Raise ValidationError unless the file has an allowed extension, matching content and size."""
    ext = os.path.splitext(name or "")[1].lower().lstrip(".")
    if ext not in ALLOWED_TYPES:
        allowed = ", ".join(sorted(ALLOWED_TYPES))
        raise ValidationError(f"“{name}”: file type .{ext or '?'} is not allowed (allowed: {allowed}).")
    if size <= 0:
        raise ValidationError(f"“{name}” is empty.")
    if size > max_upload_bytes():
        raise ValidationError(f"“{name}” is larger than {settings.MAX_UPLOAD_MB} MB.")
    content_type, signatures = ALLOWED_TYPES[ext]
    if not any(head_bytes.startswith(sig) for sig in signatures):
        raise ValidationError(f"“{name}”: contents do not look like a .{ext} file.")
    return content_type


def validate_uploaded_file(uploaded):
    uploaded.seek(0)
    head = uploaded.read(16)
    uploaded.seek(0)
    return validate_file(uploaded.name, head, uploaded.size)


def save_uploaded_document(case, uploaded, doc_type, source, user):
    content_type = validate_uploaded_file(uploaded)
    doc = CaseDocument(
        case=case, type=doc_type, source=source, uploaded_by=user,
        original_name=os.path.basename(uploaded.name)[:255], content_type=content_type, size=uploaded.size,
    )
    doc.file.save(uploaded.name, uploaded, save=False)
    doc.save()
    return doc


def save_bytes_document(*, case, email, name, data, doc_type, source):
    """Store raw bytes (an inbound attachment). Raises ValidationError if not acceptable."""
    content_type = validate_file(name, data[:16], len(data))
    doc = CaseDocument(
        case=case, email=email, type=doc_type, source=source,
        original_name=os.path.basename(name)[:255], content_type=content_type, size=len(data),
    )
    doc.file.save(name, ContentFile(data), save=False)
    doc.save()
    return doc
