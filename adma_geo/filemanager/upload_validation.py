"""
Upload validation helpers.

MIME-type inspection for uploaded files using python-magic (libmagic).
Both views.py (session auth) and api_views.py (token auth) import from here.
"""

import logging
import magic

logger = logging.getLogger(__name__)

# Allowed MIME types — derived from settings ALL_SPATIAL_EXTENSIONS plus
# common document types. Extend here only; adding to the allowlist is the
# safe path forward (vs. relaxing validation at call sites).
ALLOWED_UPLOAD_MIME_TYPES = frozenset({
    # Spatial / GIS
    'application/zip',
    'application/octet-stream',  # shapefiles report as octet-stream
    'application/x-dbf',
    'image/tiff',
    'application/json',
    'application/geo+json',
    'application/vnd.geo+json',
    'application/xml',
    'text/xml',
    'application/vnd.google-earth.kml+xml',
    'application/vnd.google-earth.kmz',
    # Tabular / documents
    'text/csv',
    'application/csv',
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    # Images
    'image/jpeg',
    'image/png',
    'image/gif',
    'image/webp',
    # Plain text
    'text/plain',
    'text/markdown',
})


def validate_uploaded_file_mime(uploaded_file):
    """
    Inspect the actual bytes of an UploadedFile and return (ok, detected_mime).

    On reject, ok=False; the caller should refuse the upload with a generic
    message (do not echo detected_mime to the user — it is a fingerprinting
    vector).

    The file cursor is reset to position 0 after inspection so subsequent
    reads (e.g. by Django's storage backend) are unaffected.
    """
    head = uploaded_file.read(8192)
    uploaded_file.seek(0)
    detected = magic.from_buffer(head, mime=True)
    if detected not in ALLOWED_UPLOAD_MIME_TYPES:
        return False, detected
    return True, detected
