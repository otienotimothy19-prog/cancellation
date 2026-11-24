import logging

from django.contrib import messages
from django.contrib.auth import login
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .permissions import has_cancellations_role
from .services import sso

logger = logging.getLogger(__name__)


@csrf_exempt  # posted from the Production Portal; the signed, single-use pass is the protection
@never_cache
@require_POST
def portal_sign_in(request):
    """Sign in with a pass from the Production Portal (see services/sso.py)."""
    try:
        claims = sso.verify(request.POST.get("token", ""))
    except sso.SsoError as exc:
        logger.warning("Production Portal sign-in refused: %s", exc)
        return render(request, "registration/sso_problem.html", {"problem": str(exc)}, status=400)
    user, problem = sso.user_for(claims)
    if problem:
        logger.warning("Production Portal sign-in refused for %s: %s", claims["sub"], problem)
        return render(request, "registration/sso_problem.html", {"problem": problem}, status=403)
    if not has_cancellations_role(user):
        logger.info("Production Portal sign-in without access: %s", user.email)
        return render(request, "registration/sso_problem.html", {
            "problem": f"{user.email} does not have access to Cancellations yet. "
                       "Ask an administrator to add you to the cancellations group.",
        }, status=403)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    logger.info("Signed in from the Production Portal: %s", user.email)
    messages.success(request, "Signed in from the Production Portal.")
    return redirect("cancellations:dashboard")
