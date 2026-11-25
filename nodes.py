"""ComfyUI nodes for Google Flow (Veo 3.1, Omni 1.2 Flash, Nano Banana) through the useapi.net API.

The user brings their own Google account with a Google AI plan (connected once at useapi.net) and a useapi.net
API token. Images and videos are made on that account's Google Flow allowance.
"""

from __future__ import annotations

import base64
import binascii
import io
import os
import time
from typing import Any

from .useapi_flow.client import SYNC_TIMEOUT, SUBMIT_TIMEOUT, Client, UseapiError, account_of, find_token, media_items

CATEGORY = "useapi.net/Google Flow"

# Active models only (catalog lifecycle.status == "active"); retiring models are not offered.
IMAGE_MODELS = ["nano-banana-2-lite", "nano-banana-2", "nano-banana-pro"]
IMAGE_ASPECTS = ["16:9", "4:3", "1:1", "3:4", "9:16", "auto"]  # auto = the first reference image's shape
VIDEO_MODELS = ["veo-3.1-lite", "omni-flash-1.2"]
VEO_LITE_DURATIONS = (4, 6, 8)
UPSCALE_RESOLUTIONS = ["1080p", "4K", "720p"]
MAX_REFERENCES = 10  # images: reference_1..reference_10
MAX_SEED = 2**31 - 1

EMAIL_TIP = "Google account connected at useapi.net. Leave empty to let useapi.net pick one."


# --------------------------------------------------------------------------- ComfyUI helpers (imported lazily)


def _client() -> Client:
    """The token comes from USEAPI_TOKEN or ComfyUI/user/useapi_token.txt, never from a node input (see find_token)."""
    import folder_paths

    user_dir = folder_paths.get_user_directory() if hasattr(folder_paths, "get_user_directory") else None
    return Client(find_token(user_dir))


def _check_interrupt() -> None:
    import comfy.model_management

    comfy.model_management.throw_exception_if_processing_interrupted()


