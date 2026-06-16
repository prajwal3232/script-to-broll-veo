"""Azure Blob Storage upload for generated videos.

Inert until either AZURE_BLOB_SAS_URL (a container-level SAS URL, preferred)
or AZURE_STORAGE_CONNECTION_STRING is set. When configured, `upload_video`
pushes bytes to the container under AZURE_BLOB_PREFIX and returns the public
blob URL (without the SAS query string).
"""

import config


def is_configured() -> bool:
    return bool(config.AZURE_SAS_URL) or bool(config.AZURE_STORAGE_CONNECTION_STRING)


def _full_blob_name(blob_name: str) -> str:
    prefix = config.AZURE_BLOB_PREFIX
    name = blob_name.lstrip("/")
    return f"{prefix}/{name}" if prefix else name


def _container_client():
    from azure.storage.blob import ContainerClient

    if config.AZURE_SAS_URL:
        return ContainerClient.from_container_url(config.AZURE_SAS_URL)

    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient.from_connection_string(
        config.AZURE_STORAGE_CONNECTION_STRING
    )
    return service.get_container_client(config.AZURE_STORAGE_CONTAINER)


def ensure_container() -> None:
    # With a container SAS we cannot (and need not) create the container.
    if config.AZURE_SAS_URL or not is_configured():
        return
    try:
        _container_client().create_container()
    except Exception:
        # Already exists or insufficient perms to create — ignore.
        pass


def _public_url(container_client, full_name: str) -> str:
    """Clean blob URL without the SAS query string."""
    base = container_client.url.split("?", 1)[0].rstrip("/")
    return f"{base}/{full_name}"


def upload_video(blob_name: str, data: bytes, content_type: str = "video/mp4") -> str:
    """Upload video bytes under the configured prefix and return the public URL.

    Raises RuntimeError if Azure is not configured.
    """
    if not is_configured():
        raise RuntimeError(
            "Azure storage is not configured. Set AZURE_BLOB_SAS_URL "
            "(or AZURE_STORAGE_CONNECTION_STRING) in your .env."
        )
    from azure.storage.blob import ContentSettings

    ensure_container()
    container = _container_client()
    full_name = _full_blob_name(blob_name)
    blob = container.get_blob_client(full_name)
    blob.upload_blob(
        data,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )
    return _public_url(container, full_name)
