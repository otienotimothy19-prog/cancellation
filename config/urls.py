from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.http import HttpResponse
from django.urls import include, path

from cancellations.sso_views import portal_sign_in

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("sso/portal/", portal_sign_in, name="portal_sign_in"),  # posted by the Production Portal
    # Inbound email webhooks (HTTP Basic auth via INBOUND_WEBHOOK_SECRET; Mailgun also signature-checked):
    #   /email/postmark/inbound/   /email/mailgun/inbound_mime/
    path("email/", include("anymail.urls")),
    path("healthz", lambda request: HttpResponse("ok", content_type="text/plain"), name="healthz"),
    path("", include("cancellations.urls")),
]
