# Google Health Ingestion 実装計画

> **エージェント実装時の必須事項:** この計画をタスク単位で実装する際は、`superpowers:subagent-driven-development`（推奨）または `superpowers:executing-plans` を使用する。進捗管理にはチェックボックス（`- [ ]`）を使用する。

**目標:** Google Health Webhookを安全に受信し、変更対象範囲をGoogle Health APIから取得して、論理取得範囲付きのImmutable RawとしてB2へ保存し、Loaderへ引き渡す最初のend-to-end ingestion pathを実装する。

**アーキテクチャ:** Webhook ReceiverはAuthorization・署名を検証して1 intervalごとにCloud Taskへ渡す。Fetch WorkerはGoogle Health APIの指定範囲を取得し、`source + data_type + operation + logical range + SHA-256`でRaw identityを作り、gzipしてB2へ保存する。Raw保存後にLoader Taskを発行する。初期対応data typeは`steps`、`heart-rate`、`sleep`とし、追加data typeは同じadapter契約で別タスクとして追加する。

**技術スタック:** Python, FastAPI, Pydantic, Google Tink, httpx, google-auth, google-cloud-tasks, boto3, Backblaze B2 S3-Compatible API, pytest, respx

**設計spec:** `docs/superpowers/specs/2026-08-20-personal-data-platform-design.md`

## 検証済みGoogle Health API contract

この実装計画では、現行APIについて以下のcontractを前提とする:

- 実際のWebhook通知には`204 No Content`で応答する。
- Subscriber endpoint検証では、認証あり・なしの`{"type":"verification"}` POST requestが送信される。
- 通知operationには`UPSERT`と`DELETE`がある。
- Webhook payloadには、変更データ取得に使うtime intervalが含まれる。
- Webhook署名は`GOOGLE-HEALTH-API-SIGNATURE`にBase64で入り、Raw request bodyそのものに対して検証する。
- Data Point一覧取得では、physical interval start、physical sample time、sleep session end timeによるfilterを使う。
- `pageSize`上限は通常10000、sleepは25。`nextPageToken`が空でなければresponseは未完了とみなす。

## 全体制約

- Google Health通常経路ではPollingしない。
- ReceiverはAPI fetch・B2 write・MotherDuck writeを行わない。
- Rawはlossless、gzip可逆圧縮、immutable。
- Hashは圧縮前の元response bytesへSHA-256を計算する。
- Google Health Raw keyには`logical range`、`observed_at`、`operation`を必ず含める。
- 同一logical rangeで直前観測とoperation + SHA-256が同じ場合だけ保存を省略する。状態変化後に同じbytesへ戻った場合は新しい`observed_at`で保存する。
- HTTP status、retry count、Cloud Task IDはB2へ永久保存しない。
- Webhook payloadは重複配信を前提にidempotentに処理する。
- 初期実装では`nextPageToken`が返ったら成功扱いにせず明示的に失敗させる。無言でpaginationを切り捨てない。

---

## ファイル構成

```text
src/personal_data_platform/
├─ receiver/
│  ├─ app.py
│  ├─ auth.py
│  ├─ models.py
│  └─ tasks.py
├─ fetch/
│  ├─ app.py
│  └─ handler.py
├─ sources/google_health/
│  ├─ client.py
│  ├─ credentials.py
│  ├─ filters.py
│  └─ models.py
├─ storage/
│  └─ b2.py
└─ shared/
   ├─ compression.py
   ├─ hashing.py
   └─ raw_identity.py

tests/
├─ unit/
│  ├─ test_raw_identity.py
│  ├─ test_google_health_filters.py
│  └─ test_webhook_models.py
├─ contract/
│  └─ test_google_health_webhook_contract.py
└─ integration/
   └─ test_b2_round_trip.py
```

### タスク1: Logical range・観測時刻・Raw identity

