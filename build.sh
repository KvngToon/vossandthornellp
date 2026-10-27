#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --noinput

python manage.py migrate

# Seed sample shipments (skips automatically if data already exists)
python manage.py seed_data

# Add HSBC shipment (skips if already exists)
python manage.py add_hsbc_shipment

# Create the first superuser only when credentials are supplied explicitly.
# There are deliberately NO defaults: a built-in password would be a known
# credential on a site that holds investor data.
python manage.py shell -c "
from django.contrib.auth import get_user_model
import os
User = get_user_model()
username = os.environ.get('DJANGO_SUPERUSER_USERNAME')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')
email    = os.environ.get('DJANGO_SUPERUSER_EMAIL', '')
if not (username and password):
    print('DJANGO_SUPERUSER_USERNAME/PASSWORD not set — skipping superuser creation.')
elif User.objects.filter(username=username).exists():
    print(f'Superuser \"{username}\" already exists — skipping.')
else:
    User.objects.create_superuser(username, email, password)
    print(f'Superuser \"{username}\" created.')
"
