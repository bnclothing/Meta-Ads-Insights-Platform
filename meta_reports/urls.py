from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from reporting.auth import RateLimitedLoginView


urlpatterns = [
    path("admin/", admin.site.urls),
    path("connexion/", RateLimitedLoginView.as_view(), name="login"),
    path("deconnexion/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("reporting.urls")),
]
