from django.shortcuts import render, redirect
from .models import Shipment


def home(request):
    if request.method == 'POST':
        tracking_number = request.POST.get('tracking_number', '').strip()
        if tracking_number:
            return redirect('tracking:track_shipment', tracking_number=tracking_number.upper())
    return render(request, 'tracking/home.html')


def track_shipment(request, tracking_number):
    try:
        shipment = Shipment.objects.get(tracking_number__iexact=tracking_number)
        events = shipment.events.order_by('-timestamp')
        return render(request, 'tracking/track.html', {
            'shipment': shipment,
            'events': events,
        })
    except Shipment.DoesNotExist:
        return render(request, 'tracking/track.html', {
            'error': True,
            'tracking_number': tracking_number,
        })


def about(request):
    return render(request, 'tracking/about.html')
