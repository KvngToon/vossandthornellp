from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from tracking.webhooks import resend_inbound

urlpatterns = [
    path('admin/', admin.site.urls),
    path('webhooks/resend/inbound/', resend_inbound, name='resend_inbound_webhook'),
    path('', include('tracking.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
