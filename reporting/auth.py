import hashlib

from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.http import HttpResponse


class RateLimitedLoginView(LoginView):
    template_name = "registration/login.html"
    max_attempts = 5
    window_seconds = 15 * 60

    def _cache_key(self):
        forwarded = self.request.META.get("HTTP_X_FORWARDED_FOR", "")
        ip = forwarded.split(",")[0].strip() if forwarded else self.request.META.get("REMOTE_ADDR", "unknown")
        username = self.request.POST.get("username", "").strip().lower()
        digest = hashlib.sha256(f"{ip}:{username}".encode("utf-8")).hexdigest()
        return f"login-attempts:{digest}"

    def post(self, request, *args, **kwargs):
        if cache.get(self._cache_key(), 0) >= self.max_attempts:
            return HttpResponse(
                "<h1>Connexion temporairement bloquée</h1><p>Patientez 15 minutes avant de réessayer.</p>",
                status=429,
                content_type="text/html; charset=utf-8",
            )
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        key = self._cache_key()
        cache.set(key, cache.get(key, 0) + 1, self.window_seconds)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete(self._cache_key())
        return super().form_valid(form)

