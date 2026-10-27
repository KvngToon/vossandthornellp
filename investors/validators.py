import os

from django.core.exceptions import ValidationError

ALLOWED_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.docx', '.xlsx'}
MAX_BYTES = 10 * 1024 * 1024


def validate_document(upload):
    ext = os.path.splitext(upload.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        allowed = ', '.join(sorted(e.lstrip('.') for e in ALLOWED_EXTENSIONS))
        raise ValidationError(f'Unsupported file type. Allowed: {allowed}.')
    if upload.size > MAX_BYTES:
        raise ValidationError('File is too large (maximum 10 MB).')
