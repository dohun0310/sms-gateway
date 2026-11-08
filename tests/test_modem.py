import pytest

from sms_gateway.modem import CallTimeout, ModemError, ModemManagerModem, NotSent, SendTimeout


class Scripted(ModemManagerModem):
    """ModemManagerModem with D-Bus calls replaced by a script of results per member."""

    def __init__(self, results):
        super().__init__()
        self.results = results
        self.calls = []

    async def _modem_path(self):
        if isinstance(self.results.get("modem"), Exception):
            raise self.results["modem"]
        return "/modem/0"

    async def _call(self, path, interface, member, signature="", body=(), timeout=0):
        self.calls.append(member)
        result = self.results.get(member)
        if isinstance(result, Exception):
            raise result
        return result


async def test_send_deletes_the_outgoing_copy():
    m = Scripted({"Create": ["/sms/1"], "Send": [], "Delete": []})
    await m.send("01012345678", "hi")
    assert m.calls == ["Create", "Send", "Delete"]


@pytest.mark.parametrize("results", [
    {"modem": ModemError("no modem with SMS support")},
    {"Create": ModemError("Create: not authorized")},
    {"Create": OSError("bus gone")},
])
async def test_failures_before_send_are_not_sent(results):
    with pytest.raises(NotSent):
        await Scripted(results).send("01012345678", "hi")


async def test_send_timeout_leaves_message_alone():
    m = Scripted({"Create": ["/sms/1"], "Send": CallTimeout("Send: no answer within 60s")})
    with pytest.raises(SendTimeout):
        await m.send("01012345678", "hi")
    assert "Delete" not in m.calls


async def test_rejected_send_is_a_plain_failure():
    m = Scripted({"Create": ["/sms/1"], "Send": ModemError("Send: Unknown message error: 350"), "Delete": []})
    with pytest.raises(ModemError) as exc:
        await m.send("01012345678", "hi")
    assert not isinstance(exc.value, (NotSent, SendTimeout))
    assert m.calls[-1] == "Delete"