**対象ファイル:**
- 作成: `src/personal_data_platform/shared/hashing.py`
- 作成: `src/personal_data_platform/shared/compression.py`
- 作成: `src/personal_data_platform/shared/raw_identity.py`
- テスト: `tests/unit/test_raw_identity.py`

**インターフェース:**
- 提供: `sha256_hex(data: bytes) -> str`
- 提供: `gzip_bytes(data: bytes) -> bytes`
- 提供: `gunzip_bytes(data: bytes) -> bytes`
- 提供: `LogicalRange(kind: str, start: str, end: str)`
- 提供: `raw_scope_prefix(source: str, data_type: str, logical_range: LogicalRange) -> str`
- 提供: `raw_object_key(source, data_type, operation, logical_range, observed_at, raw_bytes, extension) -> str`

- [ ] **ステップ1: object keyの失敗テストを書く**

```python
from datetime import datetime, timezone

from personal_data_platform.shared.raw_identity import (
    LogicalRange,
    raw_object_key,
    raw_scope_prefix,
)

def test_google_health_delete_key_contains_scope_observation_and_hash():
    scope = LogicalRange(
        kind="physical",
        start="2026-08-20T00:00:00Z",
        end="2026-08-20T08:00:00Z",
    )
    observed_at = datetime(2026, 8, 20, 8, 50, 12, 123456, tzinfo=timezone.utc)

    key = raw_object_key(
        source="google_health",
        data_type="sleep",
        operation="DELETE",
        logical_range=scope,
        observed_at=observed_at,
        raw_bytes=b'{"dataPoints":[]}',
        extension="json",
    )

    assert key.startswith(
        "raw/google_health/sleep/physical/"
        "20260820T000000Z_20260820T080000Z/"
        "20260820T085012123456Z/DELETE/"
    )
    assert key.endswith(".json.gz")

def test_scope_prefix_excludes_observation_and_content():
    scope = LogicalRange("physical", "2026-08-20T00:00:00Z", "2026-08-20T08:00:00Z")
    assert raw_scope_prefix("google_health", "sleep", scope) == (
        "raw/google_health/sleep/physical/"
        "20260820T000000Z_20260820T080000Z/"
    )

def test_same_payload_after_later_observation_gets_a_new_key():
    scope = LogicalRange("physical", "2026-08-20T00:00:00Z", "2026-08-20T08:00:00Z")
    a = raw_object_key(
        "google_health", "sleep", "UPSERT", scope,
        datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc), b"{}", "json"
    )
    b = raw_object_key(
        "google_health", "sleep", "UPSERT", scope,
        datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc), b"{}", "json"
    )
    assert a != b
```

- [ ] **ステップ2: テストを実行して失敗を確認する**

```bash
pytest tests/unit/test_raw_identity.py -v
```

期待結果: import or function failure.

- [ ] **ステップ3: SHA-256とgzipを実装する**

`src/personal_data_platform/shared/hashing.py`:

```python
import hashlib

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
```

`src/personal_data_platform/shared/compression.py`:

```python
import gzip

def gzip_bytes(data: bytes) -> bytes:
    return gzip.compress(data, mtime=0)

def gunzip_bytes(data: bytes) -> bytes:
    return gzip.decompress(data)
```

- [ ] **ステップ4: scope prefixとobject keyを実装する**

`src/personal_data_platform/shared/raw_identity.py`:

```python
from dataclasses import dataclass
from datetime import datetime

from .hashing import sha256_hex

@dataclass(frozen=True)
class LogicalRange:
    kind: str
    start: str
    end: str

def _compact_rfc3339(value: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.strftime("%Y%m%dT%H%M%SZ")

def _compact_observed_at(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S%fZ")

def raw_scope_prefix(
    source: str,
    data_type: str,
    logical_range: LogicalRange,
) -> str:
    scope = (
        f"{_compact_rfc3339(logical_range.start)}_"
        f"{_compact_rfc3339(logical_range.end)}"
    )
    return f"raw/{source}/{data_type}/{logical_range.kind}/{scope}/"

def raw_object_key(
    source: str,
    data_type: str,
    operation: str,
    logical_range: LogicalRange,
    observed_at: datetime,
    raw_bytes: bytes,
    extension: str,
) -> str:
    digest = sha256_hex(raw_bytes)
    return (
        f"{raw_scope_prefix(source, data_type, logical_range)}"
        f"{_compact_observed_at(observed_at)}/{operation}/"
        f"{digest[:2]}/{digest}.{extension}.gz"
    )
```

