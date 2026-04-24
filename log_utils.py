import json
import os
import re
import time
import uuid
import contextvars
from typing import Any, Optional


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
DEBUG_THROTTLE_STATE = {}
REQUEST_LOG_PATH: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_log_path", default=None)


def ensure_logs_dir():
    os.makedirs(LOGS_DIR, exist_ok=True)


def get_daily_log_file(component: str):
    ensure_logs_dir()
    safe_component = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(component or "app"))
    date_str = time.strftime("%Y-%m-%d")
    return os.path.join(LOGS_DIR, f"{safe_component}-{date_str}.log")


def start_request_log(component: str, label: str = "request", request_id: Optional[str] = None):
    ensure_logs_dir()
    safe_component = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(component or "app"))
    safe_label = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(label or "request"))
    safe_request_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(request_id or uuid.uuid4().hex[:8]))
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    milliseconds = int((time.time() % 1) * 1000)
    log_path = os.path.join(
        LOGS_DIR,
        f"{safe_component}-{timestamp}-{milliseconds:03d}-{safe_label}-{safe_request_id}.log",
    )
    token = REQUEST_LOG_PATH.set(log_path)
    return token, log_path


def reset_request_log(token):
    try:
        REQUEST_LOG_PATH.reset(token)
    except ValueError:
        REQUEST_LOG_PATH.set(None)


def _mask_long_secret(value: str):
    if len(value) <= 12:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def redact_sensitive_text(text: str):
    if not isinstance(text, str) or not text:
        return text

    redacted = text

    redacted = re.sub(
        r'(Authorization["\']?\s*[:=]\s*["\']?)([^"\',\s}]+)',
        lambda m: m.group(1) + _mask_long_secret(m.group(2)),
        redacted,
        flags=re.IGNORECASE,
    )

    redacted = re.sub(
        r'(["\']?(?:at|SNlM0e|__Secure-1PSID|__Secure-1PSIDTS|SAPISID)["\']?\s*[:=]\s*["\'])(.*?)(["\'])',
        lambda m: m.group(1) + _mask_long_secret(m.group(2)) + m.group(3),
        redacted,
        flags=re.IGNORECASE,
    )

    redacted = re.sub(
        r'((?:__Secure-1PSID|__Secure-1PSIDTS|SAPISID)=)([^;\s]+)',
        lambda m: m.group(1) + _mask_long_secret(m.group(2)),
        redacted,
        flags=re.IGNORECASE,
    )

    return redacted


def sanitize_for_log(value: Any, max_len: int = 8000):
    try:
        if isinstance(value, (dict, list, tuple)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
    except Exception:
        text = repr(value)

    text = redact_sensitive_text(text)
    if len(text) > max_len:
        return text[:max_len] + f" ...[truncated {len(text) - max_len} chars]"
    return text


def sanitize_headers(headers: Any):
    if headers is None:
        return {}
    try:
        items = dict(headers.items()) if hasattr(headers, "items") else dict(headers)
    except Exception:
        return sanitize_for_log(headers)

    masked = {}
    for key, value in items.items():
        if str(key).lower() in {"authorization", "cookie", "set-cookie", "x-goog-authuser", "x-goog-pageid"}:
            masked[key] = "***"
        else:
            masked[key] = value
    return masked


def log_line(component: str, message: str, console: bool = True):
    timestamp = time.strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {message}"

    if console:
        import sys
        console_text = f"\r\033[K{formatted_msg}\n"
        try:
            sys.stdout.write(console_text)
        except UnicodeEncodeError:
            safe_encoding = sys.stdout.encoding or "utf-8"
            sys.stdout.write(console_text.encode(safe_encoding, errors="replace").decode(safe_encoding, errors="replace"))
        sys.stdout.flush()

    try:
        log_file = REQUEST_LOG_PATH.get() or get_daily_log_file(component)
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(formatted_msg + "\n")
    except Exception:
        pass


def debug_log(component: str, enabled: bool, label: str, data: Any = None, max_len: int = 8000):
    if not enabled:
        return
    if data is None:
        log_line(component, f"[DEBUG] {label}")
        return
    log_line(component, f"[DEBUG] {label}: {sanitize_for_log(data, max_len=max_len)}")


def debug_log_throttled(
    component: str,
    enabled: bool,
    throttle_key: str,
    label: str,
    data: Any = None,
    max_len: int = 8000,
    interval_seconds: float = 8.0,
    force: bool = False,
):
    if not enabled:
        return

    state_key = f"{component}:{throttle_key}"
    now = time.monotonic()
    last_emitted_at = DEBUG_THROTTLE_STATE.get(state_key)

    if not force and last_emitted_at is not None and (now - last_emitted_at) < interval_seconds:
        return

    DEBUG_THROTTLE_STATE[state_key] = now
    debug_log(component, enabled, label, data=data, max_len=max_len)
