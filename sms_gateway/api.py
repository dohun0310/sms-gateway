"""Local JSON-lines API on a Unix socket, restricted to configured uids via SO_PEERCRED."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
from dataclasses import asdict
from pathlib import Path

from .gateway import Gateway, RequestError

log = logging.getLogger(__name__)

MAX_REQUEST = 16 * 1024


def peer_uid(writer: asyncio.StreamWriter) -> int:
    sock = writer.get_extra_info("socket")
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


class Api:
    def __init__(self, gateway: Gateway, allowed_uids: frozenset[int]):
        self._gateway = gateway
        self._allowed = allowed_uids

    async def handle(self, request: dict) -> object:
        method, params = request.get("method"), request.get("params") or {}
        if not isinstance(params, dict):
            raise RequestError("params must be an object")
        match method:
            case "inbox":
                return [asdict(m) for m in self._gateway.inbox(int(params.get("limit", 20)))]
            case "draft":
                return asdict(self._gateway.create_draft(str(params["number"]), str(params["text"])))
            case "get":
                return asdict(self._gateway.get(str(params["id"])))
            case "send":
                return asdict(await self._gateway.send_draft(str(params["id"])))
            case _:
                raise RequestError(f"unknown method: {method}")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if peer_uid(writer) not in self._allowed:
                writer.write(b'{"ok": false, "error": "not allowed"}\n')
                return
            try:
                line = await reader.readline()
                response = {"ok": True, "result": await self.handle(json.loads(line))}
            except (RequestError, KeyError, ValueError, TypeError) as exc:
                response = {"ok": False, "error": str(exc)}
            writer.write(json.dumps(response, ensure_ascii=False).encode() + b"\n")
        finally:
            await writer.drain()
            writer.close()

    async def serve(self, path: Path) -> asyncio.Server:
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(self._client, path=str(path), limit=MAX_REQUEST + 1)
        os.chmod(path, 0o666)  # access is decided by the uid check, not file mode
        return server
