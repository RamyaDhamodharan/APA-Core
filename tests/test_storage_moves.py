from types import SimpleNamespace

import pytest

from src.storage import minio_storage


class FakeMinio:
    def __init__(self):
        self.objects = {"incoming/source.pdf": (12, "etag-1")}
        self.calls = []

    def stat_object(self, _bucket, key):
        self.calls.append(("stat", key))
        if key not in self.objects:
            from minio.error import S3Error

            raise S3Error("missing", "NoSuchKey", "", "", "", None)
        size, etag = self.objects[key]
        return SimpleNamespace(size=size, etag=etag)

    def copy_object(self, _bucket, destination, _source):
        self.calls.append(("copy", destination))
        self.objects[destination] = self.objects["incoming/source.pdf"]

    def remove_object(self, _bucket, key):
        self.calls.append(("remove", key))
        self.objects.pop(key, None)


def test_move_verifies_destination_before_removing_source(monkeypatch):
    client = FakeMinio()
    monkeypatch.setattr(minio_storage, "get_minio_client", lambda: client)
    monkeypatch.setattr(minio_storage, "get_bucket_name", lambda: "test")

    minio_storage.move_file("incoming/source.pdf", "invoice/2026/10/id/source.pdf")

    assert client.objects == {"invoice/2026/10/id/source.pdf": (12, "etag-1")}
    destination_check = client.calls.index(
        ("stat", "invoice/2026/10/id/source.pdf")
    )
    source_remove = client.calls.index(("remove", "incoming/source.pdf"))
    assert destination_check < source_remove


def test_move_retry_is_idempotent_after_source_was_removed(monkeypatch):
    client = FakeMinio()
    monkeypatch.setattr(minio_storage, "get_minio_client", lambda: client)
    monkeypatch.setattr(minio_storage, "get_bucket_name", lambda: "test")
    destination = "invoice/2026/10/id/source.pdf"
    minio_storage.move_file("incoming/source.pdf", destination)
    client.calls.clear()

    minio_storage.move_file("incoming/source.pdf", destination)

    assert client.objects == {destination: (12, "etag-1")}
    assert not any(call[0] in {"copy", "remove"} for call in client.calls)


def test_move_leaves_source_if_existing_destination_does_not_match(monkeypatch):
    client = FakeMinio()
    client.objects["invoice/2026/10/id/source.pdf"] = (9, "different")
    monkeypatch.setattr(minio_storage, "get_minio_client", lambda: client)
    monkeypatch.setattr(minio_storage, "get_bucket_name", lambda: "test")

    with pytest.raises(RuntimeError, match="verification"):
        minio_storage.move_file(
            "incoming/source.pdf", "invoice/2026/10/id/source.pdf"
        )
    assert "incoming/source.pdf" in client.objects
