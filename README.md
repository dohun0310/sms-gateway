# sms-gateway

ModemManager에 연결된 모뎀으로 문자를 받고 보내는 서비스입니다.

- **긴급 전달:** 긴급 암호로 시작하는 문자가 오면, 설정 파일의 연락처에 내용을 바로 전달합니다. 발신 번호는 제한하지 않으며 Hermes 없이 동작합니다.
- **로컬 API:** `allowed_uids`에 적은 사용자만 Unix 소켓으로 수신 조회, 초안 작성, 초안 발송을 할 수 있습니다. 문자 삭제나 모뎀 설정 변경 기능은 없습니다.

## 설치

```bash
sudo bash deploy/install.sh
sudoedit /etc/sms-gateway/config.toml   # 긴급 암호와 연락처 입력
sudo systemctl enable --now sms-gateway
journalctl -u sms-gateway -f
```

코드를 고친 뒤에도 같은 `install.sh`를 다시 실행하면 됩니다. 설정 파일은 덮어쓰지 않습니다.

## 사용

```bash
smsctl inbox                      # 받은 문자
smsctl draft 01012345678 "내용"    # 초안만 만들고 보내지 않음
smsctl send <id>                  # 초안을 한 번만 발송
smsctl get <id>
```

`smsctl`은 `/opt/sms-gateway/venv/bin/smsctl`에 설치됩니다.

## 발송 상태

| 상태 | 뜻 |
| --- | --- |
| `draft` | API로 만든 초안, 발송 전. 모뎀이 없어 넘기지 못한 경우에도 이 상태로 돌아옴 |
| `pending` | 긴급 전달 대기. 모뎀이 없으면 이 상태로 남아 있다가 모뎀이 돌아오면 발송 |
| `sending` | 모뎀에 넘김 |
| `sent` | 모뎀이 발송 완료를 보고함(상대방 수신 확인은 아님) |
| `failed` | 모뎀이 거부함, 다시 보내지 않음 |
| `unknown` | 60초 안에 응답이 없었거나 발송 중에 서비스가 멈춤, 자동으로 다시 보내지 않음 |

같은 초안을 여러 번 `send`해도 한 번만 나갑니다.

발송이 느리거나 멈춰도 수신 확인과 긴급 요청 기록은 계속 진행됩니다.

## 개발

```bash
uv venv --python /usr/bin/python3 .venv
uv pip install --python .venv/bin/python -e . pytest pytest-asyncio
.venv/bin/python -m pytest -q
```
