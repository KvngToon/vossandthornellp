#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --noinput

python manage.py migrate

# Seed sample shipments (skips automatically if data already exists)
python manage.py seed_data

# Create superuser if it doesn't exist
python manage.py shell -c "
from django.contrib.auth import get_user_model
import os
User = get_user_model()
username = os.environ.get('DJANGO_SUPERUSER_USERNAME', 'admin')
password = os.environ.get('DJANGO_SUPERUSER_PASSWORD', 'admin123')
email    = os.environ.get('DJANGO_SUPERUSER_EMAIL', 'admin@vossandthorne.com')
if not User.objects.filter(username=username).exists():
    User.objects.create_superuser(username, email, password)
    print(f'Superuser \"{username}\" created.')
else:
    print(f'Superuser \"{username}\" already exists — skipping.')
"
