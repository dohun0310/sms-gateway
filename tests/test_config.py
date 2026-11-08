import pytest

from sms_gateway.config import ConfigError, load

BASE = """
allowed_uids = [1000]
[emergency]
passphrase = "블루베리42"
contacts = [{ name = "엄마", number = "010-1111-2222" }]
"""


def write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load(tmp_path):
    cfg = load(write(tmp_path, BASE))
    assert cfg.allowed_uids == {1000}
    assert cfg.emergency.contacts[0].number == "01011112222"
    assert cfg.emergency.reply_to_requester is True


def test_emergency_is_optional(tmp_path):
    assert load(write(tmp_path, "allowed_uids = [1000]\n")).emergency is None


@pytest.mark.parametrize("bad", [
    BASE.replace('"블루베리42"', '"CHANGE-ME"'),
    BASE.replace('"블루베리42"', '"짧음"'),
    BASE.replace('"블루베리42"', '"블루 베리42"'),
    BASE.replace('"010-1111-2222"', '"abc"'),
    BASE.replace("allowed_uids = [1000]", "allowed_uids = []"),
    BASE.replace('}]', '}, { name = "또", number = "01011112222" }]'),
])
def test_rejects_bad_config(tmp_path, bad):
    with pytest.raises(ConfigError):
        load(write(tmp_path, bad))
