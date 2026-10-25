import os
from functools import lru_cache

from minio import Minio
from minio.error import S3Error


@lru_cache(maxsize=1)
def get_minio_client() -> Minio:
    endpoint = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
    endpoint = endpoint.removeprefix("http://").removeprefix("https://")

    secure = os.getenv("MINIO_SECURE", "false").lower() == "true"

    return Minio(
        endpoint,
        access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin123"),
        secure=secure,
    )


def get_bucket_name() -> str:
    return os.getenv("MINIO_BUCKET", "ap-documents")


def ensure_bucket() -> None:
    client = get_minio_client()
    bucket = get_bucket_name()

    if not client.bucket_exists(bucket):
        try:
            client.make_bucket(bucket)
        except S3Error as exc:
            # Another request may have created the bucket concurrently.
            if exc.code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                raise


def upload_file(object_key: str, content: bytes, content_type: str) -> None:
    from io import BytesIO

    ensure_bucket()

    get_minio_client().put_object(
        bucket_name=get_bucket_name(),
        object_name=object_key,
        data=BytesIO(content),
        length=len(content),
        content_type=content_type or "application/octet-stream",
    )


def download_file(object_key: str) -> bytes:
    client = get_minio_client()
    response = client.get_object(get_bucket_name(), object_key)

    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def file_exists(object_key: str) -> bool:
    """Return whether an object exists without exposing object contents."""
    try:
        get_minio_client().stat_object(get_bucket_name(), object_key)
        return True
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NotFound"}:
            return False
        raise


def delete_file(object_key: str) -> None:
    get_minio_client().remove_object(get_bucket_name(), object_key)


def move_file(source_key: str, destination_key: str) -> None:
    """Copy, verify, then remove an object within the configured bucket."""
    from minio.commonconfig import CopySource

    client = get_minio_client()
    bucket = get_bucket_name()
    if source_key == destination_key:
        client.stat_object(bucket, destination_key)
        return
    source_stat = None
    try:
        source_stat = client.stat_object(bucket, source_key)
    except S3Error as exc:
        if exc.code not in {"NoSuchKey", "NoSuchObject", "NotFound"}:
            raise
    try:
        destination_stat = client.stat_object(bucket, destination_key)
    except S3Error as exc:
        if exc.code not in {"NoSuchKey", "NoSuchObject", "NotFound"}:
            raise
        if source_stat is None:
            raise
        client.copy_object(bucket, destination_key, CopySource(bucket, source_key))
        destination_stat = client.stat_object(bucket, destination_key)
    if source_stat is None:
        return
    if getattr(source_stat, "size", None) != getattr(destination_stat, "size", None):
        raise RuntimeError("Copied object verification failed")
    source_etag = getattr(source_stat, "etag", None)
    destination_etag = getattr(destination_stat, "etag", None)
    if source_etag and destination_etag and source_etag != destination_etag:
        raise RuntimeError("Copied object verification failed")
    client.remove_object(bucket, source_key)