- [ ] **ステップ5: gzip round-tripをテストする**

```python
from personal_data_platform.shared.compression import gzip_bytes, gunzip_bytes

def test_gzip_is_lossless():
    raw = b'{"dataPoints":[{"x":1}]}'
    assert gunzip_bytes(gzip_bytes(raw)) == raw
```

実行:

```bash
pytest tests/unit/test_raw_identity.py -v
```

期待結果: PASS.

- [ ] **ステップ6: commitする**

```bash
git add src/personal_data_platform/shared tests/unit/test_raw_identity.py
git commit -m "feat: define ordered raw observation identity"
```

### タスク2: Google Health Webhook contract

**対象ファイル:**
- 作成: `src/personal_data_platform/receiver/models.py`
- テスト: `tests/contract/test_google_health_webhook_contract.py`

**インターフェース:**
- 提供: `GoogleHealthNotification`
- 提供: `WebhookInterval`
- 提供: `FetchRequest`

- [ ] **ステップ1: 公式notification形式をcontract fixtureとして定義する**

```python
UPSERT = {
    "data": {
        "version": "1",
        "clientProvidedSubscriptionName": "personal-data",
        "healthUserId": "123",
        "operation": "UPSERT",
        "dataType": "steps",
        "intervals": [
            {
                "physicalTimeInterval": {
                    "startTime": "2026-03-08T01:29:00Z",
                    "endTime": "2026-03-08T01:34:00Z",
                }
            }
        ],
    }
}
```

Test:

```python
from personal_data_platform.receiver.models import GoogleHealthNotification

def test_parses_google_health_notification():
    msg = GoogleHealthNotification.model_validate(UPSERT)
    assert msg.data.operation == "UPSERT"
    assert msg.data.data_type == "steps"
    assert len(msg.data.intervals) == 1
```

- [ ] **ステップ2: 失敗を確認する**

```bash
pytest tests/contract/test_google_health_webhook_contract.py -v
```

- [ ] **ステップ3: alias付きPydantic modelを実装する**

```python
from pydantic import BaseModel, ConfigDict, Field

class PhysicalTimeInterval(BaseModel):
    start_time: str = Field(alias="startTime")
    end_time: str = Field(alias="endTime")
    model_config = ConfigDict(populate_by_name=True)

class WebhookInterval(BaseModel):
    physical_time_interval: PhysicalTimeInterval | None = Field(
        default=None, alias="physicalTimeInterval"
    )
    model_config = ConfigDict(populate_by_name=True)

class NotificationData(BaseModel):
    version: str
    client_provided_subscription_name: str = Field(alias="clientProvidedSubscriptionName")
    health_user_id: str = Field(alias="healthUserId")
    operation: str
    data_type: str = Field(alias="dataType")
    intervals: list[WebhookInterval]
    model_config = ConfigDict(populate_by_name=True)

class GoogleHealthNotification(BaseModel):
    data: NotificationData
```

`DELETE`もschemaを分岐させず同じmodelで受理する。

- [ ] **ステップ4: 未対応intervalのテストを追加する**

intervalが対応済みphysical/civil intervalのどちらでもない場合、parse自体は許可しても`FetchRequest`への変換時に`UnsupportedIntervalError`を送出する。推測して処理しない。

- [ ] **ステップ5: commitする**

```bash
git add src/personal_data_platform/receiver/models.py tests/contract
git commit -m "feat: model google health webhook payload"
```

