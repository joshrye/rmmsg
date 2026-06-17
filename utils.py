from datetime import datetime
import json
from pathlib import Path
import uuid

DISCORD_EPOCH = 1420070400000
FALLBACK_CLIENT_BUILD_NUMBER = 595897

DEVICES_FILE = Path(__file__).resolve().parent / "data" / "android_devices.json"


def _load_android_devices() -> list[str]:
    try:
        if DEVICES_FILE.exists():
            with open(DEVICES_FILE, "r", encoding="utf-8") as f:
                devices = json.load(f)
                if isinstance(devices, list) and devices:
                    return devices
    except Exception:
        pass
    return ["husky", "cheetah", "bluejay", "oriole", "dm3q", "a53x", "a23"]


ANDROID_DEVICES: list[str] = _load_android_devices()

def get_random_android_device() -> str:
    import random
    return random.choice(ANDROID_DEVICES)


def sleep_jittered(base_delay: float, jitter_range: float = 0.35, shutdown=None) -> bool:
    """Jittered sleep. Returns True if aborted early via the shutdown event."""
    import random
    import time
    jitter = base_delay * random.uniform(-jitter_range, jitter_range)
    actual_delay = max(0.1, base_delay + jitter + random.uniform(0.02, 0.08))
    if shutdown is not None:
        return shutdown.wait(actual_delay)
    time.sleep(actual_delay)
    return False


def pack_android_version(android_version: tuple) -> str:
    """Turn a (major, minor) tuple into a packed version code ("337010")."""
    major, minor = android_version
    return f"{major}{int(minor):03d}"


def pack_android_build_number(
    android_version: tuple, client_build_number: int | None
) -> int:
    """Combine the Android version code and CI build number."""
    build = (
        FALLBACK_CLIENT_BUILD_NUMBER
        if client_build_number is None
        else client_build_number
    )
    return int(f"{pack_android_version(android_version)}{build:08d}")


def pack_android_client_version(android_version: tuple) -> str:
    """Turn a (major, minor) tuple into a client version ("337.10 - rn")."""
    major, minor = android_version
    return f"{major}.{int(minor)} - rn"

def get_version(component) -> str:
    if component is None:
        return ""
    parts = [component.major, component.minor, component.patch, component.patch_minor]
    return ".".join(parts[:parts.index(None)] if None in parts else parts)


def to_snowflake(unix_seconds: float) -> int:
    ms = int(unix_seconds * 1000)
    return (ms - DISCORD_EPOCH) << 22


def from_snowflake(snowflake: int) -> float:
    ms = (snowflake >> 22) + DISCORD_EPOCH
    return ms / 1000


def iso_to_unix(iso_string: str) -> int:
    return int(datetime.fromisoformat(iso_string).timestamp())


def generate_launch_signature() -> str:
    bits = 0b00000000100000000001000000010000000010000001000000001000000000000010000010000001000000000100000000000001000000000000100000000000

    # Force all bits to 0
    random_uuid = uuid.uuid4().int & (~bits & ((1 << 128) - 1))
    result = uuid.UUID(int=random_uuid)
    return str(result)
