"""HTTP client for the useapi.net Google Flow API.

API requests go to https://api.useapi.net/v1/google-flow with the user's useapi.net API token as a Bearer token.
Finished images and videos are downloaded from Google's signed links on https://flow-content.google, without the
token. Nothing else is contacted.

API reference: https://useapi.net/docs/api-google-flow-v1
API catalog (models, inputs, errors): https://api.useapi.net/v1/google-flow/models
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import requests

VERSION = "0.1.0"
BASE_URL = "https://api.useapi.net/v1/google-flow"
USER_AGENT = f"useapi-comfyui/{VERSION}"
MEDIA_HOST = "flow-content.google"

SETUP_TOKEN_URL = "https://useapi.net/docs/start-here/setup-useapi?utm_source=comfyui&utm_medium=node"
SETUP_ACCOUNT_URL = "https://useapi.net/docs/start-here/setup-google-flow?utm_source=comfyui&utm_medium=node"
CAPTCHA_URL = SETUP_ACCOUNT_URL + "#configure-captcha-providers"
SUBSCRIPTION_URL = "https://useapi.net/docs/subscription?utm_source=comfyui&utm_medium=node"

TOKEN_ENV = "USEAPI_TOKEN"
TOKEN_FILE = "useapi_token.txt"

# Seconds. A generation request waits for its captcha before it answers, which can take minutes.
DEFAULT_TIMEOUT = 60
SUBMIT_TIMEOUT = 240
SYNC_TIMEOUT = 300
UPLOAD_TIMEOUT = 300
DOWNLOAD_TIMEOUT = 300
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024

RUNNING = ("created", "started")  # job states before completed / failed


class UseapiError(Exception):
    """A readable error for the ComfyUI user."""

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


def enc(value: str) -> str:
    """URL-encode an id or email for use in a path. Ids contain ':' and '@'."""
    return quote(str(value), safe="")


def find_token(user_dir: str | None) -> str:
    """The USEAPI_TOKEN environment variable, else <ComfyUI user dir>/useapi_token.txt.

    There is deliberately no token field on the nodes: ComfyUI copies node inputs into saved workflows, the
    metadata of every saved PNG/MP4, its history and error reports.
    """
    value = os.environ.get(TOKEN_ENV)
    if value and value.strip():
        return value.strip()
    if user_dir:
        path = os.path.join(user_dir, TOKEN_FILE)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
    raise UseapiError(
        "The useapi.net API token is missing. Set the USEAPI_TOKEN environment variable before starting ComfyUI, "
        f"or put the token in ComfyUI/user/{TOKEN_FILE}. How to get one: {SETUP_TOKEN_URL}"
    )


def account_of(ref: str | None) -> str | None:
    """The Google account an image or video id belongs to.

    Ids look like user:12345-email:<hex of the email>-image:<id> (documented on POST /assets/{email}).
    """
    if not ref:
        return None
    match = re.search(r"-email:([0-9a-fA-F]+)-", str(ref))
    if not match or len(match.group(1)) % 2:
        return None
    try:
        email = bytes.fromhex(match.group(1)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return email if "@" in email else None


def _server_message(body: Any) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("status") or err.get("code")
            msg = err.get("message") or ""
            return f"{code}: {msg}" if code else msg
        if err:
            return str(err)
    if isinstance(body, str):
        return body.strip()[:500]
    return ""


def _retry_after(body: Any, headers: Any) -> str:
    seconds = headers.get("Retry-After") if headers is not None else None
    if seconds:
        return f" Retry after {seconds} seconds."
    if isinstance(body, dict) and body.get("retryAfter"):
        return f" Retry after {body['retryAfter']}."
    return ""


def describe_error(status: int, body: Any, headers: Any = None) -> str:
    """Turn an HTTP error from the API into one or two readable sentences (meanings from the API catalog's `errors`)."""
    server = _server_message(body)
    detail = f" Details: {server}" if server else ""
    later = _retry_after(body, headers)

    if status in (403, 429) and server.startswith("captcha_quality:"):
        return (
            f"Google refused the captcha on every try ({status}), so nothing was generated and no Google Flow credits were used. "
            f"If it keeps happening, use another Google account, or leave the email empty to let useapi.net pick one.{detail}"
        )
    if status == 401:
        return f"useapi.net rejected the API token (401 Unauthorized). Check USEAPI_TOKEN. How to get one: {SETUP_TOKEN_URL}"
    if status == 402:
        return f"Your useapi.net subscription is not active (402 Payment Required). See {SUBSCRIPTION_URL}.{detail}"
    if status == 400:
        if server.startswith("No captcha provider API keys configured"):
            return f"The 300 free captcha credits are used up (400). Add your own captcha provider key to keep generating: {CAPTCHA_URL}{detail}"
        if "need a Google AI Ultra plan" in server:
            return f"This length needs a Google AI Ultra plan (400). Use 8 seconds, or an account on Google AI Ultra.{detail}"
        if any(s in server for s in ("paid Google Flow plan", "needs a paid Google AI plan", "upscaling is not available", ", which is on ")):
            return f"The Google account's plan does not include this (400). Use another account or a different option.{detail}"
        if server.startswith("Google has not given"):
            return f"Omni 1.2 Flash has not reached this Google account yet (400). Use another account or Veo 3.1 Lite.{detail}"
        return f"The request was refused (400). Change what the details say and try again.{detail}"
    if status == 403:
        if "PUBLIC_ERROR_MODEL_ACCESS_DENIED" in server:
            return f"Google refused this model on this account (403): its plan does not include it. Use another account or model.{detail}"
        if "PUBLIC_ERROR_USER_MINOR" in server:
            return f"Google treats this Google account as under 18 (403), so Google Flow is not available to it. Use another account.{detail}"
        return f"Refused (403): the id belongs to another useapi.net user, or Google refused this request for the account.{detail}"
    if status == 404:
        hint = ""
        if "account" in server.lower() and "not found" in server.lower():
            hint = f" Connect the Google account first: {SETUP_ACCOUNT_URL}"
        return f"Not found (404).{detail}{hint}"
    if status == 408:
        return f"Still running when the wait ended (408). Check your jobs before sending it again.{detail}"
    if status == 409:
        return f"Busy with an earlier request (409). Wait for it to end and retry.{detail}"
    if status == 410:
        return "The job has expired (410). Jobs are kept for 7 days."
    if status == 429:
        if server == "no_eligible_account":
            return f"Every connected Google account is set aside for this model right now (429).{later} Connect more Google accounts, or try another model.{detail}"
        return f"Google limits this account for now (429).{later} Use another account, or leave the email empty so useapi.net picks a free one.{detail}"
    if status == 502:
        return f"Google answered with an error or no readable reply (502). The request may have run: check before sending it again.{detail}"
    if status == 503:
        if server.startswith("Captcha service failed:"):
            return f"Your captcha provider failed (503): check its API key and balance. {CAPTCHA_URL}{detail}"
        return f"Google Flow is briefly unavailable (503).{later or ' Wait 5 to 10 seconds and retry.'}{detail}"
    if status == 504:
        return f"Google did not answer in time (504). The request may still run: check before sending it again.{detail}"
    if status == 596:
        return (
            "Google rejected this account's session (596). useapi.net re-verifies it on its own: retry in about a minute. "
            f"If it keeps answering 596, add the account again: {SETUP_ACCOUNT_URL}{detail}"
        )
    if status >= 500:
        return f"Something failed on the useapi.net side ({status}). Retry in a minute.{detail}"
    return f"Request failed ({status}).{detail}"


def _parse_body(resp: requests.Response) -> Any:
    if not resp.text:
        return None
    try:
        return resp.json()
    except ValueError:
        return resp.text


def is_media_url(url: str) -> bool:
    """Only Google's signed media links on flow-content.google are downloaded."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname == MEDIA_HOST and not parts.username and port in (None, 443)


def _get(obj: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def media_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The media of a job record or a sync answer: kind, id, url, base64, ok, error."""
    media = record.get("media")
    if media is None:
        media = _get(record, "response", "media")
    items: list[dict[str, Any]] = []
    for m in media if isinstance(media, list) else []:
        if not isinstance(m, dict):
            continue
        image = _get(m, "image", "generatedImage")
        if isinstance(image, dict):
            items.append(
                {
                    "kind": "image",
                    "id": image.get("mediaGenerationId") or m.get("mediaGenerationId"),
                    "url": image.get("fifeUrl"),
                    "b64": image.get("encodedImage"),
                    "ok": True,
                    "error": None,
                }
            )
            continue
        status = _get(m, "mediaMetadata", "mediaStatus", "mediaGenerationStatus")
        items.append(
            {
                "kind": "video",
                "id": m.get("mediaGenerationId"),
                "url": m.get("videoUrl"),
                "b64": None,
                "ok": status in (None, "MEDIA_GENERATION_STATUS_SUCCESSFUL"),
                "error": _get(m, "mediaMetadata", "mediaStatus", "error", "message"),
            }
        )
    return items


def failure_text(job: dict[str, Any]) -> str:
    reasons = _get(job, "response", "failureReasons") or []
    error = job.get("error")
    if isinstance(error, dict):
        error = error.get("message")
    line = f"The Google Flow {job.get('type') or 'generation'} job failed: {error or 'no reason given'}."
    if reasons:
        line += " Google's reasons: " + ", ".join(str(r) for r in reasons) + "."
    joined = " ".join(str(r) for r in reasons) + " " + str(error or "")
    if "AUDIO_FILTERED" in joined:
        line += " Google's audio filter rejected the sound made with the video. Queue it again (each try makes new sound), or use Omni 1.2 Flash."
    elif "IP_PROHIBITED" in joined or "IP_INPUT_IMAGE" in joined:
        line += " Google flagged an input image as protected content. Replace the image; a retry with the same one fails again."
    elif reasons or "PUBLIC_ERROR" in joined:
        line += " Moderation varies between tries: retry, change the prompt or the images, or switch models."
    return line + f" jobid: {job.get('jobid') or job.get('jobId')}"


class Client:
    def __init__(self, token: str, session: requests.Session | None = None, base_url: str = BASE_URL):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.http = session or requests.Session()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json", "User-Agent": USER_AGENT}
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Any:
        """Send one request and return the parsed JSON body, or raise UseapiError."""
        extra: dict[str, str] = {}
        payload: bytes | None = None
        if body is not None:
            extra["Content-Type"] = "application/json"
            payload = json.dumps(body).encode("utf-8")
        elif data is not None:
            extra["Content-Type"] = content_type or "application/octet-stream"
            payload = data
        try:
            resp = self.http.request(
                method, f"{self.base_url}/{path.lstrip('/')}", data=payload, headers=self._headers(extra), timeout=timeout
            )
        except requests.Timeout as e:
            raise UseapiError(
                f"No answer from api.useapi.net within {int(timeout)} seconds. The request may still complete: "
                "check your jobs before queueing it again."
            ) from e
        except requests.RequestException as e:
            raise UseapiError(f"Could not reach api.useapi.net: {e.__class__.__name__}") from e
        parsed = _parse_body(resp)
        if resp.status_code < 400:
            return parsed if parsed is not None else {}
        raise UseapiError(describe_error(resp.status_code, parsed, resp.headers), resp.status_code, parsed)

    def upload(self, blob: bytes, mime: str, email: str | None) -> tuple[str, str | None]:
        """Upload an image to the account's Google Flow assets. Returns (media id, email)."""
        path = f"assets/{enc(email)}" if email else "assets"
        data = self.request("POST", path, data=blob, content_type=mime, timeout=UPLOAD_TIMEOUT)
        holder = data.get("mediaGenerationId") if isinstance(data, dict) else None
        ref = holder.get("mediaGenerationId") if isinstance(holder, dict) else holder
        if not ref:
            raise UseapiError("Google Flow did not return an id for the uploaded image. Try again.")
        return ref, email or (data.get("email") if isinstance(data, dict) else None) or account_of(ref)

    def poll(
        self,
        answer: Any,
        max_wait: float,
        sleep: Callable[[float], None] = time.sleep,
        on_tick: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        every: float = 10,
    ) -> dict[str, Any]:
        """Wait for an async job to finish. Returns the completed job record or raises UseapiError."""
        job = answer if isinstance(answer, dict) else {}
        if "jobid" not in job and job.get("jobId"):
            job = {**job, "jobid": job["jobId"]}
        if "status" not in job and job.get("media") is not None:
            job = {**job, "status": "completed"}
        started = clock()
        while job.get("status") in RUNNING and job.get("jobid"):
            elapsed = clock() - started
            if elapsed >= max_wait:
                raise UseapiError(
                    f"Google Flow is still working on job {job['jobid']} after {int(max_wait)} seconds. "
                    "Raise max_wait_seconds, or fetch it later from GET /jobs/{jobid}. Do not queue it again."
                )
            if on_tick:
                on_tick(elapsed)
            sleep(min(every, max_wait - elapsed))
            try:
                job = self.request("GET", f"jobs/{enc(job['jobid'])}")
            except UseapiError as e:
                if e.status in (401, 402, 403, 404, 410):
                    raise
                # A hiccup (5xx, 408, network) must not end the wait: the job keeps running.
        if job.get("status") == "failed":
            raise UseapiError(failure_text(job), body=job)
        return job

    def download(self, url: str, timeout: float = DOWNLOAD_TIMEOUT) -> tuple[bytes, str]:
        """Download a signed Google Flow media link (no token is sent). Returns (bytes, content type)."""
        if not is_media_url(url):
            raise UseapiError(f"Refusing to download a file from outside https://{MEDIA_HOST}.")
        return self._stream(url, {"User-Agent": USER_AGENT, "Accept": "*/*"}, timeout, MEDIA_HOST)

    def download_video_raw(self, media_id: str, timeout: float = DOWNLOAD_TIMEOUT) -> tuple[bytes, str]:
        """Download a finished video through api.useapi.net (GET /assets/{id}?raw=true, video only).

        Used when Google has made the video but has not produced its download link yet.
        """
        url = f"{self.base_url}/assets/{enc(media_id)}?raw=true"
        return self._stream(url, self._headers({"Accept": "*/*"}), timeout, "api.useapi.net")

    def _stream(self, url: str, headers: dict[str, str], timeout: float, host: str) -> tuple[bytes, str]:
        try:
            resp = self.http.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=False)
        except requests.RequestException as e:
            raise UseapiError(f"Could not download the file from {host}: {e.__class__.__name__}") from e
        if resp.status_code >= 300:
            body = _parse_body(resp) if host == "api.useapi.net" else None
            resp.close()
            if body is not None:
                raise UseapiError(describe_error(resp.status_code, body, resp.headers), resp.status_code, body)
            raise UseapiError(f"The media link answered {resp.status_code}. Links expire about 6 hours after they are made.")
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise UseapiError("The file is larger than 500 MB; download it from its link instead.")
                chunks.append(chunk)
        except requests.RequestException as e:
            raise UseapiError(f"The download from {host} broke off: {e.__class__.__name__}. Try again.") from e
        finally:
            resp.close()
        return b"".join(chunks), resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
