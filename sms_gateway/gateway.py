"""Gateway logic: record received messages, forward emergencies, send approved drafts."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from . import emergency
from .config import EmergencyConfig, validate_number
from .modem import Modem, ModemError, ReceivedSms
from .store import Inbound, Outbound, Store, inbound_key

log = logging.getLogger(__name__)

MAX_TEXT = 1000


class RequestError(ValueError):
    """A caller asked for something the gateway refuses to do."""


def mask(number: str) -> str:
    return number[:3] + "*" * max(len(number) - 7, 0) + number[-4:] if len(number) > 7 else "***"


class Gateway:
    def __init__(self, store: Store, modem: Modem, emergency_config: EmergencyConfig | None):
        self._store = store
        self._modem = modem
        self._emergency = emergency_config

    def recover(self) -> None:
        interrupted = self._store.mark_interrupted()
        if interrupted:
            log.warning("%d message(s) were being sent when the gateway stopped; marked unknown", interrupted)

    # receiving

    async def poll(self) -> None:
        for sms in await self._modem.received():
            self._record(sms)
        await self._dispatch()

    def _record(self, sms: ReceivedSms) -> None:
        key = inbound_key(sms.number, sms.timestamp, sms.text)
        if self._store.has_inbound(key):
            return

        kind, text, forwards = "normal", sms.text, []
        body = emergency.parse(sms.text, self._emergency.passphrase) if self._emergency else None
        if body is not None:
            assert self._emergency is not None
            text = body  # never store the passphrase
            if self._store.emergencies_since(timedelta(hours=1)) >= self._emergency.max_requests_per_hour:
                kind = "rate_limited"
            else:
                kind = "emergency"
                forward = emergency.forward_text(self._emergency.intro, body, sms.number)
                forwards = [(c.number, forward) for c in self._emergency.contacts]

        msg = Inbound(key=key, number=sms.number, text=text, sent_at=sms.timestamp,
                      received_at=datetime.now(UTC).isoformat(timespec="seconds"), kind=kind)
        if self._store.add_inbound(msg, forwards):
            log.info("received %s message from %s", kind, mask(sms.number))

    async def _dispatch(self) -> None:
        for out in self._store.pending_outbound():
            await self._send(out, from_state="pending")

        if not (self._emergency and self._emergency.reply_to_requester):
            return
        for msg in self._store.emergencies_awaiting_reply():
            sent, total = self._store.forward_results(msg.key)
            reply = self._store.add_outbound("emergency_reply", msg.number,
                                             emergency.reply_text(sent, total), "pending", msg.key)
            await self._send(reply, from_state="pending")

    async def _send(self, out: Outbound, from_state: str) -> Outbound:
        if not self._store.transition(out.id, from_state, "sending"):
            current = self._store.get_outbound(out.id)
            assert current is not None
            return current
        try:
            await self._modem.send(out.number, out.text)
        except ModemError as exc:
            self._store.transition(out.id, "sending", "failed", str(exc))
            log.error("%s %s to %s failed: %s", out.kind, out.id, mask(out.number), exc)
        else:
            self._store.transition(out.id, "sending", "sent")
            log.info("%s %s sent to %s", out.kind, out.id, mask(out.number))
        result = self._store.get_outbound(out.id)
        assert result is not None
        return result

    # API operations

    def inbox(self, limit: int = 20) -> list[Inbound]:
        return self._store.recent_inbound(max(1, min(limit, 200)))

    def create_draft(self, number: str, text: str) -> Outbound:
        try:
            number = validate_number(number)
        except ValueError as exc:
            raise RequestError(str(exc)) from None
        text = text.strip()
        if not text or len(text) > MAX_TEXT:
            raise RequestError(f"text must be 1-{MAX_TEXT} characters")
        return self._store.add_outbound("draft", number, text, "draft")

    def get(self, oid: str) -> Outbound:
        out = self._store.get_outbound(oid)
        if out is None:
            raise RequestError("no such message")
        return out

    async def send_draft(self, oid: str) -> Outbound:
        """Send a draft once. Calling again returns the recorded outcome instead of resending."""
        out = self.get(oid)
        if out.kind != "draft":
            raise RequestError("only drafts can be sent through the API")
        if out.state != "draft":
            return out
        return await self._send(out, from_state="draft")