def _sleeper(pbar: Any, max_wait: float):
    """Sleep in 1 s steps so Cancel in ComfyUI stops the wait quickly; move the progress bar while waiting."""

    def sleep(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            _check_interrupt()
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

    def tick(elapsed: float) -> None:
        if pbar is not None:
            pbar.update_absolute(int(min(elapsed, max_wait)), int(max_wait))

    return sleep, tick


def _progress(total: int) -> Any:
    try:
        import comfy.utils

        return comfy.utils.ProgressBar(total)
    except Exception:  # noqa: BLE001 - progress is cosmetic
        return None


def _tensor_to_png(image: Any) -> bytes:
    """One ComfyUI IMAGE frame ([H, W, C] float 0..1) as PNG bytes."""
    import numpy as np
    from PIL import Image

    array = np.clip(image.cpu().numpy() * 255.0, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(array).save(buf, format="PNG")
    return buf.getvalue()


def _images_to_tensor(blobs: list[bytes]) -> Any:
    """Decode images into a ComfyUI IMAGE batch [B, H, W, 3]; later images are resized to the first one's size."""
    import numpy as np
    import torch
    from PIL import Image

    frames = []
    size = None
    for blob in blobs:
        try:
            img = Image.open(io.BytesIO(blob)).convert("RGB")
        except Exception as e:  # noqa: BLE001 - PIL raises several types for a bad file
            raise UseapiError(f"Google Flow returned an image that could not be read ({e.__class__.__name__}). Try again.") from e
        if size is None:
            size = img.size
        elif img.size != size:
            img = img.resize(size, Image.LANCZOS)
        frames.append(torch.from_numpy(np.asarray(img).astype(np.float32) / 255.0))
    return torch.stack(frames)


def _save_video(blob: bytes, stem: str) -> tuple[Any, str, dict[str, Any]]:
    """Save the MP4 under output/useapi-google-flow/ and return (VIDEO, path, preview entry)."""
    import folder_paths

    subfolder = "useapi-google-flow"
    folder = os.path.join(folder_paths.get_output_directory(), subfolder)
    os.makedirs(folder, exist_ok=True)
    name = f"{stem}_{time.strftime('%Y%m%d-%H%M%S')}_{os.urandom(3).hex()}.mp4"
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(blob)
    try:
        from comfy_api.input_impl import VideoFromFile
    except ImportError as e:  # ComfyUI older than the VIDEO type
        raise UseapiError(f"This ComfyUI is too old for video outputs; update ComfyUI. The video was saved to {path}") from e
    return VideoFromFile(path), path, {"filename": name, "subfolder": subfolder, "type": "output"}


def _download_first_video(client: Client, job: dict[str, Any]) -> tuple[str, bytes]:
    items = [i for i in media_items(job) if i["kind"] == "video"]
    good = [i for i in items if i["ok"]]
    if not good:
        reason = next((i.get("error") for i in items if i.get("error")), None)
        raise UseapiError(f"Google Flow finished job {job.get('jobid')} without a video{': ' + reason if reason else ''}.")
    item = good[0]
    media_id = item.get("id") or ""
    try:
        if item.get("url"):
            blob, _mime = client.download(item["url"])
        elif media_id:  # made, but Google has not produced the link yet: fetch it through api.useapi.net instead
            blob, _mime = client.download_video_raw(media_id)
        else:
            raise UseapiError("the answer carried neither a link nor a media id")
    except UseapiError as e:
        raise UseapiError(
            f"The video was made (job {job.get('jobid')}, media id {media_id or 'unknown'}) but could not be downloaded: {e} "
            "Do not queue it again: fetch it later with GET /assets/{media id}?raw=true or from your Google Flow project."
        ) from e
    return media_id, blob


def _run(fn):
    """Show UseapiError as a plain message in ComfyUI (no traceback noise)."""
    try:
        return fn()
    except UseapiError as e:
        raise RuntimeError(f"Google Flow (useapi.net): {e}") from None


# --------------------------------------------------------------------------- nodes


class GoogleFlowImage:
    """Nano Banana images on your own Google AI plan, through Google Flow."""

    DESCRIPTION = (
        "Make images with Nano Banana 2 Lite, Nano Banana 2.1 or Nano Banana Pro in Google Flow, on your own Google AI plan, "
        "through the useapi.net API. Optional reference images edit or guide the result."
    )
    CATEGORY = CATEGORY
    FUNCTION = "generate"
    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "media_ids")
    OUTPUT_TOOLTIPS = ("The generated images.", "Google Flow media ids, one per line.")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "", "tooltip": "What to make. With reference images, say what to change."}),
                "model": (IMAGE_MODELS, {"default": "nano-banana-2-lite", "tooltip": "nano-banana-2 is Nano Banana 2.1."}),
                "aspect_ratio": (IMAGE_ASPECTS, {"default": "16:9", "tooltip": "auto keeps the first reference image's shape."}),
                "count": ("INT", {"default": 1, "min": 1, "max": 4, "tooltip": "Images per run (1 to 4)."}),
                "seed": ("INT", {"default": 0, "min": 0, "max": MAX_SEED, "control_after_generate": True}),
            },
            "optional": {
                "reference_images": ("IMAGE", {"tooltip": "Up to 10 images (a batch). Each is uploaded to the Google account first."}),
                "email": ("STRING", {"default": "", "tooltip": EMAIL_TIP}),
            },
        }

    def generate(self, prompt, model, aspect_ratio, count, seed, reference_images=None, email=""):
        return _run(lambda: self._generate(prompt, model, aspect_ratio, count, seed, reference_images, email))

    def _generate(self, prompt, model, aspect_ratio, count, seed, reference_images, email):
        if not prompt.strip():
            raise UseapiError("The prompt is empty.")
        if aspect_ratio == "auto" and reference_images is None:
            raise UseapiError("aspect_ratio 'auto' takes the first reference image's shape: connect reference_images or pick a ratio.")
        client = _client()
        email = email.strip() or None
        body: dict[str, Any] = {"prompt": prompt, "model": model, "aspectRatio": aspect_ratio, "count": count, "seed": seed}
        if reference_images is not None:
            if reference_images.shape[0] > MAX_REFERENCES:
                raise UseapiError(f"Give at most {MAX_REFERENCES} reference images (got {reference_images.shape[0]}).")
            for n, frame in enumerate(reference_images, 1):
                _check_interrupt()
                ref, email = client.upload(_tensor_to_png(frame), "image/png", email)
                body[f"reference_{n}"] = ref
        if email:
            body["email"] = email
        _check_interrupt()
        answer = client.request("POST", "images", body=body, timeout=SYNC_TIMEOUT)  # images answer in sync mode only
        items = [i for i in media_items(answer if isinstance(answer, dict) else {}) if i["kind"] == "image"]
        blobs: list[bytes] = []
        ids: list[str] = []
        for item in items:
            blob = None
            if item.get("b64"):
                try:
                    blob = base64.b64decode(item["b64"])
                except (binascii.Error, ValueError):
                    blob = None
            if blob is None and item.get("url"):
                blob, _mime = client.download(item["url"])
            if blob:
                blobs.append(blob)
                if item.get("id"):
                    ids.append(item["id"])
        if not blobs:
            raise UseapiError("Google Flow returned no image. Moderation varies between tries: retry or change the prompt.")
        return (_images_to_tensor(blobs), "\n".join(ids))


