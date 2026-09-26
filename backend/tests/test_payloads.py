from app.adobe.payloads import (
    IMAGE_MODEL_IDS,
    VIDEO_MODEL_IDS,
    build_image_payload,
    build_video_payload,
    catalog_for,
)
from app.core.errors import AppError


def test_nano_banana_pro_payload_uses_gemini_flash() -> None:
    body = build_image_payload(
        model="nano-banana-pro", prompt="a cat", resolution="1K", aspect_ratio="16:9"
    )
    assert body["modelId"] == "gemini-flash"
    assert body["modelVersion"] == "nano-banana-3"
    assert body["size"] == {"width": 1024, "height": 1024}
    assert body["modelSpecificPayload"]["aspectRatio"] == "16:9"
    assert "skipCai" not in body


def test_nano_banana_square_sends_aspect_ratio() -> None:
    body = build_image_payload(
        model="nano-banana", prompt="a cat", resolution="1K", aspect_ratio="1:1"
    )
    assert body["size"] == {"width": 1024, "height": 1024}
    assert body["modelSpecificPayload"]["aspectRatio"] == "1:1"
    assert "skipCai" not in body


def test_gpt_image_2_uses_nested_size_string() -> None:
    body = build_image_payload(
        model="gpt-image-2", prompt="a cat", resolution="1K", aspect_ratio="1:1"
    )
    assert body["modelId"] == "gpt-image"
    assert body["modelVersion"] == "2"
    assert body["modelSpecificPayload"]["size"] == "1024x1024"
    assert body["caiClaimVersion"] == 2
    assert "size" not in body


def test_sunburst_maps_to_prism_and_has_cai() -> None:
    body = build_image_payload(
        model="gpt-image-2.5-sunburst", prompt="a cat", resolution="2K", aspect_ratio="1:1"
    )
    assert body["modelVersion"] == "gpt-image-2.5-prism"
    assert body["caiClaimVersion"] == 2
    assert body["size"]["width"] == 2048
    assert body["modelSpecificPayload"] == {"size": "2048x2048"}


def test_reference_blobs_are_attached_when_ids_given() -> None:
    body = build_image_payload(
        model="nano-banana",
        prompt="a cat",
        resolution="1K",
        aspect_ratio="1:1",
        reference_blob_ids=["blob-1", ""],
    )
    assert body["referenceBlobs"] == [{"id": "blob-1", "usage": "general"}]
    gpt = build_image_payload(
        model="gpt-image-2",
        prompt="a cat",
        resolution="1K",
        aspect_ratio="1:1",
        reference_blob_ids=["blob-2"],
    )
    assert gpt["referenceBlobs"] == [{"id": "blob-2", "usage": "subject"}]


def test_catalog_lists_all_official_models() -> None:
    assert IMAGE_MODEL_IDS == (
        "nano-banana",
        "nano-banana-pro",
        "gpt-image-2",
        "gpt-image-2.5-flare",
        "gpt-image-2.5-sunburst",
    )
    assert VIDEO_MODEL_IDS == (
        "gemini-omni-flash",
        "seedance-2.0",
        "seedance-2.0-fast",
    )
    img = catalog_for("nano-banana-pro")
    assert img["resolutions"] == ["1K", "2K", "4K"]
    assert img["submit_ready"] is True
    assert img["max_reference_images"] == 8
    assert img["modes"] == ["t2i", "i2i"]
    vid = catalog_for("seedance-2.0")
    assert vid["resolutions"] == ["720p", "1080p"]
    assert vid["submit_ready"] is True
    assert vid["max_reference_images"] == 4
    assert vid["modes"] == ["t2v", "i2v"]
    assert catalog_for("seedance-2.0-fast")["modes"] == ["t2v", "i2v"]
    assert catalog_for("seedance-2.0-fast")["resolutions"] == ["720p"]
    assert catalog_for("gemini-omni-flash")["submit_ready"] is True
    assert catalog_for("gemini-omni-flash")["modes"] == ["t2v", "i2v"]
    assert catalog_for("veo-3.1")["submit_ready"] is False
    assert catalog_for("veo-3.1-fast")["submit_ready"] is False
    assert catalog_for("veo-3.1-ref")["submit_ready"] is False
    unknown = catalog_for("sora2")
    assert unknown["submit_ready"] is False and unknown["modes"] == []


def test_omni_video_payload_uses_aura_module() -> None:
    body = build_video_payload(
        model="gemini-omni-flash",
        prompt="a cat walking",
        resolution="720p",
        aspect_ratio="16:9",
        duration=8,
    )
    assert body["modelId"] == "gemini-omni"
    assert body["modelVersion"] == "omni-flash"
    assert body["generationMetadata"] == {"module": "aura"}
    assert body["duration"] == 8
    assert body["size"] == {"width": 1280, "height": 720}
    assert body["generationSettings"]["aspectRatio"] == "16:9"
    refs = build_video_payload(
        model="gemini-omni-flash",
        prompt="a cat walking",
        resolution="720p",
        aspect_ratio="16:9",
        duration=5,
        reference_blob_ids=["blob-omni"],
    )
    assert refs["referenceBlobs"] == [{"id": "blob-omni", "usage": "style"}]


def test_seedance_video_payload_and_style_refs() -> None:
    body = build_video_payload(
        model="seedance-2.0-fast",
        prompt="a cat walking",
        resolution="720p",
        aspect_ratio="9:16",
        duration=5,
        reference_blob_ids=["blob-a", "blob-b"],
    )
    assert body["modelId"] == "seedance"
    assert body["modelVersion"] == "seedance_2.0_fast"
    assert body["generateAudio"] is True
    assert body["negativePrompt"]
    assert body["size"] == {"width": 720, "height": 1280}
    assert body["referenceBlobs"] == [
        {"id": "blob-a", "usage": "style"},
        {"id": "blob-b", "usage": "style"},
    ]
    try:
        build_video_payload(
            model="seedance-2.0-fast",
            prompt="a cat walking",
            resolution="1080p",
            aspect_ratio="16:9",
            duration=5,
        )
    except AppError as exc:
        assert exc.code == "resolution_unsupported"
    else:
        raise AssertionError("expected resolution_unsupported for seedance-2.0-fast 1080p")


def test_delisted_veo_cannot_build_payload() -> None:
    for model in ("veo-3.1", "veo-3.1-fast", "veo-3.1-ref"):
        try:
            build_video_payload(
                model=model,
                prompt="a cat walking",
                resolution="720p",
                aspect_ratio="16:9",
                duration=8,
            )
        except AppError as exc:
            assert exc.code == "model_unsupported"
            continue
        raise AssertionError(f"expected model_unsupported for {model}")
