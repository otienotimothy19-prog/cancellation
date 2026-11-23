"""Navigation data shared by every page: real counts only, for users with the role."""

from django.urls import resolve, Resolver404

from .models import CaseEmail
from .permissions import has_cancellations_role
from .services.workflow import S

NAV = [
    # (url name, label, icon, sections it covers)
    ("cancellations:dashboard", "Dashboard", "layout-dashboard", {"dashboard"}),
    ("cancellations:case_list", "Cases", "folder", {"case_list", "case_detail", "case_edit"}),
    ("cancellations:case_create", "New Cancellation", "file-plus", {"case_create"}),
    ("cancellations:unmatched_list", "Unmatched Inbox", "inbox", {"unmatched_list", "unmatched_detail"}),
]


def navigation(request):
    user = getattr(request, "user", None)
    if not has_cancellations_role(user):
        return {}
    try:
        current = resolve(request.path_info).url_name
    except Resolver404:
        current = ""
    unmatched = (CaseEmail.objects.filter(direction=CaseEmail.Direction.INBOUND, case__isnull=True)
                 .exclude(classification=CaseEmail.Classification.DISMISSED).count())
    to_review = (CaseEmail.objects.filter(direction=CaseEmail.Direction.INBOUND, case__isnull=False,
                                          classification=CaseEmail.Classification.UNREVIEWED)
                 .exclude(case__status=S.CLOSED).count())
    badges = {"cancellations:dashboard": to_review, "cancellations:unmatched_list": unmatched}
    return {
        "nav_items": [
            {"url": url, "label": label, "icon": icon, "active": current in names, "badge": badges.get(url, 0)}
            for url, label, icon, names in NAV
        ],
        "nav_unmatched": unmatched,
        "nav_to_review": to_review,
    }