### タスク3: Webhook Authorization・署名検証

**対象ファイル:**
- 作成: `src/personal_data_platform/receiver/auth.py`
- テスト: `tests/unit/test_receiver_auth.py`

**インターフェース:**
- 提供: `WebhookAuthenticator.verify_authorization(value: str | None) -> None`
- 提供: `WebhookSignatureVerifier.verify(raw_body: bytes, signature_b64: str) -> None`

- [ ] **ステップ1: Authorizationのテストを書く**

```python
import pytest
from personal_data_platform.receiver.auth import AuthorizationError, verify_authorization

def test_correct_authorization_is_accepted():
    verify_authorization("Bearer abc", "Bearer abc")

def test_missing_authorization_is_rejected():
    with pytest.raises(AuthorizationError):
        verify_authorization(None, "Bearer abc")
```

- [ ] **ステップ2: 失敗を確認する**

```bash
pytest tests/unit/test_receiver_auth.py -v
```

- [ ] **ステップ3: 定数時間のAuthorization比較を実装する**

`endpointAuthorization.secret`の比較には`hmac.compare_digest`を使用し、通常の文字列等価比較を使わない。

- [ ] **ステップ4: Tink公開鍵署名検証を追加する**

実装contract:

```python
class WebhookSignatureVerifier:
    PUBLIC_KEYSET_URL = (
        "https://www.gstatic.com/googlehealthapi/webhooks/"
        "webhooks_public_keyset.json"
    )

    async def verify(self, raw_body: bytes, signature_b64: str) -> None:
        ...
```

実装では以下を行う:

1. Base64-decode `GOOGLE-HEALTH-API-SIGNATURE`.
2. Fetch/cache the official public Tink keyset.
3. Register Tink signature primitives.
4. Parse the keyset with `tink.json_proto_keyset_format.parse_without_secret`.
5. Obtain `signature.PublicKeyVerify`.
6. Call `verify(decoded_signature, raw_body)`.
7. Raise `SignatureVerificationError` on any verification failure.

署名検証前にJSONを再serializeしない。HTTPのRaw bodyそのものを検証する。

- [ ] **ステップ5: Dependency InjectionでverifierをUnit Testする**

Inject a fake primitive:

```python
class FakeVerifier:
    def verify(self, signature: bytes, data: bytes) -> None:
        assert signature == b"sig"
        assert data == b'{"data":{}}'
```

Unit Testはofflineで完結させ、実keysetの確認は必要に応じてIntegration Testで行う。

- [ ] **ステップ6: commitする**

```bash
git add src/personal_data_platform/receiver/auth.py tests/unit/test_receiver_auth.py
git commit -m "feat: verify google health webhook authenticity"
```

### タスク4: Receiver → Cloud Tasks

**対象ファイル:**
- 作成: `src/personal_data_platform/receiver/tasks.py`
- 作成: `src/personal_data_platform/receiver/app.py`
- 変更: `src/personal_data_platform/entrypoint.py`
- テスト: `tests/unit/test_receiver_app.py`

**インターフェース:**
- 提供: `POST /webhooks/google-health`
- 提供: one `FetchRequest` task per notification interval
- 使用: Cloud Tasks queue `google-health-fetch`

- [ ] **ステップ1: endpoint verification handshakeをテストする**

必須の2ケースをテストする:

```python
def test_authorized_verification_returns_200(client):
    response = client.post(
        "/webhooks/google-health",
        headers={"Authorization": "Bearer expected"},
        json={"type": "verification"},
    )
    assert response.status_code == 200

def test_unauthorized_verification_returns_401(client):
    response = client.post("/webhooks/google-health", json={"type": "verification"})
    assert response.status_code == 401
```

- [ ] **ステップ2: 実通知でintervalごとにTaskを1件作成し204を返すことをテストする**

fake task clientを使い、responseが`204 No Content`であることと、serializeされたbodyが以下を含むことを確認する:

