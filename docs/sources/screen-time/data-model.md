# データモデル

## Raw record

SEGB container内の各recordについて次を保持する。

```text
segment filename
record data offset
record metadata
original protobuf bytes
```

protobufのdecode結果だけでなく元bytesも保持する。macOS更新でschemaが変わった場合に再decodeできるようにするためである。

## `screen_time_transition`

CollectorはiPhoneとMacを次の共通形式へ正規化する。

```text
screen_time_transition
- event_key
- device_identifier
- platform
- source_stream
- bundle_id
- event_at
- state
- transition_reason
- app_version
- app_build
- platform_flag_raw
- segment_filename
- record_offset
- observed_at
- original_payload
- parser_version
```

Macに存在しない`transition_reason`、`app_version`、`app_build`はnullとする。

`event_key`は次をcanonical化してhash化する。

```text
device_identifier
source_stream
bundle_id
event timestampのraw値
state
payload discriminator
```

segment filenameとoffsetは追跡情報として保存するが、同じeventが別segmentに現れる可能性を考慮し、それだけで重複判定しない。

## `screen_time_interval`

開始eventと終了eventからアプリ利用区間を生成する。

```text
screen_time_interval
- interval_key
- device_identifier
- platform
- bundle_id
- started_at
- ended_at
- duration_seconds
- source_stream
- quality
```

区間生成時は次の状態を区別する。

```text
start + 同じappのend   → complete
start + 次のapp start  → inferred_end_from_next_start
startのみ              → missing_end
endのみ                → missing_start
end < start            → invalid_timestamp
同一eventが複数存在    → duplicate
```

推定した終了時刻を、実際のend eventと同じ品質として扱わない。`quality`を付けて保存する。

## アプリ利用時間

生データに利用時間は入っていない。開始eventと終了eventの差から算出する。

```text
利用時間 = 終了event時刻 - 開始event時刻
```

### iPhone

```text
in_foreground = 1 → 開始
in_foreground = 0 → 終了
```

この値は「アプリが前面として記録された経過時間」であり、キー入力やタッチ操作を継続していた時間ではない。

### Mac

```text
field 1 = 1 → 開始
field 1 = 0 → 終了
```

この値は`ScreenTime.AppUsage`が記録した利用区間である。Appleの画面表示と完全に一致することは保証しない。
