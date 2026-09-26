import base64
import inspect
from pathlib import Path

import pytest

from app.adobe.client import AdobeGenerateError, blob_id_from_upload
from app.adobe.refs import load_reference_image, sniff_image_mime, stage_reference_bytes

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_sniff_png_magic() -> None:
    assert sniff_image_mime(_PNG, "image/jpeg") == "image/png"


def test_load_data_uri_and_raw_base64(tmp_path: Path) -> None:
    raw = base64.b64encode(_PNG).decode()
    data, mime = load_reference_image(f"data:image/png;base64,{raw}", media_dir=tmp_path)
    assert mime == "image/png"
    assert data == _PNG
    again, again_mime = load_reference_image(raw, media_dir=tmp_path)
    assert again == _PNG
    assert again_mime == "image/png"


def test_load_staged_file_and_reject_escape(tmp_path: Path) -> None:
    media = tmp_path / "media"
    token = stage_reference_bytes(media, "job-1", 0, _PNG, "image/png")
    data, mime = load_reference_image(token, media_dir=media)
    assert data == _PNG and mime == "image/png"
    with pytest.raises(AdobeGenerateError):
        load_reference_image("file:../secret.png", media_dir=media)


def test_load_local_logo(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    (tmp_path / "logo.png").write_bytes(_PNG)
    data, mime = load_reference_image("local:logo", media_dir=media)
    assert data == _PNG
    assert mime == "image/png"


def test_reject_plaintext_blob_token(tmp_path: Path) -> None:
    with pytest.raises(AdobeGenerateError) as error:
        load_reference_image("not-an-image", media_dir=tmp_path)
    assert error.value.status_code == 400


def test_load_http_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class _Resp:
        headers = {"content-type": "image/png"}

        def read(self, _n: int) -> bytes:
            return _PNG

        def __enter__(self) -> "_Resp":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: _Resp())
    data, mime = load_reference_image("https://cdn.example/a.png", media_dir=tmp_path)
    assert data == _PNG
    assert mime == "image/png"


def test_worker_uploads_refs_instead_of_passthrough() -> None:
    from app.services.generation.worker import process_job

    src = inspect.getsource(process_job)
    assert "blob_ids.append(token)" not in src
    assert "_upload_reference" in src
    assert "upload_via_proxy" in src
    assert "generate_video" in src
    assert "video_not_ready" not in src


def test_blob_id_prefers_images_array() -> None:
    assert blob_id_from_upload({"id": "req", "images": [{"id": "blob-9"}]}) == "blob-9"
