CREATE TABLE base.owntracks_location (
    event_key VARCHAR PRIMARY KEY,
    device_key VARCHAR NOT NULL,
    event_at TIMESTAMPTZ NOT NULL,
    latitude DOUBLE NOT NULL CHECK (latitude BETWEEN -90 AND 90),
    longitude DOUBLE NOT NULL CHECK (longitude BETWEEN -180 AND 180),
    accuracy_m DOUBLE,
    velocity_kmh DOUBLE,
    battery_percent INTEGER,
    object_key VARCHAR NOT NULL,
    storage_generation BIGINT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL,
    parser_version VARCHAR NOT NULL
);