```json
{
  "health_user_id": "123",
  "data_type": "steps",
  "operation": "UPSERT",
  "range_kind": "physical",
  "start": "2026-03-08T01:29:00Z",
  "end": "2026-03-08T01:34:00Z"
}
```

- [ ] **ステップ3: Cloud Tasks作成処理を実装する**

private Cloud Run `health-fetch` URLをtargetにする際はOIDCを使用する。

Task bodyはUTF-8 JSONとする。

notification bytes + interval indexから決定的Hash Task IDを生成する。Cloud Tasksが`ALREADY_EXISTS`を返した場合は、重複排除成功として扱う。

- [ ] **ステップ4: FastAPI appをentrypointへ接続する**

`webhook` roleでは以下を実行する:

```python
uvicorn.run("personal_data_platform.receiver.app:app", host="0.0.0.0", port=port)
```

Cloud Runから渡される`PORT`を使用する。

- [ ] **ステップ5: テストを実行する**

```bash
pytest tests/unit/test_receiver_app.py tests/contract/test_google_health_webhook_contract.py -v
```

- [ ] **ステップ6: commitする**

```bash
git add src/personal_data_platform/receiver src/personal_data_platform/entrypoint.py tests
git commit -m "feat: queue verified google health notifications"
```

### タスク5: Google Health API client

**対象ファイル:**
- 作成: `src/personal_data_platform/sources/google_health/credentials.py`
- 作成: `src/personal_data_platform/sources/google_health/filters.py`
- 作成: `src/personal_data_platform/sources/google_health/client.py`
- テスト: `tests/unit/test_google_health_filters.py`
- テスト: `tests/unit/test_google_health_client.py`

**インターフェース:**
- 提供: `GoogleHealthClient.fetch(request: FetchRequest) -> bytes`
- Initial supported data types: `steps`, `heart-rate`, `sleep`

- [ ] **ステップ1: filter生成をテストする**

```python
def test_steps_filter_uses_interval_start_time():
    assert build_filter(
        "steps",
        "2026-08-20T00:00:00Z",
        "2026-08-20T01:00:00Z",
    ) == (
        'steps.interval.start_time >= "2026-08-20T00:00:00Z" AND '
        'steps.interval.start_time < "2026-08-20T01:00:00Z"'
    )
```

以下についても対応する正確なテストを追加する:

```text
heart-rate → heart_rate.sample_time.physical_time
sleep      → sleep.interval.end_time
```

sleep queryでは、対応filterがsession end time基準のためend timeを使用する。

- [ ] **ステップ2: 明示的なfilter mapを実装する**

すべてのdata typeを汎用推論で処理しない。

```python
FILTER_FIELDS = {
    "steps": "steps.interval.start_time",
    "heart-rate": "heart_rate.sample_time.physical_time",
    "sleep": "sleep.interval.end_time",
}
```

上記mapを初期対応contractとし、未対応data typeでは`UnsupportedDataTypeError`を送出する。

- [ ] **ステップ3: OAuth refresh credentialを実装する**

以下から`google.oauth2.credentials.Credentials`を構築する:

```text
GOOGLE_HEALTH_CLIENT_ID
GOOGLE_HEALTH_CLIENT_SECRET
GOOGLE_HEALTH_REFRESH_TOKEN
```

credentialが無効または期限切れの場合はrequest前にrefreshする。

- [ ] **ステップ4: list API呼び出しを実装する**

Request:

```text
GET https://health.googleapis.com/v4/users/me/dataTypes/<data-type>/dataPoints
```

Parameters:

```python
{
    "pageSize": 10000,  # sleep adapter uses 25
    "filter": build_filter(...),
}
```

`response.content`を変更せずそのまま返す。

- [ ] **ステップ5: paginationが必要なら明示的に失敗させる**

`nextPageToken`確認に必要な最小限だけJSONをparseした後:

```python
if payload.get("nextPageToken"):
    raise PaginationRequiredError(request.data_type, request.start, request.end)
```

