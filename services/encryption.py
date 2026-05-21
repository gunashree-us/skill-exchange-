import hashlib

from core import MAX_ENCRYPTED_MESSAGE_LENGTH, MAX_MESSAGE_LENGTH


def key_fingerprint(value):
    # Short fingerprint so users can identify device keys without exposing the full key.
    if not value:
        return "Unavailable"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest().upper()
    return ":".join(digest[index:index + 4] for index in range(0, 16, 4))


def is_encrypted_message_body(body):
    # Client-side encrypted chat payloads are stored as ciphertext strings with a stable prefix.
    return isinstance(body, str) and (body.startswith("enc::") or body.startswith("encauto::"))


def message_preview(body):
    # Never leak ciphertext into the thread list preview.
    if is_encrypted_message_body(body):
        return "Encrypted message. Unlock to read."
    return body or "Start the conversation"


def validate_message_body(raw_body):
    # Allow either plaintext chat text or browser-encrypted ciphertext envelopes.
    if raw_body is None:
        raise ValueError("Message is required.")
    body = str(raw_body).strip()
    if not body:
        raise ValueError("Message is required.")
    max_length = MAX_ENCRYPTED_MESSAGE_LENGTH if is_encrypted_message_body(body) else MAX_MESSAGE_LENGTH
    if len(body) > max_length:
        label = "Encrypted message" if is_encrypted_message_body(body) else "Message"
        raise ValueError(f"{label} must be {max_length} characters or fewer.")
    return body
