"""The subset of the untyped GCS SDK used by the Raw transport."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Protocol


class GCSBlob(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def generation(self) -> int | None: ...

    @property
    def time_created(self) -> datetime | None: ...

    content_encoding: str | None

    def upload_from_string(
        self,
        data: bytes,
        *,
        content_type: str,
        if_generation_match: int | None = None,
        checksum: str = "auto",
        crc32c_checksum_value: str | None = None,
        retry: object = ...,
    ) -> None: ...

    def download_as_bytes(
        self, *, raw_download: bool, if_generation_match: int | None = None
    ) -> bytes: ...

    def delete(self, *, if_generation_match: int) -> None: ...


class GCSBucket(Protocol):
    def blob(self, blob_name: str, *, generation: int | None = None) -> GCSBlob: ...


class GCSBlobIterator(Protocol):
    @property
    def pages(self) -> Iterable[Iterable[GCSBlob]]: ...

    def __iter__(self) -> Iterator[GCSBlob]: ...


class GCSClient(Protocol):
    def bucket(self, bucket_name: str) -> GCSBucket: ...

    def list_blobs(self, bucket_or_name: GCSBucket, *, prefix: str) -> GCSBlobIterator: ...