途中pageを成功したsnapshotとして保存しない。

- [ ] **ステップ6: テストを実行する**

```bash
pytest tests/unit/test_google_health_filters.py tests/unit/test_google_health_client.py -v
```

- [ ] **ステップ7: commitする**

```bash
git add src/personal_data_platform/sources/google_health tests/unit
git commit -m "feat: fetch bounded google health ranges"
```

### タスク6: B2 Raw観測repository

**対象ファイル:**
- 作成: `src/personal_data_platform/storage/b2.py`
- テスト: `tests/unit/test_b2_repository.py`
- テスト: `tests/integration/test_b2_round_trip.py`

**インターフェース:**
- 提供: `RawObservationRef(key: str, observed_at: datetime, operation: str, sha256: str)`
- 提供: `B2RawRepository.latest_observation(scope_prefix: str) -> RawObservationRef | None`
- 提供: `B2RawRepository.store_if_changed(identity, raw_bytes: bytes) -> str | None`
- 提供: `B2RawRepository.get_raw(key: str) -> bytes`
- 提供: `B2RawRepository.list_raw(prefix: str = "raw/") -> list[RawObservationRef]`

- [ ] **ステップ1: 連続重複排除をUnit Testする**

```python
def test_same_observation_as_latest_is_skipped():
    repo = make_repo(
        latest=RawObservationRef(
            key="raw/.../20260820T080000000000Z/UPSERT/ab/abc.json.gz",
            observed_at=OBSERVED_A,
            operation="UPSERT",
            sha256="abc",
        )
    )
    stored = repo.store_if_changed(identity(operation="UPSERT", sha256="abc"), b"payload")
    assert stored is None
    repo.client.put_object.assert_not_called()
```

- [ ] **ステップ2: `A → B → A`が保持されることをUnit Testする**

```python
def test_return_to_old_content_is_stored_as_new_observation():
    repo = make_repo(
        latest=RawObservationRef(
            key="raw/.../20260820T090000000000Z/UPSERT/de/def.json.gz",
            observed_at=OBSERVED_B,
            operation="UPSERT",
            sha256="def",
        )
    )
    stored = repo.store_if_changed(identity(operation="UPSERT", sha256="abc"), b"payload-a")
    assert stored is not None
    assert "abc" in stored
    repo.client.put_object.assert_called_once()
```

B2 replayで`A → B → A`の状態変化を再構築するために必須とする。

- [ ] **ステップ3: S3-Compatible API経由でB2を実装する**

Configure boto3 with:

```text
B2_ENDPOINT
B2_KEY_ID
B2_APPLICATION_KEY
B2_BUCKET
```

Algorithm:

```text
scope_prefixをListObjectsV2
→ object key内のobserved_atで最新を選ぶ
→ latest.operation + latest.sha256 と今回を比較
   ├─ 同じ → 保存しない
   └─ 違う → 新しいobserved_at付きkeyへput_object
```

upload bodyには`gzip_bytes(raw_bytes)`を使用する。

object tagやcustom metadataは使用しない。

- [ ] **ステップ4: object keyから観測fieldをparseする**

`RawObservationRef`は以下をobject keyから復元できるようにする:

```text
logical scope
observed_at
operation
sha256
```

from the key without reading the body.

- [ ] **ステップ5: 専用test prefixでround tripを検証する**

使用するprefix:

```text
integration-test/<uuid>
```

以下を検証する:

```text
A store → stored
A consecutive store → skipped
B store → stored
A store again → stored with a later observed_at
```

保存した各objectをdownloadしてgunzipし、SHA-256とbytesが一致することを確認する。

削除対象は生成したintegration-test prefix配下だけに限定する。

- [ ] **ステップ6: commitする**

```bash
git add src/personal_data_platform/storage/b2.py tests
git commit -m "feat: persist ordered raw observations to b2"
```

### タスク7: Fetch Worker → B2 → Loader Task

