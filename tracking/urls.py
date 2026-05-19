from django.urls import path
from . import views

app_name = 'tracking'

urlpatterns = [
    path('', views.home, name='home'),
    path('track/<str:tracking_number>/', views.track_shipment, name='track_shipment'),
    path('about/', views.about, name='about'),
]
