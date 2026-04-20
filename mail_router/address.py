from __future__ import annotations

import re

from .models import ParsedAddress

HASH_ID_RE = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$")
WEB_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def is_valid_ds_id(ds_id: str) -> bool:
    if not ds_id:
        return False
    if HASH_ID_RE.match(ds_id):
        return True
    if not WEB_ID_RE.match(ds_id):
        return False
    return "--" not in ds_id


def parse_recipient(email: str, allowed_domains: set[str]) -> ParsedAddress | None:
    """Parse <ds_id>[{+|--}mailbox]@<domain>. First + or -- wins.

    Returns None if the address is malformed, the domain is not allowed, or the
    resulting ds_id does not validate as hash-id / web-id.
    """
    if email.count("@") == 0:
        return None
    local_part, domain = email.rsplit("@", 1)
    domain = domain.lower()
    if domain not in allowed_domains:
        return None

    plus_idx = local_part.find("+")
    dashes_idx = local_part.find("--")
    separators = [i for i in (plus_idx, dashes_idx) if i >= 0]

    if not separators:
        ds_id = local_part
        mailbox: str | None = None
    else:
        sep_idx = min(separators)
        sep_len = 2 if sep_idx == dashes_idx and (plus_idx < 0 or dashes_idx < plus_idx) else 1
        ds_id = local_part[:sep_idx]
        mailbox = local_part[sep_idx + sep_len:] or None

    ds_id = ds_id.lower()
    if not is_valid_ds_id(ds_id):
        return None

    return ParsedAddress(ds_id=ds_id, mailbox=mailbox, domain=domain)