**対象ファイル:**
- 作成: `src/personal_data_platform/fetch/handler.py`
- 作成: `src/personal_data_platform/fetch/app.py`
- 変更: `src/personal_data_platform/entrypoint.py`
- テスト: `tests/unit/test_fetch_handler.py`

**インターフェース:**
- 使用: `FetchRequest`
- 提供: B2 object key
- 提供: Loader task `{ "object_key": "<key>" }`

- [ ] **ステップ1: 新規Raw保存成功をテストする**

call順序を確認する:

```text
Google Health fetch
→ key build
→ B2 store_if_changed
→ enqueue Loader
```

- [ ] **ステップ2: 完全重複をテストする**

B2の`store_if_changed`が`None`を返した場合は204を返し、新しいLoader Taskを作成しない。

- [ ] **ステップ3: handlerを実装する**

handlerでは分析用data point parseを行わない。

fetch成功判定前に`nextPageToken`がないことを確認する目的に限りresponseをparseしてよい。

- [ ] **ステップ4: private Cloud Run endpointを公開する**

Create `POST /tasks/fetch/google-health`.

Cloud Tasks以外からの呼び出しはCloud Run IAMで拒否する。application code側でTask headerをidentityとして信用しない。

- [ ] **ステップ5: Ingestionテストを実行する**

```bash
pytest tests/unit tests/contract -v
```

- [ ] **ステップ6: commitする**

```bash
git add src/personal_data_platform/fetch src/personal_data_platform/entrypoint.py tests
git commit -m "feat: complete google health raw ingestion"
```

### タスク8: Google Health subscriber設定command

**対象ファイル:**
- 作成: `scripts/configure_google_health_subscriber.py`
- テスト: `tests/unit/test_subscriber_config.py`

**インターフェース:**
- 使用: deployed webhook URL, project number, endpoint authorization secret
- 提供: Google Health subscriber configured for `steps`, `heart-rate`, `sleep`

- [ ] **ステップ1: subscriber requestの正確なテストを書く**

期待するrequest body:

```json
{
  "endpointUri": "https://<health-webhook-url>/webhooks/google-health",
  "subscriberConfigs": [
    {
      "dataTypes": ["steps", "heart-rate", "sleep"],
      "subscriptionCreatePolicy": "AUTOMATIC"
    }
  ],
  "endpointAuthorization": {
    "secret": "Bearer <configured-secret>"
  }
}
```

- [ ] **ステップ2: create-or-update処理を実装する**

Google Health subscriber管理権限を持つService AccountのApplication Default Credentialsを使用する。

scriptは以下のargumentを受け付ける:

```text
--project-number
--subscriber-id personal-data
--endpoint-uri
```

Authorization値は以下のenvironment variableから読む:

```text
GOOGLE_HEALTH_ENDPOINT_AUTHORIZATION
```

- [ ] **ステップ3: 必須endpoint handshakeを検証する**

subscriber作成・更新がnon-2xxの場合はcommandを失敗させる。deploy済みendpointは以下を満たす:

```text
authorized {"type":"verification"}   → 200 or 201
unauthorized {"type":"verification"} → 401 or 403
```

- [ ] **ステップ4: Unit Testを実行する**

```bash
pytest tests/unit/test_subscriber_config.py -v
```

- [ ] **ステップ5: commitする**

```bash
git add scripts/configure_google_health_subscriber.py tests/unit/test_subscriber_config.py
git commit -m "feat: configure google health webhook subscriber"
```

## 計画全体の検証

実行:

```bash
pytest tests/unit tests/contract -v
ruff check src tests
```

Then perform one controlled staging event and verify:

```text
Google Health webhook
→ health-webhook log
→ Cloud Task
→ health-fetch
→ B2 object under scoped raw key
→ raw-loader task created
```

SwitchBot、天気、ローカルScreen Timeなどは独立したsource固有subprojectとして扱う。このGoogle Health vertical sliceでsource adapterとRaw contractを確立した後に追加する。
