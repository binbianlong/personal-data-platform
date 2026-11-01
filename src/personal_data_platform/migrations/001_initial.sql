-- 個人の行動・健康データを保存する基礎スキーマ。
CREATE SCHEMA IF NOT EXISTS base;
CREATE SCHEMA IF NOT EXISTS marts;
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.schema_migration (
    migration_id VARCHAR PRIMARY KEY,
    checksum VARCHAR NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE ops.job_lock (
    job_name VARCHAR PRIMARY KEY,
    owner_id VARCHAR NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
COMMENT ON TABLE ops.schema_migration IS '適用済みSQL migrationの台帳。1行はSQLファイル1本。';
COMMENT ON COLUMN ops.schema_migration.migration_id IS 'SQLファイル名。主キー。NULL不可。';
COMMENT ON COLUMN ops.schema_migration.checksum IS '適用したSQLのSHA-256。NULL不可。';
COMMENT ON COLUMN ops.schema_migration.applied_at IS '適用時刻。タイムゾーン付きの絶対時刻。NULL不可。';
COMMENT ON TABLE ops.job_lock IS '共有writerの期限付き排他lease。1行はjob名ごとの現在の所有者。';
COMMENT ON COLUMN ops.job_lock.job_name IS '排他対象job名。主キー。NULL不可。';
COMMENT ON COLUMN ops.job_lock.owner_id IS 'lease所有者の実行識別子。NULL不可。';
COMMENT ON COLUMN ops.job_lock.expires_at IS 'lease失効時刻。タイムゾーン付きの絶対時刻。NULL不可。';


CREATE TABLE base.sleep_session (
    sleep_session_id VARCHAR PRIMARY KEY,
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    source_date DATE,
    sleep_minutes DOUBLE,
    sleep_type VARCHAR,
    is_main_sleep BOOLEAN,
    awake_minutes DOUBLE,
    sleep_period_minutes DOUBLE,
    sleep_latency_minutes DOUBLE,
    after_wake_minutes DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER
);
COMMENT ON TABLE base.sleep_session IS '睡眠1回。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.sleep_session.sleep_session_id IS '睡眠セッションの識別子。NULL不可。';
COMMENT ON COLUMN base.sleep_session.start_at IS '提供元の睡眠開始時刻。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_session.end_at IS '提供元の睡眠終了時刻。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_session.source_date IS '提供元が付けた睡眠日付。提供元の日付を保持し日本時間へ置換しない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_session.sleep_minutes IS '提供元サマリーの実睡眠分数。開始・終了の差ではない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_session.sleep_type IS '提供元の睡眠データ種別。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_session.is_main_sleep IS '提供元の主睡眠フラグ。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_session.awake_minutes IS '睡眠期間内の覚醒分数。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_session.sleep_period_minutes IS '提供元の睡眠期間分数。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_session.sleep_latency_minutes IS '入眠までの分数。自動記録の0が既定値の場合もある。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_session.after_wake_minutes IS '提供元の起床後分数。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_session.offset_seconds IS '開始時刻に付属する提供元のUTCオフセット秒。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_session.end_offset_seconds IS '終了時刻に付属する提供元のUTCオフセット秒。NULLは不明・未取得・未算出。';

CREATE TABLE base.sleep_detail (
    sleep_detail_id VARCHAR PRIMARY KEY,
    sleep_session_id VARCHAR,
    kind VARCHAR,
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    category VARCHAR,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER
);
COMMENT ON TABLE base.sleep_detail IS '睡眠段階・短い覚醒の区間。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.sleep_detail.sleep_detail_id IS '詳細区間の識別子。NULL不可。';
COMMENT ON COLUMN base.sleep_detail.sleep_session_id IS '親となるbase.sleep_sessionのID。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_detail.kind IS 'sleep-stage、sleep-wakeなどの詳細種別。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_detail.start_at IS '区間開始。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_detail.end_at IS '区間終了。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_detail.category IS 'deep・light・rem・awake・short-awakeningなどの分類。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_detail.offset_seconds IS '開始時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_detail.end_offset_seconds IS '終了時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';

CREATE TABLE base.heart_rate_minute (
    heart_rate_minute_id VARCHAR PRIMARY KEY,
    start_at TIMESTAMPTZ,
    mean_bpm DOUBLE,
    min_bpm DOUBLE,
    max_bpm DOUBLE,
    aggregation_version VARCHAR
);
COMMENT ON TABLE base.heart_rate_minute IS '1分ごとの心拍。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.heart_rate_minute.heart_rate_minute_id IS '系列・開始分・集計方式を含めた識別子。NULL不可。';
COMMENT ON COLUMN base.heart_rate_minute.start_at IS 'UTCの1分区間の開始。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.heart_rate_minute.mean_bpm IS '分平均心拍。bpm。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.heart_rate_minute.min_bpm IS '分最小心拍。bpm。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.heart_rate_minute.max_bpm IS '分最大心拍。bpm。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.heart_rate_minute.aggregation_version IS '分集計の方式の版。NULLは不明・未取得・未算出。';

CREATE TABLE base.steps_interval (
    steps_interval_id VARCHAR PRIMARY KEY,
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    source_date DATE,
    steps DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER
);
COMMENT ON TABLE base.steps_interval IS '歩数の区間記録。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.steps_interval.steps_interval_id IS '歩数区間の識別子。NULL不可。';
COMMENT ON COLUMN base.steps_interval.start_at IS '区間開始。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.steps_interval.end_at IS '区間終了。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.steps_interval.source_date IS '提供元の日付。提供元の日付を保持し日本時間へ置換しない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.steps_interval.steps IS '区間内の歩数増分。累積歩数ではない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.steps_interval.offset_seconds IS '開始時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.steps_interval.end_offset_seconds IS '終了時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';

CREATE TABLE base.active_zone_interval (
    active_zone_interval_id VARCHAR PRIMARY KEY,
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    source_date DATE,
    active_zone_minutes DOUBLE,
    category VARCHAR,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER
);
COMMENT ON TABLE base.active_zone_interval IS 'アクティブゾーン分数の区間。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.active_zone_interval.active_zone_interval_id IS '区間の識別子。NULL不可。';
COMMENT ON COLUMN base.active_zone_interval.start_at IS '区間開始。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.active_zone_interval.end_at IS '区間終了。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.active_zone_interval.source_date IS '提供元の日付。提供元の日付を保持し日本時間へ置換しない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.active_zone_interval.active_zone_minutes IS '提供元で重み付け済みのアクティブゾーン分数。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.active_zone_interval.category IS 'fat_burn・cardio・peak・分類未指定など。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.active_zone_interval.offset_seconds IS '開始時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.active_zone_interval.end_offset_seconds IS '終了時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';

CREATE TABLE base.exercise_session (
    exercise_session_id VARCHAR PRIMARY KEY,
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    source_date DATE,
    exercise_type VARCHAR,
    exercise_name VARCHAR,
    active_seconds DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER
);
COMMENT ON TABLE base.exercise_session IS '種類付きの運動記録。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.exercise_session.exercise_session_id IS '運動セッションの識別子。NULL不可。';
COMMENT ON COLUMN base.exercise_session.start_at IS '開始時刻。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.exercise_session.end_at IS '終了時刻。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.exercise_session.source_date IS '提供元の開始日付。提供元の日付を保持し日本時間へ置換しない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.exercise_session.exercise_type IS '提供元の運動種類。未知の種類も保持する。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.exercise_session.exercise_name IS '提供元の運動表示名。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.exercise_session.active_seconds IS '休止を除いた活動秒数。未提供ならNULL。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.exercise_session.offset_seconds IS '開始時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.exercise_session.end_offset_seconds IS '終了時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';

CREATE TABLE base.sedentary_interval (
    sedentary_interval_id VARCHAR PRIMARY KEY,
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    source_date DATE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER
);
COMMENT ON TABLE base.sedentary_interval IS '不活動区間。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.sedentary_interval.sedentary_interval_id IS '不活動区間の識別子。NULL不可。';
COMMENT ON COLUMN base.sedentary_interval.start_at IS '区間開始。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sedentary_interval.end_at IS '区間終了。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sedentary_interval.source_date IS '提供元の日付。提供元の日付を保持し日本時間へ置換しない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sedentary_interval.offset_seconds IS '開始時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sedentary_interval.end_offset_seconds IS '終了時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';

CREATE TABLE base.distance_interval (
    distance_interval_id VARCHAR PRIMARY KEY,
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    source_date DATE,
    distance_meters DOUBLE,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER
);
COMMENT ON TABLE base.distance_interval IS '提供元の距離記録。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.distance_interval.distance_interval_id IS '距離区間の識別子。NULL不可。';
COMMENT ON COLUMN base.distance_interval.start_at IS '区間開始。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.distance_interval.end_at IS '区間終了。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.distance_interval.source_date IS '提供元の日付。提供元の日付を保持し日本時間へ置換しない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.distance_interval.distance_meters IS '提供元が報告した区間距離。m。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.distance_interval.offset_seconds IS '開始時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.distance_interval.end_offset_seconds IS '終了時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';

CREATE TABLE base.heart_rate_zone_interval (
    heart_rate_zone_interval_id VARCHAR PRIMARY KEY,
    start_at TIMESTAMPTZ,
    end_at TIMESTAMPTZ,
    source_date DATE,
    zone VARCHAR,
    offset_seconds INTEGER,
    end_offset_seconds INTEGER
);
COMMENT ON TABLE base.heart_rate_zone_interval IS '心拍ゾーン区間。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.heart_rate_zone_interval.heart_rate_zone_interval_id IS '区間の識別子。NULL不可。';
COMMENT ON COLUMN base.heart_rate_zone_interval.start_at IS '区間開始。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.heart_rate_zone_interval.end_at IS '区間終了。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.heart_rate_zone_interval.source_date IS '提供元の日付。提供元の日付を保持し日本時間へ置換しない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.heart_rate_zone_interval.zone IS 'LIGHT・MODERATE・VIGOROUS・PEAKなどの提供元分類。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.heart_rate_zone_interval.offset_seconds IS '開始時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.heart_rate_zone_interval.end_offset_seconds IS '終了時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';

CREATE TABLE base.health_daily (
    source_date DATE PRIMARY KEY,
    resting_heart_rate_bpm DOUBLE,
    hrv_rmssd_ms DOUBLE,
    deep_sleep_hrv_rmssd_ms DOUBLE,
    non_rem_heart_rate_bpm BIGINT,
    hrv_entropy DOUBLE,
    respiratory_rate_breaths_per_minute DOUBLE,
    spo2_average_percent DOUBLE,
    spo2_lower_bound_percent DOUBLE,
    spo2_upper_bound_percent DOUBLE,
    spo2_standard_deviation_percent DOUBLE,
    nightly_skin_temperature_celsius DOUBLE,
    baseline_skin_temperature_celsius DOUBLE,
    relative_nightly_stddev_30d_celsius DOUBLE,
    vo2_max_ml_kg_min DOUBLE,
    vo2_max_estimated BOOLEAN,
    cardio_fitness_level VARCHAR,
    vo2_max_covariance DOUBLE
);
COMMENT ON TABLE base.health_daily IS '提供元の日次健康指標。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.health_daily.source_date IS '提供元の日付。提供元の日付を保持し日本時間へ置換しない。NULL不可。';
COMMENT ON COLUMN base.health_daily.resting_heart_rate_bpm IS '安静時心拍。bpm。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.hrv_rmssd_ms IS '日次平均RMSSD。ms。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.deep_sleep_hrv_rmssd_ms IS '深睡眠時のRMSSD。ms。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.non_rem_heart_rate_bpm IS 'non-REM睡眠時の心拍。bpm。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.hrv_entropy IS '提供元の心拍間隔分布のエントロピー。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.respiratory_rate_breaths_per_minute IS '提供元の日次呼吸数。回/分。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.spo2_average_percent IS 'SpO2平均。%。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.spo2_lower_bound_percent IS 'SpO2の信頼区間下限。最小値ではない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.spo2_upper_bound_percent IS 'SpO2の信頼区間上限。最大値ではない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.spo2_standard_deviation_percent IS '過去7〜30日の日次SpO2平均に関する標準偏差。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.nightly_skin_temperature_celsius IS '夜間皮膚温。℃。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.baseline_skin_temperature_celsius IS '基準皮膚温。℃。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.relative_nightly_stddev_30d_celsius IS '提供元の相対夜間温度の30日標準偏差。℃。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.vo2_max_ml_kg_min IS '日次VO2 max。ml/kg/min。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.vo2_max_estimated IS '提供元が付けた推定フラグ。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.cardio_fitness_level IS '提供元の心肺フィットネス分類。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.health_daily.vo2_max_covariance IS '提供元のVO2 max共分散値。NULLは不明・未取得・未算出。';

CREATE TABLE base.hrv_sample (
    hrv_sample_id VARCHAR PRIMARY KEY,
    sample_at TIMESTAMPTZ,
    source_date DATE,
    rmssd_ms DOUBLE,
    sdnn_ms DOUBLE,
    offset_seconds INTEGER
);
COMMENT ON TABLE base.hrv_sample IS 'HRVの詳細サンプル。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.hrv_sample.hrv_sample_id IS 'サンプルの識別子。NULL不可。';
COMMENT ON COLUMN base.hrv_sample.sample_at IS '測定時刻。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.hrv_sample.source_date IS '提供元の日付。提供元の日付を保持し日本時間へ置換しない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.hrv_sample.rmssd_ms IS 'サンプルのRMSSD。ms。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.hrv_sample.sdnn_ms IS 'サンプルのSDNN。ms。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.hrv_sample.offset_seconds IS '測定時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';

CREATE TABLE base.sleep_respiratory_summary (
    respiratory_summary_id VARCHAR PRIMARY KEY,
    sample_at TIMESTAMPTZ,
    source_date DATE,
    full_breaths_per_minute DOUBLE,
    full_standard_deviation DOUBLE,
    full_signal_to_noise DOUBLE,
    deep_breaths_per_minute DOUBLE,
    deep_standard_deviation DOUBLE,
    deep_signal_to_noise DOUBLE,
    light_breaths_per_minute DOUBLE,
    light_standard_deviation DOUBLE,
    light_signal_to_noise DOUBLE,
    rem_breaths_per_minute DOUBLE,
    rem_standard_deviation DOUBLE,
    rem_signal_to_noise DOUBLE,
    offset_seconds INTEGER
);
COMMENT ON TABLE base.sleep_respiratory_summary IS '睡眠呼吸の詳細サマリー。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.sleep_respiratory_summary.respiratory_summary_id IS 'サンプルの識別子。NULL不可。';
COMMENT ON COLUMN base.sleep_respiratory_summary.sample_at IS '提供元の測定時刻。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.source_date IS '提供元の日付。提供元の日付を保持し日本時間へ置換しない。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.full_breaths_per_minute IS '睡眠全体の呼吸数。回/分。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.full_standard_deviation IS '睡眠全体の呼吸数の標準偏差。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.full_signal_to_noise IS '睡眠全体の提供元SNR。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.deep_breaths_per_minute IS '深睡眠の呼吸数。回/分。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.deep_standard_deviation IS '深睡眠の呼吸数の標準偏差。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.deep_signal_to_noise IS '深睡眠の提供元SNR。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.light_breaths_per_minute IS '浅い睡眠の呼吸数。回/分。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.light_standard_deviation IS '浅い睡眠の呼吸数の標準偏差。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.light_signal_to_noise IS '浅い睡眠の提供元SNR。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.rem_breaths_per_minute IS 'REM睡眠の呼吸数。回/分。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.rem_standard_deviation IS 'REM睡眠の呼吸数の標準偏差。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.rem_signal_to_noise IS 'REM睡眠の提供元SNR。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.sleep_respiratory_summary.offset_seconds IS '測定時刻の提供元UTCオフセット秒。NULLは不明・未取得・未算出。';

CREATE TABLE base.location_point (
    location_point_id VARCHAR PRIMARY KEY,
    device_key VARCHAR,
    observed_at TIMESTAMPTZ,
    latitude DOUBLE,
    longitude DOUBLE,
    accuracy_meters DOUBLE,
    speed_kmh DOUBLE,
    motion_activities VARCHAR[]
);
COMMENT ON TABLE base.location_point IS '位置情報。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.location_point.location_point_id IS '端末と記録内容から識別する測位ID。NULL不可。';
COMMENT ON COLUMN base.location_point.device_key IS '測位した端末のソース内キー。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.location_point.observed_at IS '測位時刻。サーバー受信時刻ではない。タイムゾーン付きの絶対時刻。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.location_point.latitude IS '緯度。度。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.location_point.longitude IS '経度。度。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.location_point.accuracy_meters IS '提供元の測位精度。m。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.location_point.speed_kmh IS '提供元の速度。km/h。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.location_point.motion_activities IS '提供元の活動ラベル配列。NULL・空配列・未知値を保持する。NULLは不明・未取得・未算出。';

CREATE TABLE base.iphone_screen_time_event (
    event_key VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    bundle_id VARCHAR NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ('start', 'end')),
    is_valid BOOLEAN NOT NULL DEFAULT TRUE
);
COMMENT ON TABLE base.iphone_screen_time_event IS 'スクリーンタイムのアプリ開始・終了イベント。1行は一意なイベント。送信ファイル消失だけでは無効化しない。。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.iphone_screen_time_event.event_key IS 'イベントの識別子。主キー。再送しても同じキーを使う。NULL不可。';
COMMENT ON COLUMN base.iphone_screen_time_event.device_key IS '記録元の端末。NULL不可。';
COMMENT ON COLUMN base.iphone_screen_time_event.bundle_id IS 'アプリの識別子。NULL不可。';
COMMENT ON COLUMN base.iphone_screen_time_event.event_at IS 'イベントが起きた時刻。タイムゾーン付きの絶対時刻。NULL不可。';
COMMENT ON COLUMN base.iphone_screen_time_event.state IS 'start＝前面利用開始、end＝終了。NULL不可。';
COMMENT ON COLUMN base.iphone_screen_time_event.is_valid IS '現在の分析に採用する記録ならTRUE。NULL不可。';

CREATE TABLE base.mac_screen_time_event (
    event_key VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    bundle_id VARCHAR NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    state VARCHAR NOT NULL CHECK (state IN ('start', 'end')),
    is_valid BOOLEAN NOT NULL DEFAULT TRUE
);
COMMENT ON TABLE base.mac_screen_time_event IS 'スクリーンタイムのアプリ開始・終了イベント。1行は一意なイベント。送信ファイル消失だけでは無効化しない。。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.mac_screen_time_event.event_key IS 'イベントの識別子。主キー。再送しても同じキーを使う。NULL不可。';
COMMENT ON COLUMN base.mac_screen_time_event.device_key IS '記録元の端末。NULL不可。';
COMMENT ON COLUMN base.mac_screen_time_event.bundle_id IS 'アプリの識別子。NULL不可。';
COMMENT ON COLUMN base.mac_screen_time_event.event_at IS 'イベントが起きた時刻。タイムゾーン付きの絶対時刻。NULL不可。';
COMMENT ON COLUMN base.mac_screen_time_event.state IS 'start＝前面利用開始、end＝終了。NULL不可。';
COMMENT ON COLUMN base.mac_screen_time_event.is_valid IS '現在の分析に採用する記録ならTRUE。NULL不可。';

CREATE TABLE base.app (
    platform VARCHAR NOT NULL CHECK (platform IN ('ios', 'macos')),
    bundle_id VARCHAR NOT NULL,
    app_name VARCHAR,
    PRIMARY KEY (platform, bundle_id)
);
COMMENT ON TABLE base.app IS 'アプリ名の対応。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.app.platform IS 'iosまたはmacos。NULL不可。';
COMMENT ON COLUMN base.app.bundle_id IS 'そのOSでのアプリ識別子。NULL不可。';
COMMENT ON COLUMN base.app.app_name IS '本人が分かるアプリ名。NULLは不明・未取得・未算出。';

CREATE TABLE base.profile (
    profile_id VARCHAR PRIMARY KEY,
    sex VARCHAR,
    height_cm DOUBLE,
    birth_date DATE,
    home_place_id VARCHAR,
    university_place_id VARCHAR
);
COMMENT ON TABLE base.profile IS '本人の固定情報。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.profile.profile_id IS '本人の設定行ID。NULL不可。';
COMMENT ON COLUMN base.profile.sex IS '本人が入力した性別。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.profile.height_cm IS '現在の身長。cm。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.profile.birth_date IS '生年月日。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.profile.home_place_id IS '現在の自宅を示すbase.placeのID。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.profile.university_place_id IS '大学を示すbase.placeのID。NULLは不明・未取得・未算出。';

CREATE TABLE base.place (
    place_id VARCHAR PRIMARY KEY,
    place_name VARCHAR,
    latitude DOUBLE,
    longitude DOUBLE,
    radius_meters DOUBLE
);
COMMENT ON TABLE base.place IS '自宅・大学などの場所。NULLは不明・未取得・未算出を表す。';
COMMENT ON COLUMN base.place.place_id IS '場所の識別子。NULL不可。';
COMMENT ON COLUMN base.place.place_name IS '自宅・大学などの表示名。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.place.latitude IS '代表地点の緯度。度。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.place.longitude IS '代表地点の経度。度。NULLは不明・未取得・未算出。';
COMMENT ON COLUMN base.place.radius_meters IS '円で場所判定するときの半径。m。NULLは不明・未取得・未算出。';
