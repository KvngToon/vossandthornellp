"""Private storage for investor documents.

Production: a PRIVATE Supabase Storage bucket via its S3-compatible API. Files
are never public; downloads redirect to a signed URL that expires in a minute,
and only after the view has checked the file belongs to the signed-in investor.

Local development (no SUPABASE_S3_BUCKET): files go to PRIVATE_MEDIA_ROOT, which
is not under MEDIA_ROOT and so is never served by the /media/ route.
"""
from django.conf import settings
from django.core.files.storage import FileSystemStorage

SIGNED_URL_SECONDS = 60


def using_supabase():
    return bool(getattr(settings, 'SUPABASE_S3_BUCKET', ''))


def document_storage():
    if using_supabase():
        from storages.backends.s3 import S3Storage
        return S3Storage(
            bucket_name=settings.SUPABASE_S3_BUCKET,
            endpoint_url=settings.SUPABASE_S3_ENDPOINT,
            access_key=settings.SUPABASE_S3_ACCESS_KEY_ID,
            secret_key=settings.SUPABASE_S3_SECRET_ACCESS_KEY,
            region_name=settings.SUPABASE_S3_REGION,
            addressing_style='path',
            signature_version='s3v4',
            querystring_auth=True,
            querystring_expire=SIGNED_URL_SECONDS,
            default_acl=None,
            file_overwrite=False,
        )
    return FileSystemStorage(location=str(settings.PRIVATE_MEDIA_ROOT))
