import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from sms_gateway import emergency
from sms_gateway.api import Api
from sms_gateway.client import call
from sms_gateway.config import Contact, EmergencyConfig
from sms_gateway.gateway import Gateway, RequestError
from sms_gateway.modem import ModemError, NotSent, ReceivedSms, SendTimeout
from sms_gateway.store import Store

PASS = "블루베리42"


class FakeModem:
    def __init__(self):
        self.inbox: list[ReceivedSms] = []
        self.sent: list[tuple[str, str]] = []
        self.fail_for: set[str] = set()
        self.raise_for: dict[str, Exception] = {}

    async def received(self):
        return list(self.inbox)

    async def send(self, number, text):
        if number in self.raise_for:
            raise self.raise_for[number]
        if number in self.fail_for:
            raise ModemError("rejected")
        self.sent.append((number, text))


def make(tmp_path, reply=True, limit=3):
    cfg = EmergencyConfig(
        passphrase=PASS,
        contacts=(Contact("엄마", "01011112222"), Contact("아빠", "01033334444")),
        intro="홍길동의 긴급 요청 전달 서비스입니다.",
        reply_to_requester=reply,
        max_requests_per_hour=limit,
    )
    store = Store(tmp_path / "sms.db")
    modem = FakeModem()
    return Gateway(store, modem, cfg), store, modem


def sms(text, number="01099998888", ts="2026-11-08T16:17:20+09"):
    return ReceivedSms(number=number, text=text, timestamp=ts)


# emergency rules

@pytest.mark.parametrize("text,expected", [
    (f"{PASS} 폰 잃어버림", "폰 잃어버림"),
    (f"  {PASS}\n여기 강남역", "여기 강남역"),
    (PASS, ""),
    (f"{PASS}x 폰", None),
    ("블루베리41 폰", None),
    ("안녕", None),
])
def test_parse(text, expected):
    assert emergency.parse(text, PASS) == expected


# receiving

async def test_normal_message_is_recorded_once(tmp_path):
    gw, store, modem = make(tmp_path)
    modem.inbox = [sms("반가워요")]
    await gw.poll()
    await gw.poll()
    inbox = gw.inbox()
    assert [(m.text, m.kind) for m in inbox] == [("반가워요", "normal")]
    assert modem.sent == []


async def test_emergency_forwards_to_contacts_then_replies(tmp_path):
    gw, store, modem = make(tmp_path)
    modem.inbox = [sms(f"{PASS} 폰을 잃어버렸어요. 이 번호로 연락 주세요")]
    await gw.poll()

    forward = "홍길동의 긴급 요청 전달 서비스입니다.\n\n폰을 잃어버렸어요. 이 번호로 연락 주세요\n\n회신 번호: 01099998888"
    assert modem.sent == [
        ("01011112222", forward),
        ("01033334444", forward),
        ("01099998888", "긴급 요청을 지정 연락처 2명 중 2명에게 전송했습니다."),
    ]
    stored = gw.inbox()[0]
    assert stored.kind == "emergency" and PASS not in stored.text

    await gw.poll()  # same message still on the modem
    assert len(modem.sent) == 3


async def test_reply_reports_failed_forwards(tmp_path):
    gw, store, modem = make(tmp_path)
    modem.fail_for = {"01011112222", "01033334444"}
    modem.inbox = [sms(f"{PASS} 도와줘")]
    await gw.poll()
    assert modem.sent == [("01099998888", "긴급 요청을 받았지만 전달에 실패했습니다. 다른 방법으로 연락해 주세요.")]


async def test_reply_can_be_disabled(tmp_path):
    gw, store, modem = make(tmp_path, reply=False)
    modem.inbox = [sms(f"{PASS} 도와줘")]
    await gw.poll()
    assert [n for n, _ in modem.sent] == ["01011112222", "01033334444"]


async def test_wrong_passphrase_sends_nothing(tmp_path):
    gw, store, modem = make(tmp_path)
    modem.inbox = [sms("블루베리 도와줘")]
    await gw.poll()
    assert modem.sent == [] and gw.inbox()[0].kind == "normal"


async def test_rate_limit(tmp_path):
    gw, store, modem = make(tmp_path, reply=False, limit=1)
    modem.inbox = [sms(f"{PASS} 첫번째", ts="t1"), sms(f"{PASS} 두번째", ts="t2")]
    await gw.poll()
    assert len(modem.sent) == 2  # only the first request was forwarded
    assert sorted(m.kind for m in gw.inbox()) == ["emergency", "rate_limited"]


async def test_interrupted_send_is_not_retried(tmp_path):
    gw, store, modem = make(tmp_path)
    # simulate a crash: the forward was handed to the modem but the outcome never recorded
    modem.inbox = [sms(f"{PASS} 도와줘")]
    original_send = modem.send

    async def crash(number, text):
        raise KeyboardInterrupt

    modem.send = crash
    with pytest.raises(KeyboardInterrupt):
        await gw.poll()

    modem.send = original_send
    restarted = Gateway(Store(tmp_path / "sms.db"), modem, gw._emergency)
    restarted.recover()
    await restarted.poll()
    # the interrupted forward is unknown and not resent; the other one and the reply go out
    assert [n for n, _ in modem.sent] == ["01033334444", "01099998888"]
    assert modem.sent[1][1] == "긴급 요청을 지정 연락처 2명 중 1명에게 전송했습니다."