class GoogleFlowVideo:
    """Veo 3.1 Lite or Omni 1.2 Flash videos on your own Google AI plan, through Google Flow."""

    DESCRIPTION = (
        "Make a video with Veo 3.1 Lite or Omni 1.2 Flash in Google Flow, on your own Google AI plan, through the useapi.net "
        "API. Text to video, or start from an image (and optionally end on one). Saves the MP4 under output/useapi-google-flow."
    )
    CATEGORY = CATEGORY
    FUNCTION = "generate"
    OUTPUT_NODE = True
    RETURN_TYPES = ("VIDEO", "STRING", "STRING")
    RETURN_NAMES = ("video", "media_id", "file_path")
    OUTPUT_TOOLTIPS = (
        "The generated video.",
        "Google Flow media id: connect it to Google Flow Upscale Video.",
        "Where the MP4 was saved.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": ""}),
                "model": (VIDEO_MODELS, {"default": "veo-3.1-lite"}),
                "aspect_ratio": (["landscape", "portrait"], {"default": "landscape"}),
                "duration": (
                    "INT",
                    {
                        "default": 8,
                        "min": 3,
                        "max": 15,
                        "tooltip": "Seconds. Veo 3.1 Lite: 4, 6 or 8 (4 and 6 need Google AI Ultra). "
                        "Omni 1.2 Flash: 3 to 15 (above 10 on a paid plan; 3 to 10 with start and end images).",
                    },
                ),
                "resolution": (["720p", "360p"], {"default": "720p", "tooltip": "360p is Omni 1.2 Flash only."}),
                "seed": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "max": MAX_SEED,
                        "control_after_generate": True,
                        "tooltip": "Not sent to Google: change it to make a new take with the same settings.",
                    },
                ),
            },
            "optional": {
                "start_image": ("IMAGE", {"tooltip": "First frame (the first image of a batch is used)."}),
                "end_image": ("IMAGE", {"tooltip": "Last frame. Needs a start image."}),
                "email": ("STRING", {"default": "", "tooltip": EMAIL_TIP}),
                "max_wait_seconds": ("INT", {"default": 900, "min": 60, "max": 3600, "tooltip": "Stop waiting after this long."}),
            },
        }

    def generate(self, prompt, model, aspect_ratio, duration, resolution, seed, start_image=None, end_image=None,
                 email="", max_wait_seconds=900):
        return _run(lambda: self._generate(prompt, model, aspect_ratio, duration, resolution, start_image, end_image, email, max_wait_seconds))

    def _generate(self, prompt, model, aspect_ratio, duration, resolution, start_image, end_image, email, max_wait):
        if not prompt.strip():
            raise UseapiError("The prompt is empty.")
        if end_image is not None and start_image is None:
            raise UseapiError("An end image needs a start image too (end-frame-only is not supported).")
        if model == "veo-3.1-lite":
            if duration not in VEO_LITE_DURATIONS:
                raise UseapiError("Veo 3.1 Lite makes 4, 6 or 8 second videos (4 and 6 need Google AI Ultra).")
            if resolution != "720p":
                raise UseapiError("Veo 3.1 Lite makes 720p only; 360p is Omni 1.2 Flash only.")
        elif start_image is not None and end_image is not None and duration > 10:
            raise UseapiError("Omni 1.2 Flash with start and end images makes 3 to 10 second videos.")
        client = _client()
        email = email.strip() or None
        body: dict[str, Any] = {
            "prompt": prompt,
            "model": model,
            "aspectRatio": aspect_ratio,
            "duration": duration,
            "resolution": resolution,
            "count": 1,
            "async": True,
        }
        for field, image in (("startImage", start_image), ("endImage", end_image)):
            if image is not None:
                _check_interrupt()
                body[field], email = client.upload(_tensor_to_png(image[0]), "image/png", email)
        if email:
            body["email"] = email
        _check_interrupt()
        answer = client.request("POST", "videos", body=body, timeout=SUBMIT_TIMEOUT)
        pbar = _progress(max_wait)
        sleep, tick = _sleeper(pbar, max_wait)
        job = client.poll(answer, max_wait, sleep=sleep, on_tick=tick)
        media_id, blob = _download_first_video(client, job)
        video, path, preview = _save_video(blob, model)
        return {"ui": {"images": [preview], "animated": (True,)}, "result": (video, media_id, path)}


