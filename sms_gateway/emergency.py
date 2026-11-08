"""Pure rules for recognising an emergency request and composing the forwarded text."""

from __future__ import annotations

import hmac


def parse(text: str, passphrase: str) -> str | None:
    """Return the request body when ``text`` starts with the passphrase, else None.

    The passphrase must be followed by whitespace or the end of the message, so a longer
    word that merely starts with it does not match.
    """
    text = text.strip()
    head, rest = text[: len(passphrase)], text[len(passphrase) :]
    if not hmac.compare_digest(head.encode(), passphrase.encode()):
        return None
    if rest and not rest[0].isspace():
        return None
    return rest.strip()


def forward_text(intro: str, body: str, requester: str) -> str:
    return f"{intro}\n\n{body or '(내용 없음)'}\n\n회신 번호: {requester}"


def reply_text(sent: int, total: int) -> str:
    if sent == 0:
        return "긴급 요청을 받았지만 전달에 실패했습니다. 다른 방법으로 연락해 주세요."
    return f"긴급 요청을 지정 연락처 {total}명 중 {sent}명에게 전송했습니다."
