"""Attachments: saving an email's files to disk, and building a reply draft that carries files.

Provider-agnostic: both services hand over the raw RFC 822 bytes (IMAP FETCH, Gmail
format=raw) and take back a stdlib message to store as a draft. Attachment bytes come
from strangers, so names are reduced to a safe basename and nothing is written outside
the destination folder or read from outside ~/Documents.
"""

import email as email_lib
import mimetypes
import os
import re
from email import policy
from email.message import EmailMessage as MimeMessage
from email.utils import formatdate, make_msgid

ATTACHMENTS_DIR = os.path.join("~", "Documents", "Jarvis", "Attachments")
# Files a draft may carry: the user's documents, never keys or configs elsewhere in home.
DRAFT_FILES_ROOT = os.path.join("~", "Documents")
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def parse_raw(raw: bytes) -> email_lib.message.Message:
    return email_lib.message_from_bytes(raw, policy=policy.default)


def attachment_parts(msg: email_lib.message.Message) -> list[tuple[str, bytes]]:
    """(filename, bytes) of each attached file, in message order."""
    out = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        if not name:
            continue
        data = part.get_payload(decode=True)
        if data is None:
            continue
        out.append((name, data))
    return out


def text_body(msg: email_lib.message.Message, max_chars: int) -> str:
    """The plain-text body (HTML stripped as a fallback), for the work the email asks for."""
    part = msg.get_body(preferencelist=("plain", "html")) if hasattr(msg, "get_body") else None
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_subtype() == "html":
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()[:max_chars]


def safe_name(name: str) -> str:
    """A basename with no path, control characters or leading dots."""
    name = os.path.basename(name.replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f/:*?\"<>|]", "_", name).strip().lstrip(".")
    return name[:150] or "attachment"


def save_attachments(msg: email_lib.message.Message, dest_dir: str) -> list[str]:
    """Write every attachment into dest_dir (made private); returns their absolute paths.

    An existing name gets " (2)", " (3)"… so nothing the user has is overwritten.
    """
    dest = os.path.realpath(os.path.expanduser(dest_dir))
    os.makedirs(dest, mode=0o700, exist_ok=True)
    saved = []
    for name, data in attachment_parts(msg):
        if len(data) > MAX_ATTACHMENT_BYTES:
            continue
        stem, ext = os.path.splitext(safe_name(name))
        path, n = os.path.join(dest, stem + ext), 1
        while True:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                n += 1
                path = os.path.join(dest, f"{stem} ({n}){ext}")
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        saved.append(path)
    return saved


def folder_for(subject: str, date_tag: str) -> str:
    """The attachments folder for one email: <date> <subject>, under ATTACHMENTS_DIR."""
    label = safe_name(f"{date_tag} {subject}".strip())[:80]
    return os.path.join(ATTACHMENTS_DIR, label)


def draft_file(path: str) -> str:
    """The real path of a file a draft may attach, or ValueError: under ~/Documents only."""
    root = os.path.realpath(os.path.expanduser(DRAFT_FILES_ROOT))
    real = os.path.realpath(os.path.expanduser(path))
    if os.path.commonpath([root, real]) != root:
        raise ValueError(f"{path} isn't in your Documents folder")
    if not os.path.isfile(real):
        raise ValueError(f"{path} doesn't exist")
    if os.path.getsize(real) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"{path} is too big to attach")
    return real


def build_reply_draft(
    original: email_lib.message.Message,
    sender: str,
    body: str,
    files: list[str],
) -> MimeMessage:
    """A reply to original from sender, threaded, with files attached (already checked)."""
    subject = str(original.get("Subject", "") or "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    msg = MimeMessage()
    if sender:
        msg["From"] = sender
    msg["To"] = str(original.get("Reply-To") or original.get("From") or "")
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    parent = str(original.get("Message-ID", "") or "")
    if parent:
        msg["In-Reply-To"] = parent
        refs = str(original.get("References", "") or "")
        msg["References"] = f"{refs} {parent}".strip()
    msg.set_content(body)
    for path in files:
        ctype, _ = mimetypes.guess_type(path)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=os.path.basename(path))
    return msg
