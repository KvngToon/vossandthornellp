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


def contact(request):
    if request.method == 'POST':
        name         = request.POST.get('name', '').strip()
        organisation = request.POST.get('organisation', '').strip()
        email        = request.POST.get('email', '').strip()
        subject      = request.POST.get('subject', '').strip()
        message      = request.POST.get('message', '').strip()

        if name and organisation and email and subject and message:
            from tracking.emails import send_contact_enquiry_email
            sent = send_contact_enquiry_email(name, organisation, email, subject, message)
            return render(request, 'tracking/contact.html', {'sent': sent, 'failed': not sent})

        return render(request, 'tracking/contact.html', {'error': 'Please fill in all fields.'})

    return render(request, 'tracking/contact.html')
