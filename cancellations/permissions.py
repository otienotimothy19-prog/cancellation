from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

ROLE_GROUP = "cancellations"


def has_cancellations_role(user):
    return bool(user and user.is_authenticated and user.is_active and user.groups.filter(name=ROLE_GROUP).exists())


def cancellations_role_required(view):
    """Only members of the "cancellations" group may use the app (superusers are not exempt)."""

    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not has_cancellations_role(request.user):
            raise PermissionDenied("You need the cancellations role.")
        return view(request, *args, **kwargs)

    return wrapper
