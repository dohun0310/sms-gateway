"""ModemManager access over the system D-Bus."""

from __future__ import annotations

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


class ModemError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReceivedSms:
    number: str
    text: str
    timestamp: str


class Modem(Protocol):
    async def received(self) -> list[ReceivedSms]: ...

    async def send(self, number: str, text: str) -> None: ...


class ModemManagerModem:
    """Uses the first modem that offers the Messaging interface, found again on every call
    so that a re-plugged modem or a ModemManager restart is picked up automatically."""

    def __init__(self) -> None:
        self._bus: MessageBus | None = None

    async def _connection(self) -> MessageBus:
        if self._bus is None or not self._bus.connected:
            self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return self._bus

    async def _call(self, path: str, interface: str, member: str, signature: str = "", body=()):
        bus = await self._connection()
        reply = await bus.call(
            Message(destination=MM, path=path, interface=interface, member=member,
                    signature=signature, body=list(body))
        )
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
        modem = await self._modem_path()
        (path,) = await self._call(
            modem, MESSAGING, "Create", "a{sv}",
            [{"number": Variant("s", number), "text": Variant("s", text)}],
        )
        try:
            await self._call(path, SMS, "Send")
        finally:
            try:
                await self._call(modem, MESSAGING, "Delete", "o", [path])
            except ModemError:
                pass  # the outgoing copy is only kept in ModemManager's memory
