from django.urls import path
from . import views

urlpatterns = [
    path("jobs/<uuid:callback_id>/callback", views.job_callback, name="job_callback"),
]
