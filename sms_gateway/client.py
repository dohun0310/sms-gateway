"""Command-line client for the local API (standard library only)."""

from __future__ import annotations

import argparse
import json
import socket
import sys

DEFAULT_SOCKET = "/run/sms-gateway/sms.sock"


def call(socket_path: str, method: str, **params) -> object:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(120)
        sock.connect(socket_path)
        sock.sendall(json.dumps({"method": method, "params": params}).encode() + b"\n")
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    response = json.loads(data)
    if not response.get("ok"):
        raise SystemExit(f"error: {response.get('error')}")
    return response["result"]


def main() -> None:
    parser = argparse.ArgumentParser(prog="smsctl", description="SMS gateway client")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    sub = parser.add_subparsers(dest="command", required=True)
    inbox = sub.add_parser("inbox", help="list received messages")
    inbox.add_argument("--limit", type=int, default=20)
    draft = sub.add_parser("draft", help="create a draft; nothing is sent")
    draft.add_argument("number")
    draft.add_argument("text")
    for name, help_text in (("get", "show a message"), ("send", "send a draft once")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("id")
    args = parser.parse_args()

    params = {k: v for k, v in vars(args).items() if k not in ("socket", "command")}
    result = call(args.socket, args.command, **params)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
