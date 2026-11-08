"""ModemManager access over the system D-Bus."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from dbus_fast import BusType, Message, MessageType, Variant
from dbus_fast.aio import MessageBus

MM = "org.freedesktop.ModemManager1"
MM_PATH = "/org/freedesktop/ModemManager1"
MESSAGING = "org.freedesktop.ModemManager1.Modem.Messaging"
SMS = "org.freedesktop.ModemManager1.Sms"

SMS_STATE_RECEIVED = 3
SMS_PDU_DELIVER = 1

CALL_TIMEOUT = 30.0
SEND_TIMEOUT = 60.0


class ModemError(RuntimeError):
    """The modem refused or failed an operation."""


class CallTimeout(ModemError):
    """ModemManager did not answer a D-Bus call in time."""


class NotSent(ModemError):
    """Failed before the message was handed to the network; safe to try again."""


class SendTimeout(ModemError):
    """No answer to Send in time; the message may or may not have gone out."""


@dataclass(frozen=True)
class ReceivedSms:
    number: str
    text: str
    timestamp: str


class Modem(Protocol):
    async def received(self) -> list[ReceivedSms]: ...

    async def send(self, number: str, text: str) -> None:
        """Raises NotSent, SendTimeout, or ModemError when the network rejected the message."""
        ...


class ModemManagerModem:
    """Uses the first modem that offers the Messaging interface, found again on every call
    so that a re-plugged modem or a ModemManager restart is picked up automatically."""

    def __init__(self) -> None:
        self._bus: MessageBus | None = None

    async def _connection(self) -> MessageBus:
        if self._bus is None or not self._bus.connected:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return self._bus

    async def _call(self, path: str, interface: str, member: str, signature: str = "", body=(),
                    timeout: float = CALL_TIMEOUT):
        bus = await self._connection()
        message = Message(destination=MM, path=path, interface=interface, member=member,
                          signature=signature, body=list(body))
        try:
            reply = await asyncio.wait_for(bus.call(message), timeout)
        except TimeoutError:
            raise CallTimeout(f"{member}: no answer within {timeout:.0f}s") from None
        if reply.message_type == MessageType.ERROR:
            raise ModemError(f"{member}: {reply.error_name}: {reply.body[0] if reply.body else ''}")
        return reply.body

    async def _modem_path(self) -> str:
        (objects,) = await self._call(MM_PATH, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        for path, interfaces in sorted(objects.items()):
            if MESSAGING in interfaces:
                return path
        raise ModemError("no modem with SMS support")

    async def _sms_properties(self, path: str) -> dict:
        (props,) = await self._call(path, "org.freedesktop.DBus.Properties", "GetAll", "s", [SMS])
        return {k: v.value for k, v in props.items()}

    async def received(self) -> list[ReceivedSms]:
        modem = await self._modem_path()
        (paths,) = await self._call(modem, MESSAGING, "List")
        result = []
        for path in paths:
            try:
                p = await self._sms_properties(path)
            except ModemError:
                continue  # deleted between List and GetAll
            if p.get("PduType") == SMS_PDU_DELIVER and p.get("State") == SMS_STATE_RECEIVED:
                result.append(ReceivedSms(number=p["Number"], text=p["Text"], timestamp=p["Timestamp"]))
        return result

    async def send(self, number: str, text: str) -> None:
        try:
            modem = await self._modem_path()
            (path,) = await self._call(
                modem, MESSAGING, "Create", "a{sv}",
                [{"number": Variant("s", number), "text": Variant("s", text)}],
            )
        except (ModemError, OSError) as exc:
            raise NotSent(str(exc)) from exc

        try:
            await self._call(path, SMS, "Send", timeout=SEND_TIMEOUT)
        except CallTimeout as exc:
            # ModemManager still owns the message and may finish sending it; leave it alone.
            raise SendTimeout(str(exc)) from exc
        except ModemError as exc:
            await self._delete(modem, path)
            raise
        await self._delete(modem, path)

    async def _delete(self, modem: str, path: str) -> None:
        try:
            await self._call(modem, MESSAGING, "Delete", "o", [path])
        except ModemError:
            pass  # the outgoing copy is only kept in ModemManager's memory
