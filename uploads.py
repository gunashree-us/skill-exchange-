import os
import secrets
from werkzeug.utils import secure_filename

from core import (
    BASE_DIR,
    CERTIFICATE_UPLOADS_DIR,
    PROFILE_UPLOADS_DIR,
    UPLOAD_CERTIFICATE_EXTENSIONS,
    UPLOAD_IMAGE_EXTENSIONS,
    execute_db,
    query_db,
)


def ensure_upload_dirs():
    # Keep profile asset folders ready before saving uploaded files.
    os.makedirs(PROFILE_UPLOADS_DIR, exist_ok=True)
    os.makedirs(CERTIFICATE_UPLOADS_DIR, exist_ok=True)


def file_extension(filename):
    # Normalize the file extension for validation and storage naming.
    return filename.rsplit('.', 1)[-1].lower() if filename and '.' in filename else ''


def is_previewable_image(filename):
    # Used by profile UI to decide whether to show an inline thumbnail.
    return file_extension(filename) in UPLOAD_IMAGE_EXTENSIONS


def read_file_header(file_storage, size=32):
    # Peek at the first bytes without disturbing the file pointer for later saving.
    stream = file_storage.stream
    current_position = stream.tell()
    stream.seek(0)
    header = stream.read(size)
    stream.seek(current_position)
    return header


def header_matches_extension(header, extension):
    # Accept only real PNG/JPEG/WebP/PDF file signatures, not renamed files.
    if not header:
        return False
    if extension == 'png':
        return header.startswith(b'\x89PNG\r\n\x1a\n')
    if extension in {'jpg', 'jpeg'}:
        return header.startswith(b'\xff\xd8\xff')
    if extension == 'webp':
        return len(header) >= 12 and header.startswith(b'RIFF') and header[8:12] == b'WEBP'
    if extension == 'pdf':
        return header.startswith(b'%PDF-')
    return False


def validate_uploaded_file_signature(file_storage, allowed_extensions, error_message):
    # Reject files whose content does not match the claimed extension.
    if not file_storage or not file_storage.filename:
        return
    cleaned_name = secure_filename(file_storage.filename)
    extension = file_extension(cleaned_name)
    if extension not in allowed_extensions:
        raise ValueError(error_message)
    header = read_file_header(file_storage)
    if not header_matches_extension(header, extension):
        raise ValueError(error_message)


def save_uploaded_file(file_storage, destination_dir, allowed_extensions):
    # Save a single uploaded file after validating extension and file signature.
    if not file_storage or not file_storage.filename:
        return None, None
    cleaned_name = secure_filename(file_storage.filename)
    extension = file_extension(cleaned_name)
    validate_uploaded_file_signature(file_storage, allowed_extensions, 'Upload a supported file type.')
    ensure_upload_dirs()
    stored_name = f"{secrets.token_hex(12)}.{extension}"
    absolute_path = os.path.join(destination_dir, stored_name)
    file_storage.stream.seek(0)
    file_storage.save(absolute_path)
    relative_path = os.path.relpath(absolute_path, BASE_DIR).replace(os.sep, '/')
    return cleaned_name, relative_path


def validate_profile_uploads(profile_photo_file=None, certificate_files=None):
    # Validate uploaded asset types before any profile fields are committed.
    if profile_photo_file and profile_photo_file.filename:
        validate_uploaded_file_signature(
            profile_photo_file,
            UPLOAD_IMAGE_EXTENSIONS,
            'Profile photo must be a real PNG, JPG, JPEG, or WebP image.',
        )
    for file_storage in certificate_files or []:
        if not file_storage or not file_storage.filename:
            continue
        validate_uploaded_file_signature(
            file_storage,
            UPLOAD_CERTIFICATE_EXTENSIONS,
            'Certificate files must be real PDF, PNG, JPG, JPEG, or WebP files.',
        )


def save_profile_assets(user_id, profile_photo_file=None, certificate_files=None):
    # Store optional profile photo and any number of certificate files.
    from core import delete_file_if_exists

    if profile_photo_file and profile_photo_file.filename:
        current_user = query_db('SELECT profile_photo_path FROM users WHERE id = ?', (user_id,), one=True)
        _original_name, photo_path = save_uploaded_file(
            profile_photo_file,
            PROFILE_UPLOADS_DIR,
            UPLOAD_IMAGE_EXTENSIONS,
        )
        execute_db(
            'UPDATE users SET profile_photo_path = ? WHERE id = ?',
            (photo_path, user_id),
        )
        delete_file_if_exists(current_user['profile_photo_path'] if current_user else '')
    uploaded_count = 0
    for file_storage in certificate_files or []:
        if not file_storage or not file_storage.filename:
            continue
        original_name, certificate_path = save_uploaded_file(
            file_storage,
            CERTIFICATE_UPLOADS_DIR,
            UPLOAD_CERTIFICATE_EXTENSIONS,
        )
        execute_db(
            'INSERT INTO profile_certificates (user_id, file_name, file_path) VALUES (?, ?, ?)',
            (user_id, original_name, certificate_path),
        )
        uploaded_count += 1
    return uploaded_count