class GoogleFlowUpscaleVideo:
    """Upscale a Google Flow video to 720p, 1080p or 4K on your own Google AI plan."""

    DESCRIPTION = (
        "Upscale a Google Flow video (up to 15 s) to 720p (for 360p clips), 1080p or 4K, through the useapi.net API. "
        "Connect media_id from Google Flow Video, or paste a media id. 720p and 1080p need a paid Google AI plan; 4K needs Ultra."
    )
    CATEGORY = CATEGORY
    FUNCTION = "upscale"
    OUTPUT_NODE = True
    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "file_path")
    OUTPUT_TOOLTIPS = ("The upscaled video.", "Where the MP4 was saved.")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "media_id": ("STRING", {"default": "", "tooltip": "The media id of a Google Flow video."}),
                "resolution": (
                    UPSCALE_RESOLUTIONS,
                    {"default": "1080p", "tooltip": "720p and 1080p need a paid Google AI plan (Plus, Pro or Ultra); 4K needs Google AI Ultra."},
                ),
            },
            "optional": {
                "max_wait_seconds": ("INT", {"default": 900, "min": 60, "max": 3600, "tooltip": "Stop waiting after this long."}),
            },
        }

    def upscale(self, media_id, resolution, max_wait_seconds=900):
        return _run(lambda: self._upscale(media_id, resolution, max_wait_seconds))

    def _upscale(self, media_id, resolution, max_wait):
        media_id = (media_id or "").strip()
        if not media_id:
            raise UseapiError("Give the media id of a Google Flow video (connect media_id from Google Flow Video).")
        if not account_of(media_id):
            raise UseapiError("That does not look like a Google Flow media id (user:...-email:...-video:...).")
        client = _client()
        body = {"mediaGenerationId": media_id, "resolution": resolution, "async": True}
        answer = client.request("POST", "videos/upscale", body=body, timeout=SUBMIT_TIMEOUT)
        pbar = _progress(max_wait)
        sleep, tick = _sleeper(pbar, max_wait)
        job = client.poll(answer, max_wait, sleep=sleep, on_tick=tick)
        _id, blob = _download_first_video(client, job)
        video, path, preview = _save_video(blob, f"upscaled-{resolution.lower()}")
        return {"ui": {"images": [preview], "animated": (True,)}, "result": (video, path)}


NODE_CLASS_MAPPINGS = {
    "UseapiGoogleFlowImage": GoogleFlowImage,
    "UseapiGoogleFlowVideo": GoogleFlowVideo,
    "UseapiGoogleFlowUpscaleVideo": GoogleFlowUpscaleVideo,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "UseapiGoogleFlowImage": "Google Flow Image (Nano Banana)",
    "UseapiGoogleFlowVideo": "Google Flow Video (Veo 3.1 Lite, Omni 1.2 Flash)",
    "UseapiGoogleFlowUpscaleVideo": "Google Flow Upscale Video (720p, 1080p, 4K)",
}