async def test_old_emergency_gets_no_late_reply(tmp_path):
    gw, store, modem = make(tmp_path)
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    store._db.execute("INSERT INTO inbound VALUES ('k', '01099998888', 'x', 't', ?, 'emergency')", (old,))
    await gw.poll()
    assert modem.sent == []


# drafts

async def test_draft_is_sent_once(tmp_path):
    gw, store, modem = make(tmp_path)
    draft = gw.create_draft("010-1234-5678", "안녕하세요! 문자 수신 잘 됩니다.")
    assert draft.state == "draft" and modem.sent == []
    first = await gw.send_draft(draft.id)
    second = await gw.send_draft(draft.id)
    assert first.state == second.state == "sent"
    assert modem.sent == [("01012345678", "안녕하세요! 문자 수신 잘 됩니다.")]


async def test_failed_draft_is_not_resent(tmp_path):
    gw, store, modem = make(tmp_path)
    modem.fail_for = {"01012345678"}
    draft = gw.create_draft("01012345678", "hi")
    assert (await gw.send_draft(draft.id)).state == "failed"
    modem.fail_for = set()
    assert (await gw.send_draft(draft.id)).state == "failed"
    assert modem.sent == []


@pytest.mark.parametrize("number,text", [("abc", "hi"), ("01012345678", "  "), ("01012345678", "x" * 1001)])
def test_draft_validation(tmp_path, number, text):
    gw, _, _ = make(tmp_path)
    with pytest.raises(RequestError):
        gw.create_draft(number, text)


async def test_emergency_rows_cannot_be_sent_through_api(tmp_path):
    gw, store, modem = make(tmp_path, reply=False)
    modem.fail_for = {"01011112222"}
    modem.inbox = [sms(f"{PASS} 도와줘")]
    await gw.poll()
    failed = store._db.execute("SELECT id FROM outbound WHERE state = 'failed'").fetchone()[0]
    with pytest.raises(RequestError):
        await gw.send_draft(failed)


# slow or missing modem

async def test_timed_out_send_is_unknown_and_not_retried(tmp_path):
    gw, store, modem = make(tmp_path, reply=False)
    modem.raise_for = {"01011112222": SendTimeout("Send: no answer within 60s")}
    modem.inbox = [sms(f"{PASS} 도와줘")]
    await gw.poll()
    modem.raise_for = {}
    await gw.poll()
    assert [n for n, _ in modem.sent] == ["01033334444"]
    states = dict(store._db.execute("SELECT number, state FROM outbound").fetchall())
    assert states["01011112222"] == "unknown"


async def test_forward_waits_for_modem_and_is_sent_later(tmp_path):
    gw, store, modem = make(tmp_path)
    absent = NotSent("no modem with SMS support")
    modem.raise_for = {n: absent for n in ("01011112222", "01033334444", "01099998888")}
    modem.inbox = [sms(f"{PASS} 도와줘")]
    await gw.poll()
    assert modem.sent == []

    modem.raise_for = {}  # modem is back
    await gw.poll()
    assert [n for n, _ in modem.sent] == ["01011112222", "01033334444", "01099998888"]
    assert modem.sent[2][1] == "긴급 요청을 지정 연락처 2명 중 2명에게 전송했습니다."


async def test_receiving_continues_while_a_send_hangs(tmp_path):
    gw, store, modem = make(tmp_path, reply=False)
    release = asyncio.Event()
    original_send = modem.send

    async def slow_send(number, text):
        await release.wait()
        await original_send(number, text)

    modem.send = slow_send
    modem.inbox = [sms(f"{PASS} 첫번째", ts="t1")]
    await gw.record_received()
    dispatch = asyncio.create_task(gw.dispatch())
    await asyncio.sleep(0)  # dispatch is now stuck in the first send

    modem.inbox.append(sms("새 문자", ts="t2"))
    await gw.record_received()
    assert "새 문자" in [m.text for m in gw.inbox()]

    release.set()
    await dispatch
    assert len(modem.sent) == 2


async def test_draft_not_handed_to_network_can_be_sent_again(tmp_path):
    gw, store, modem = make(tmp_path)
    modem.raise_for = {"01012345678": NotSent("no modem with SMS support")}
    draft = gw.create_draft("01012345678", "hi")
    assert (await gw.send_draft(draft.id)).state == "draft"
    modem.raise_for = {}
    assert (await gw.send_draft(draft.id)).state == "sent"
    assert modem.sent == [("01012345678", "hi")]


# socket API

async def test_api_round_trip_and_uid_check(tmp_path):
    gw, store, modem = make(tmp_path)
    path = tmp_path / "s.sock"

    async def run(allowed):
        api = Api(gw, frozenset(allowed))
        server = await api.serve(path)
        async with server:
            return await asyncio.to_thread(lambda: _calls(str(path)))

    def _calls(p):
        draft = call(p, "draft", number="01012345678", text="테스트")
        sent = call(p, "send", id=draft["id"])
        return draft, sent, call(p, "inbox")

    draft, sent, inbox = await run({os.getuid()})
    assert draft["state"] == "draft" and sent["state"] == "sent" and inbox == []

    with pytest.raises(SystemExit, match="not allowed"):
        await run({os.getuid() + 1})
