from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from django.conf import settings
from django.conf.urls.static import static
from tracking.webhooks import resend_inbound

urlpatterns = [
    # Browsers request /favicon.ico by default; point them at the static file.
    path('favicon.ico', RedirectView.as_view(url=settings.STATIC_URL + 'favicon.ico', permanent=True)),
    path('admin/', admin.site.urls),
    path('webhooks/resend/inbound/', resend_inbound, name='resend_inbound_webhook'),
    path('investors/', include('investors.urls')),
    path('', include('tracking.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
