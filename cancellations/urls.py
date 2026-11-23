from django.urls import path

from . import exports, views

app_name = "cancellations"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("cases/", views.case_list, name="case_list"),
    path("cases/new/", views.case_create, name="case_create"),
    path("cases/<int:pk>/", views.case_detail, name="case_detail"),
    path("cases/<int:pk>/edit/", views.case_edit, name="case_edit"),
    path("cases/<int:pk>/send/", views.case_send, name="case_send"),
    path("cases/<int:pk>/close/", views.case_close, name="case_close"),
    path("cases/<int:pk>/follow-up/", views.case_follow_up, name="case_follow_up"),
    path("cases/<int:pk>/documents/", views.document_upload, name="document_upload"),
    path("documents/<int:doc_id>/", views.document_download, name="document_download"),
    path("emails/<int:email_id>/review/", views.email_review, name="email_review"),
    path("unmatched/", views.unmatched_list, name="unmatched_list"),
    path("unmatched/<int:email_id>/", views.unmatched_detail, name="unmatched_detail"),
    path("exports/confirmed-cancellations.csv", exports.confirmed_csv, name="confirmed_csv"),
    path("api/confirmed-cancellations/", exports.confirmed_api, name="confirmed_api"),
]
