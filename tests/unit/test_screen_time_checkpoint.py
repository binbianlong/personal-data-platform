import pytest
from google.api_core.exceptions import NotFound, PreconditionFailed

from personal_data_platform.sources.screen_time.checkpoint import (
    FileCheckpointStore,
    GCSCheckpointStore,
)


class Bucket:
    def __init__(self):
        self.generation = 0
        self.payload = None

    def blob(self, key):
        bucket = self

        class Blob:
            generation = None

            def reload(self):
                if bucket.payload is None:
                    raise NotFound("missing")
                self.generation = bucket.generation

            def download_as_bytes(self, *, raw_download, if_generation_match):
                assert raw_download
                if if_generation_match != bucket.generation:
                    raise PreconditionFailed("stale download")
                return bucket.payload

            def upload_from_string(self, payload, *, if_generation_match, **kwargs):
                if if_generation_match != bucket.generation:
                    raise PreconditionFailed("stale upload")
                bucket.payload = payload
                bucket.generation += 1
                self.generation = bucket.generation

        return Blob()


def test_gcs_checkpoint_fences_stale_writer_and_reader():
    bucket = Bucket()
    first, second = (
        GCSCheckpointStore(bucket, "checkpoint"),
        GCSCheckpointStore(bucket, "checkpoint"),
    )
    assert first.read() is None
    assert second.read() is None
    first.write(b"first")
    with pytest.raises(PreconditionFailed):
        second.write(b"stale")
    assert second.read() == b"first"
    second.write(b"second")
    with pytest.raises(PreconditionFailed):
        first.write(b"stale")
    assert first.read() == b"second"


def test_file_checkpoint_is_private_and_replaced_atomically(tmp_path):
    path = tmp_path / "checkpoint.sqlite"
    store = FileCheckpointStore(path)
    assert store.read() is None
    store.write(b"first")
    store.write(b"second")
    assert store.read() == b"second"
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]
