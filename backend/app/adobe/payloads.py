"""Upstream generate-async bodies.

Image sizes and field shapes follow the verified HAR tables in this repo.
Video bodies follow adobe2api-plus (authorized video source only):
https://github.com/songsongQAQ/adobe2api-plus — `src/lib/adobe/payloads.ts`
`buildVideoPayload`. Image `referenceBlobs[].usage` is required live (`general` nano,
`subject` gpt). Veo is delisted (408). Do not copy that project's ARP or retry.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.core.errors import AppError
from app.services.config.pricing import normalize_resolution

_IMAGE_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9")
_VIDEO_RATIOS = ("16:9", "9:16")
_VIDEO_RESOLUTIONS = ("720p", "1080p")
_VIDEO_DURATIONS = (5, 8, 10)

# Panel API caps. Not Adobe HAR-verified limits.
IMAGE_MAX_REFS = 8
VIDEO_MAX_REFS = 4

# Nano Banana family: square placeholder; ratio is in modelSpecificPayload.aspectRatio.
_NB_PLACEHOLDER = {"1K": (1024, 1024), "2K": (2048, 2048), "4K": (4096, 4096)}

# GPT Image 2 / 2.5: both edges multiples of 16.
_GPT_SIZE: dict[str, dict[str, tuple[int, int]]] = {
    "1K": {
        "1:1": (1024, 1024),
        "3:2": (1536, 1024),
        "2:3": (1024, 1536),
        "4:3": (1152, 864),
        "3:4": (864, 1152),
        "5:4": (1120, 896),
        "4:5": (896, 1120),
        "16:9": (1280, 720),
        "9:16": (720, 1280),
        "21:9": (1456, 624),
    },
    "2K": {
        "1:1": (2048, 2048),
        "3:2": (2496, 1664),
        "2:3": (1664, 2496),
        "4:3": (2304, 1728),
        "3:4": (1728, 2304),
        "5:4": (2240, 1792),
        "4:5": (1792, 2240),
        "16:9": (2560, 1440),
        "9:16": (1440, 2560),
        "21:9": (3024, 1296),
    },
    "4K": {
        "1:1": (2480, 2480),
        "3:2": (3056, 2032),
        "2:3": (2032, 3056),
        "4:3": (2880, 2160),
        "3:4": (2160, 2880),
        "5:4": (2784, 2224),
        "4:5": (2224, 2784),
        "16:9": (3328, 1872),
        "9:16": (1872, 3328),
        "21:9": (3808, 1632),
    },
}

# Panel model_id → (upstream modelId, modelVersion, family)
_MODELS: dict[str, tuple[str, str, str]] = {
    "nano-banana": ("gemini-flash", "nano-banana-2", "nano"),
    "nano-banana-pro": ("gemini-flash", "nano-banana-3", "nano"),
    "gpt-image-2": ("gpt-image", "2", "gpt2"),
    "gpt-image-2.5-flare": ("gpt-image", "gpt-image-2.5-flare", "gpt25"),
    "gpt-image-2.5-sunburst": ("gpt-image", "gpt-image-2.5-prism", "gpt25"),
}

# Panel id → adobe2api-plus engine mapping (omni / seedance / veo only).
_SEEDANCE_NEGATIVE = "cartoon, vector art, & bad aesthetics & poor aesthetic"
_VIDEO_MODELS: dict[str, dict[str, Any]] = {
    "gemini-omni-flash": {
        "family": "omni",
        "modes": ["t2v", "i2v"],
        "model_id": "gemini-omni",
        "model_version": "omni-flash",
        "resolutions": ["720p", "1080p"],
    },
    "seedance-2.0": {
        "family": "seedance",
        "modes": ["t2v", "i2v"],
        "model_id": "seedance",
        "model_version": "seedance_2.0",
        "resolutions": ["720p", "1080p"],
    },
    "seedance-2.0-fast": {
        "family": "seedance",
        "modes": ["t2v", "i2v"],
        "model_id": "seedance",
        "model_version": "seedance_2.0_fast",
        "resolutions": ["720p"],
    },
    "veo-3.1": {
        "family": "veo",
        "modes": ["t2v"],
        "model_id": "veo",
        "model_version": "3.1-generate",
    },
    "veo-3.1-ref": {
        "family": "veo_ref",
        "modes": ["i2v"],
        "model_id": "veo",
        "model_version": "3.1-generate",
    },
    "veo-3.1-fast": {
        "family": "veo",
        "modes": ["t2v"],
        "model_id": "veo",
        "model_version": "3.1-fast-generate",
    },
}

_VIDEO_SIZE = {
    "720p": {"16:9": (1280, 720), "9:16": (720, 1280)},
    "1080p": {"16:9": (1920, 1080), "9:16": (1080, 1920)},
}

_DELISTED_VIDEO = frozenset({"veo-3.1", "veo-3.1-fast", "veo-3.1-ref"})

IMAGE_MODEL_IDS: tuple[str, ...] = tuple(_MODELS)
VIDEO_MODEL_IDS: tuple[str, ...] = tuple(
    key for key in _VIDEO_MODELS if key not in _DELISTED_VIDEO
)


def catalog_for(model_id: str) -> dict[str, Any]:
    key = model_id.strip().lower()
    if key in _MODELS:
        return {
            "aspect_ratios": list(_IMAGE_RATIOS),
            "resolutions": ["1K", "2K", "4K"],
            "supports_edit": True,
            "max_reference_images": IMAGE_MAX_REFS,
            "modes": ["t2i", "i2i"],
            "submit_ready": True,
        }
    video = _VIDEO_MODELS.get(key)
    if video is not None:
        listed = key not in _DELISTED_VIDEO
        resolutions = list(video.get("resolutions") or _VIDEO_RESOLUTIONS)
        return {
            "aspect_ratios": list(_VIDEO_RATIOS),
            "resolutions": resolutions,
            "durations": list(_VIDEO_DURATIONS),
            "supports_edit": False,
            "max_reference_images": VIDEO_MAX_REFS,
            "modes": list(video["modes"]) if listed else [],
            "submit_ready": listed,
        }
    return {
        "aspect_ratios": [],
        "resolutions": [],
        "durations": None,
        "supports_edit": False,
        "max_reference_images": 0,
        "modes": [],
        "submit_ready": False,
    }


def _seed() -> int:
    return int.from_bytes(uuid4().bytes[:4], "big") % 999_999


def _gpt_size(ratio: str, resolution: str) -> tuple[int, int]:
    tier = _GPT_SIZE.get(normalize_resolution(resolution), _GPT_SIZE["1K"])
    return tier.get(ratio, tier["1:1"])


def build_image_payload(
    *,
    model: str,
    prompt: str,
    resolution: str,
    aspect_ratio: str,
    reference_blob_ids: list[str] | None = None,
) -> dict[str, Any]:
    key = model.strip().lower()
    mapped = _MODELS.get(key)
    if mapped is None:
        raise AppError(
            code="model_unsupported", message=f"模型 {model} 暂未接入提交链路", http_status=422
        )
    model_id, version, family = mapped
    text = prompt.strip()[:4000]
    usage = "general" if family == "nano" else "subject"
    blobs = [
        {"id": item, "usage": usage}
        for item in (reference_blob_ids or [])
        if str(item).strip()
    ]
    meta = {"module": "text2image", "submodule": "ff-image-generate"}
    if family == "nano":
        w, h = _NB_PLACEHOLDER.get(normalize_resolution(resolution), (1024, 1024))
        msp: dict[str, Any] = {"parameters": {"addWatermark": False}}
        # 1:1 也必须带 aspectRatio；省略时上游默认 16:9。
        if aspect_ratio and aspect_ratio.lower() != "auto":
            msp["aspectRatio"] = aspect_ratio
        return {
            "modelId": model_id,
            "modelVersion": version,
            "n": 1,
            "prompt": text,
            "size": {"width": w, "height": h},
            "seeds": [_seed()],
            "groundSearch": False,
            "output": {"storeInputs": True},
            "referenceBlobs": blobs,
            "generationMetadata": meta,
            "modelSpecificPayload": msp,
            "caiClaimVersion": 2,
        }
    w, h = _gpt_size(aspect_ratio, resolution)
    label = f"{w}x{h}"
    body: dict[str, Any] = {
        "modelId": model_id,
        "modelVersion": version,
        "n": 1,
        "prompt": text,
        "seeds": [_seed()],
        "output": {"storeInputs": True},
        "referenceBlobs": blobs,
        "caiClaimVersion": 2,
        "generationMetadata": meta,
        "generationSettings": {"detailLevel": 3},
        "modelSpecificPayload": {"size": label},
    }
    if family == "gpt25":
        body["size"] = {"width": w, "height": h}
    return body


def _video_size(ratio: str, resolution: str) -> dict[str, int]:
    tier_key = resolution.strip().lower()
    tier = _VIDEO_SIZE.get(tier_key, _VIDEO_SIZE["720p"])
    w, h = tier.get(ratio, tier["16:9"])
    return {"width": w, "height": h}


def _video_blobs(family: str, reference_blob_ids: list[str] | None) -> list[dict[str, Any]]:
    ids = [item.strip() for item in (reference_blob_ids or []) if str(item).strip()]
    if family == "omni":
        return [{"id": item, "usage": "style"} for item in ids[:4]]
    if family == "seedance":
        return [{"id": item, "usage": "style"} for item in ids[:9]]
    if family == "veo_ref":
        return [{"id": item, "usage": "asset"} for item in ids[:3]]
    return [
        {"id": item, "usage": "general", "promptReference": index + 1}
        for index, item in enumerate(ids[:2])
    ]


def build_video_payload(
    *,
    model: str,
    prompt: str,
    resolution: str,
    aspect_ratio: str,
    duration: int,
    reference_blob_ids: list[str] | None = None,
) -> dict[str, Any]:
    key = model.strip().lower()
    mapped = _VIDEO_MODELS.get(key)
    if mapped is None or key in _DELISTED_VIDEO:
        raise AppError(
            code="model_unsupported", message=f"模型 {model} 暂未接入提交链路", http_status=422
        )
    text = prompt.strip()[:4000]
    ratio = aspect_ratio if aspect_ratio in _VIDEO_RATIOS else "16:9"
    seconds = duration if duration > 0 else 8
    allowed = [str(item).lower() for item in mapped.get("resolutions") or _VIDEO_RESOLUTIONS]
    tier = resolution.strip().lower()
    if tier not in allowed:
        options = ", ".join(mapped.get("resolutions") or _VIDEO_RESOLUTIONS)
        raise AppError(
            code="resolution_unsupported",
            message=f"{model} 不支持 {resolution}，可选：{options}",
            http_status=422,
        )
    size = _video_size(ratio, resolution)
    blobs = _video_blobs(str(mapped["family"]), reference_blob_ids)
    family = str(mapped["family"])
    if family == "omni":
        return {
            "modelId": mapped["model_id"],
            "modelVersion": mapped["model_version"],
            "n": 1,
            "seeds": [_seed()],
            "prompt": text,
            "output": {"storeInputs": True},
            "referenceBlobs": blobs,
            "generationMetadata": {"module": "aura"},
            "size": size,
            "duration": seconds,
            "generationSettings": {"aspectRatio": ratio},
        }
    if family == "seedance":
        return {
            "modelId": mapped["model_id"],
            "modelVersion": mapped["model_version"],
            "size": size,
            "seeds": [_seed()],
            "n": 1,
            "referenceBlobs": blobs,
            "prompt": text,
            "negativePrompt": _SEEDANCE_NEGATIVE,
            "duration": seconds,
            "generateAudio": True,
            "generationMetadata": {"module": "text2video", "submodule": "ff-video-generate"},
            "generationSettings": {"aspectRatio": ratio},
            "output": {"storeInputs": True},
        }
    return {
        "n": 1,
        "seeds": [_seed()],
        "modelId": mapped["model_id"],
        "modelVersion": mapped["model_version"],
        "output": {"storeInputs": True},
        "prompt": text,
        "size": size,
        "generateAudio": True,
        "referenceBlobs": blobs,
        "generationMetadata": {"module": "text2video"},
        "modelSpecificPayload": {
            "parameters": {
                "durationSeconds": seconds,
                "aspectRatio": ratio,
                "addWaterMark": False,
            }
        },
    }
