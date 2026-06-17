#!/usr/bin/env python3

import collections
import concurrent.futures
import enum
import inspect
import json
import logging
import random
import threading
import time
import urllib.parse
import uuid
import zlib
from typing import Any, Callable

import select
import socket
from curl_cffi import requests
from curl_cffi.curl import CurlECode, CurlError, CurlInfo, CurlWsFlag
from curl_cffi.requests.websockets import (
    CURL_SOCKET_BAD,
    WebSocket,
    WebSocketClosed,
    WebSocketError,
)
logger = logging.getLogger("discord.gateway")

ZLIB_SUFFIX = b"\x00\x00\xff\xff"
DEFAULT_GATEWAY_URL = "https://gateway.discord.gg"
MAX_PAYLOAD_SIZE = 15360  # 15 KiB per Discord docs
RATE_LIMIT_MAX_EVENTS = 120
RATE_LIMIT_PERIOD = 60.0  # seconds
PRESENCE_RATE_LIMIT_MAX = 5
PRESENCE_RATE_LIMIT_PERIOD = 20.0  # seconds
GUILD_MEMBERS_RATE_LIMIT_PERIOD = 30.0  # seconds
TIME_SPENT_INTERVAL = 1800.0  # 30 minutes in seconds
MAX_RESUME_ATTEMPTS = 2


class GatewayOpcode(enum.IntEnum):
    DISPATCH = 0
    HEARTBEAT = 1
    IDENTIFY = 2
    PRESENCE_UPDATE = 3
    VOICE_STATE_UPDATE = 4
    VOICE_SERVER_PING = 5
    RESUME = 6
    RECONNECT = 7
    REQUEST_GUILD_MEMBERS = 8
    INVALID_SESSION = 9
    HELLO = 10
    HEARTBEAT_ACK = 11
    CALL_CONNECT = 13
    GUILD_SUBSCRIPTIONS = 14
    LOBBY_VOICE_STATES = 17
    STREAM_CREATE = 18
    STREAM_DELETE = 19
    STREAM_WATCH = 20
    STREAM_PING = 21
    STREAM_SET_PAUSED = 22
    REQUEST_FORUM_UNREADS = 28
    REMOTE_COMMAND = 29
    REQUEST_DELETED_ENTITY_IDS = 30
    REQUEST_SOUNDBOARD_SOUNDS = 31
    REQUEST_LAST_MESSAGES = 34
    SEARCH_RECENT_MEMBERS = 35
    GUILD_SUBSCRIPTIONS_BULK = 37
    GUILD_CHANNELS_RESYNC = 38
    REQUEST_CHANNEL_MEMBER_COUNT = 39
    QOS_HEARTBEAT = 40
    UPDATE_TIME_SPENT_SESSION_ID = 41
    LOBBY_VOICE_SERVER_PING = 42
    REQUEST_CHANNEL_INFO = 43


class GatewayCloseCode(enum.IntEnum):
    NORMAL = 1000
    GOING_AWAY = 1001
    UNKNOWN_ERROR = 4000
    UNKNOWN_OPCODE = 4001
    DECODE_ERROR = 4002
    NOT_AUTHENTICATED = 4003
    AUTHENTICATION_FAILED = 4004
    ALREADY_AUTHENTICATED = 4005
    INVALID_SEQ = 4007
    RATE_LIMITED = 4008
    SESSION_TIMED_OUT = 4009
    INVALID_SHARD = 4010
    SHARDING_REQUIRED = 4011
    INVALID_API_VERSION = 4012
    INVALID_INTENTS = 4013
    DISALLOWED_INTENTS = 4014
    TOO_MANY_SESSIONS = 4015
    CONNECTION_REQUEST_CANCELED = 4016


# Close codes that should NOT trigger a reconnect (fatal)
FATAL_CLOSE_CODES = {
    GatewayCloseCode.AUTHENTICATION_FAILED,
    GatewayCloseCode.INVALID_SHARD,
    GatewayCloseCode.SHARDING_REQUIRED,
    GatewayCloseCode.INVALID_API_VERSION,
    GatewayCloseCode.INVALID_INTENTS,
    GatewayCloseCode.DISALLOWED_INTENTS,
    GatewayCloseCode.TOO_MANY_SESSIONS,
    GatewayCloseCode.CONNECTION_REQUEST_CANCELED,
}

# Close codes that require a fresh IDENTIFY (cannot resume)
NON_RESUMABLE_CLOSE_CODES = {
    GatewayCloseCode.NORMAL,
    GatewayCloseCode.GOING_AWAY,
    GatewayCloseCode.NOT_AUTHENTICATED,
    GatewayCloseCode.ALREADY_AUTHENTICATED,
    GatewayCloseCode.INVALID_SEQ,
    GatewayCloseCode.SESSION_TIMED_OUT,
}


class GatewayIntents(enum.IntFlag):
    GUILDS = 1 << 0
    GUILD_MEMBERS = 1 << 1
    GUILD_MODERATION = 1 << 2
    GUILD_EMOJIS_AND_STICKERS = 1 << 3
    GUILD_INTEGRATIONS = 1 << 4
    GUILD_WEBHOOKS = 1 << 5
    GUILD_INVITES = 1 << 6
    GUILD_VOICE_STATES = 1 << 7
    GUILD_PRESENCES = 1 << 8
    GUILD_MESSAGES = 1 << 9
    GUILD_MESSAGE_REACTIONS = 1 << 10
    GUILD_MESSAGE_TYPING = 1 << 11
    DIRECT_MESSAGES = 1 << 12
    DIRECT_MESSAGE_REACTIONS = 1 << 13
    DIRECT_MESSAGE_TYPING = 1 << 14
    MESSAGE_CONTENT = 1 << 15
    GUILD_SCHEDULED_EVENTS = 1 << 16
    GUILD_EMBEDDED_ACTIVITIES = 1 << 17
    PRIVATE_CHANNELS = 1 << 18
    CALLS = 1 << 19
    AUTO_MODERATION_CONFIGURATION = 1 << 20
    AUTO_MODERATION_EXECUTION = 1 << 21
    USER_RELATIONSHIPS = 1 << 22
    USER_PRESENCE = 1 << 23
    GUILD_MESSAGE_POLLS = 1 << 24
    DIRECT_MESSAGE_POLLS = 1 << 25
    DIRECT_EMBEDDED_ACTIVITIES = 1 << 26
    LOBBIES = 1 << 27
    LOBBY_DELETE = 1 << 28


class GatewayCapabilities(enum.IntFlag):
    LAZY_USER_NOTES = 1 << 0
    NO_AFFINE_USER_IDS = 1 << 1
    VERSIONED_READ_STATES = 1 << 2
    VERSIONED_USER_GUILD_SETTINGS = 1 << 3
    DEDUPE_USER_OBJECTS = 1 << 4
    PRIORITIZED_READY_PAYLOAD = 1 << 5
    MULTIPLE_GUILD_EXPERIMENT_POPULATIONS = 1 << 6
    NON_CHANNEL_READ_STATES = 1 << 7
    AUTH_TOKEN_REFRESH = 1 << 8
    USER_SETTINGS_PROTO = 1 << 9
    CLIENT_STATE_V2 = 1 << 10
    PASSIVE_GUILD_UPDATE = 1 << 11
    AUTO_CALL_CONNECT = 1 << 12
    DEBOUNCE_MESSAGE_REACTIONS = 1 << 13
    PASSIVE_GUILD_UPDATE_V2 = 1 << 14
    CHANNEL_OBFUSCATION = 1 << 15
    AUTO_LOBBY_CONNECT = 1 << 16


# Capabilities included:
#   LAZY_USER_NOTES (0), VERSIONED_READ_STATES (2), VERSIONED_USER_GUILD_SETTINGS (3),
#   DEDUPE_USER_OBJECTS (4), PRIORITIZED_READY_PAYLOAD (5),
#   MULTIPLE_GUILD_EXPERIMENT_POPULATIONS (6), NON_CHANNEL_READ_STATES (7),
#   AUTH_TOKEN_REFRESH (8), USER_SETTINGS_PROTO (9), CLIENT_STATE_V2 (10),
#   AUTO_CALL_CONNECT (12), DEBOUNCE_MESSAGE_REACTIONS (13), PASSIVE_GUILD_UPDATE_V2 (14),
#   Undocumented bits set by the official client: 17, 19, 20
# Capabilities excluded:
#   NO_AFFINE_USER_IDS (1), PASSIVE_GUILD_UPDATE (11),
#   CHANNEL_OBFUSCATION (15), AUTO_LOBBY_CONNECT (16)
DEFAULT_USER_CAPABILITIES = 1734653


class DiscordGateway:
    """Discord Gateway WebSocket client.

    Implements the Gateway protocol as documented at
    https://docs.discord.food/gateway/using-gateway.

    Supported encodings: JSON only (ETF is not supported).
    Supported transport compression: zlib-stream only (zstd-stream is not supported).

    Features:
      - Connection lifecycle: Hello, Heartbeating, Identify, Resume, and Disconnect handling.
      - Heartbeat monitor with zombie-connection detection (unacknowledged heartbeats trigger reconnect).
      - Resuming: reconnects via resume_gateway_url and sequence replay.
      - Rate limiting: enforces 120 events / 60 seconds and 15 KiB payload size limit.
      - Optional zlib-stream transport decompression.
      - Event listeners and blocking wait_for() support.
      - Can run in foreground or as a background thread.
    """

    def __init__(
        self,
        token: str,
        properties: dict,
        gateway_url: str = DEFAULT_GATEWAY_URL,
        api_version: int = 9,
        encoding: str = "json",
        compress: str | None = None,
        intents: int | GatewayIntents | None = None,
        capabilities: int | GatewayCapabilities | None = None,
        client_state: dict | None = None,
        presence: dict | None = None,
        large_threshold: int | None = None,
        shard: tuple[int, int] | None = None,
        session: requests.Session | None = None,
        auto_reconnect: bool = True,
        max_reconnect_attempts: int = 10,
        reconnect_delay: float = 2.0,
        max_reconnect_delay: float = 60.0,
        user_agent: str | None = None,
        use_qos: bool = False,
        get_heartbeat_session_id: Callable[[], str | None] | None = None,
        on_rotate_heartbeat_session: Callable[[], str] | None = None,
        on_auth_token_refresh: Callable[[str], None] | None = None,
    ):
        if compress not in (None, "zlib-stream"):
            raise ValueError(f"Unsupported compress mode: {compress!r}. Only 'zlib-stream' or None is supported.")
        if encoding not in ("json",):
            raise ValueError(f"Unsupported encoding: {encoding!r}. Only 'json' is supported.")

        self.token = token
        self.base_gateway_url = gateway_url.rstrip("/")
        self.api_version = api_version
        self.encoding = encoding
        self.compress = compress  # None or "zlib-stream"
        self.properties = properties
        self.intents = intents
        self.capabilities = (
            capabilities if capabilities is not None
            else (None if intents is not None else DEFAULT_USER_CAPABILITIES)
        )
        self.client_state = client_state
        self.presence = presence
        if large_threshold is not None and not (25 <= large_threshold <= 250):
            raise ValueError(f"large_threshold must be between 25 and 250, got {large_threshold}")
        self.large_threshold = large_threshold if large_threshold is not None else (25 if intents is not None else 250)
        self.shard = shard
        self.auto_reconnect = auto_reconnect
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay
        self.user_agent = user_agent
        self.use_qos = use_qos
        self._get_heartbeat_session_id_cb = get_heartbeat_session_id
        self._on_rotate_heartbeat_session = on_rotate_heartbeat_session
        self._on_auth_token_refresh = on_auth_token_refresh
        self._last_time_spent = 0.0
        # Session
        self._managed_session = session is None
        self.session = session or requests.Session(impersonate="chrome")
        if self.user_agent:
            self.session.headers["User-Agent"] = self.user_agent
        # Connection state
        self.ws: WebSocket | None = None
        self._ws_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._send_history = collections.deque()  # timestamps for rate limiting

        # Sub-rate limits (Opcode 8: 1 req/guild/30s, Opcode 3: 5 req/20s)
        self._sub_rate_limit_lock = threading.Lock()
        self._guild_members_rate_limit: dict[str, float] = {}
        self._presence_history = collections.deque()
        self.last_rate_limit: dict[str, Any] | None = None

        # Gateway session state for Resuming
        self.session_id: str | None = None
        self.resume_gateway_url: str | None = None
        self.sequence: int | None = None
        self.user: dict | None = None

        # Heartbeat state
        self.heartbeat_interval: float | None = None  # seconds
        self.last_heartbeat_sent: float | None = None
        self.last_heartbeat_ack: float | None = None
        self.heartbeat_acked: bool = True
        self._heartbeat_thread: threading.Thread | None = None
        self._stop_heartbeat = threading.Event()
        self._heartbeat_reset = threading.Event()

        # Reconnection & lifecycle flags
        self._running = False
        self._closed_manually = False
        self._force_reconnect = threading.Event()
        self._server_requested_reconnect = False
        self._session_resumed = False
        self._reconnect_attempts = 0
        self._resume_attempts = 0
        self._invalid_session_delay: float | None = None
        self.is_connected = False
        self.ready = threading.Event()
        self.ready_supplemental = threading.Event()
        self.resumed = threading.Event()
        self._invalid_session_timer: threading.Timer | None = None
        self._runner_thread: threading.Thread | None = None
        self._listener_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="DiscordGateway-Listener"
        )

        # Client UUIDs for telemetry / time spent
        self._client_launch_id = str(uuid.uuid4())
        self._client_heartbeat_session_id = str(uuid.uuid4())
        self._client_heartbeat_session_init_ts = int(time.time() * 1000)
        # Event dispatching
        self._listeners: dict[str, list[Callable[[dict], None]]] = collections.defaultdict(list)
        self._wildcard_listeners: list[tuple[Callable, bool]] = []
        self._waiters: list[tuple[str, Callable[[dict], bool] | None, threading.Event, list]] = []
        self._waiters_lock = threading.Lock()
        self._listeners_lock = threading.Lock()

        # Zlib context
        self._inflator = None
        self._zlib_buffer = bytearray()


    def _get_listener_pool(self) -> concurrent.futures.ThreadPoolExecutor:
        """Return the thread pool executor for listeners."""
        return self._listener_pool

    def discover_gateway_url(self) -> str | None:
        """Query GET /gateway to retrieve the recommended gateway entrypoint."""
        try:
            res = self.session.get(f"https://discord.com/api/v{self.api_version}/gateway")
            if res.ok:
                data = res.json()
                url = data.get("url")
                if url:
                    self.base_gateway_url = url.rstrip("/")
                    return self.base_gateway_url
        except Exception as e:
            logger.debug("Failed to discover gateway URL from API: %s", e)
        return None

    def _build_ws_url(self, resume: bool = False) -> str:
        """Construct WebSocket connection URL with parameters."""
        base = (self.resume_gateway_url if (resume and self.resume_gateway_url) else self.base_gateway_url)
        # Normalize protocol to wss://
        if base.startswith("http://"):
            base = "ws://" + base[7:]
        elif base.startswith("https://"):
            base = "wss://" + base[8:]
        elif not base.startswith("ws://") and not base.startswith("wss://"):
            base = f"wss://{base}"

        parsed = urllib.parse.urlparse(base)
        params = urllib.parse.parse_qs(parsed.query)
        params["v"] = [str(self.api_version)]
        params["encoding"] = [self.encoding]
        if self.compress:
            params["compress"] = [self.compress]

        query_str = urllib.parse.urlencode({k: v[0] for k, v in params.items()})
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}?{query_str}"

    # -------------------------------------------------------------------------
    # Event listener registration
    # -------------------------------------------------------------------------
    def on(self, event_name: str, handler: Callable[[dict], None] | None = None):
        """Register an event handler decorator or function.

        Example:
            @gateway.on("MESSAGE_CREATE")
            def handle_msg(data):
                print(data)
        """
        def decorator(func: Callable[[dict], None]):
            ev = event_name.upper()
            with self._listeners_lock:
                if ev == "*":
                    self._wildcard_listeners.append((func, self._is_single_arg(func)))
                else:
                    self._listeners[ev].append(func)
            return func

        if handler is not None:
            return decorator(handler)
        return decorator

    def on_any(self, handler: Callable[[str, dict], None]):
        """Register a handler that receives all dispatch events (event_name, data)."""
        with self._listeners_lock:
            self._wildcard_listeners.append((handler, self._is_single_arg(handler)))
        return handler

    def remove_listener(self, event_name: str, handler: Callable):
        """Remove a previously registered listener."""
        ev = event_name.upper()
        with self._listeners_lock:
            if ev == "*":
                self._wildcard_listeners[:] = [
                    (h, s) for h, s in self._wildcard_listeners if h is not handler
                ]
            elif ev in self._listeners:
                if handler in self._listeners[ev]:
                    self._listeners[ev].remove(handler)

    def wait_for(
        self,
        event_name: str,
        check: Callable[[dict], bool] | None = None,
        timeout: float | None = None,
    ) -> dict | None:
        """Wait synchronously for a specific dispatch event matching check condition.

        Returns the event data dict if a matching event is received within the
        timeout, or None if the timeout elapses or the gateway shuts down.
        """
        event_ready = threading.Event()
        result_container: list[dict] = []
        waiter = (event_name.upper(), check, event_ready, result_container)

        with self._waiters_lock:
            self._waiters.append(waiter)

        signaled = event_ready.wait(timeout=timeout)
        with self._waiters_lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

        return result_container[0] if signaled and result_container else None

    # -------------------------------------------------------------------------
    # Sending payloads & rate limiting
    # -------------------------------------------------------------------------
    def _send_raw(self, encoded: bytes) -> bool:
        """Send pre-encoded bytes over the WebSocket under _ws_lock."""
        with self._ws_lock:
            if not self.ws or self.ws.closed:
                logger.debug("Attempted to send payload while WebSocket is closed")
                return False
            try:
                self.ws.send(encoded, flags=CurlWsFlag.TEXT)
                return True
            except Exception as e:
                logger.error("Failed to send WebSocket payload: %s", e)
                return False

    def send_payload(self, op: int, d: Any, *, _bypass_rate_limit: bool = False) -> bool:
        """Send a Gateway opcode payload over the active WebSocket."""
        payload = {"op": int(op), "d": d}
        serialized = json.dumps(payload, separators=(",", ":"))
        encoded = serialized.encode("utf-8")

        if len(encoded) > MAX_PAYLOAD_SIZE:
            raise ValueError(f"Gateway payload exceeds maximum size of 15 KiB ({len(encoded)} bytes)")

        if _bypass_rate_limit:
            return self._send_raw(encoded)

        sleep_time = 0.0
        while True:
            with self._send_lock:
                now = time.monotonic()
                while self._send_history and (now - self._send_history[0] > RATE_LIMIT_PERIOD):
                    self._send_history.popleft()

                if len(self._send_history) >= RATE_LIMIT_MAX_EVENTS:
                    sleep_time = RATE_LIMIT_PERIOD - (now - self._send_history[0]) + 0.05
                else:
                    sent = self._send_raw(encoded)
                    if sent:
                        self._send_history.append(time.monotonic())
                    return sent

            if sleep_time > 0:
                logger.warning("Gateway client hit 120 events/60s rate limit. Sleeping %.2fs", sleep_time)
                time.sleep(sleep_time)

    def send_heartbeat(self, seq: int | None = None):
        """Send Opcode 1 Heartbeat with last received sequence number.

        Bypasses the 120/60s rate limiter to prevent heartbeat starvation.
        """
        s = self.sequence if seq is None else seq
        logger.debug("Sending Heartbeat (op: 1, seq: %s)", s)
        self.last_heartbeat_sent = time.monotonic()
        return self.send_payload(GatewayOpcode.HEARTBEAT, s, _bypass_rate_limit=True)

    def send_qos_heartbeat(
        self,
        seq: int | None = None,
        active: bool = True,
        reasons: list[str] | None = None,
    ):
        """Send Opcode 40 QoS Heartbeat.

        Bypasses the 120/60s rate limiter to prevent heartbeat starvation.
        """
        s = self.sequence if seq is None else seq
        qos_data = {
            "ver": 31,
            "active": active,
            "reasons": reasons if reasons is not None else ["foregrounded"],
        }
        logger.debug("Sending QoS Heartbeat (op: 40, seq: %s)", s)
        self.last_heartbeat_sent = time.monotonic()
        return self.send_payload(GatewayOpcode.QOS_HEARTBEAT, {"seq": s, "qos": qos_data}, _bypass_rate_limit=True)

    def rotate_analytics_session(self):
        """Regenerate analytics session UUID every 30 minutes per spec."""
        if self._on_rotate_heartbeat_session is not None:
            # Rotate via the API so HTTP and Gateway stay in sync
            new_sid = self._on_rotate_heartbeat_session()
            if new_sid:
                self._client_heartbeat_session_id = new_sid
            else:
                self._client_heartbeat_session_id = str(uuid.uuid4())
        else:
            self._client_heartbeat_session_id = str(uuid.uuid4())
        self._client_heartbeat_session_init_ts = int(time.time() * 1000)

    def _get_heartbeat_session_id(self) -> str:
        """Return the current heartbeat session ID from the API callback or local state."""
        if self._get_heartbeat_session_id_cb is not None:
            sid = self._get_heartbeat_session_id_cb()
            if sid:
                return sid
        return self._client_heartbeat_session_id

    def update_time_spent(
        self,
        session_id: str | None = None,
        client_launch_id: str | None = None,
    ):
        """Send Opcode 41 Update Time Spent Session ID accompanied by Opcode 40 QoS Heartbeat."""
        sid = session_id or self._get_heartbeat_session_id()
        lid = (
            client_launch_id
            or (self.properties or {}).get("client_launch_id")
            or self._client_launch_id
        )
        init_ts = getattr(self, "_client_heartbeat_session_init_ts", int(time.time() * 1000))
        d = {
            "initialization_timestamp": init_ts,
            "session_id": sid,
            "client_launch_id": lid,
        }
        res = self.send_payload(GatewayOpcode.UPDATE_TIME_SPENT_SESSION_ID, d)
        if self.use_qos:
            self.send_qos_heartbeat()
        return res

    def identify(self):
        """Send Opcode 2 Identify to initiate the handshake."""
        logger.info("Sending Opcode 2 Identify")
        props = dict(self.properties)
        # Always inject the current heartbeat session ID
        props["client_heartbeat_session_id"] = self._get_heartbeat_session_id()
        d: dict[str, Any] = {
            "token": self.token,
            "properties": props,
            "compress": False,
        }

        if self.large_threshold is not None:
            d["large_threshold"] = self.large_threshold

        if self.shard is not None:
            d["shard"] = list(self.shard)

        is_oauth2 = self.token.startswith("Bearer ")

        if self.presence is not None:
            d["presence"] = self.presence
        elif self.intents is None and not is_oauth2:
            # User account default presence (not supported in OAuth2 contexts)
            d["presence"] = {
                "status": "unknown",
                "since": 0,
                "activities": [],
                "afk": False,
            }

        if self.intents is not None:
            d["intents"] = int(self.intents)

        if self.capabilities is not None:
            d["capabilities"] = int(self.capabilities)

        if self.client_state is not None:
            d["client_state"] = self.client_state
        elif self.intents is None and not is_oauth2:
            d["client_state"] = {
                "guild_versions": {},
                "api_code_version": 0,
            }
        return self.send_payload(GatewayOpcode.IDENTIFY, d)

    def resume(self):
        """Send Opcode 6 Resume to replay missed events."""
        logger.info("Sending Opcode 6 Resume (session_id=%s, seq=%s)", self.session_id, self.sequence)
        d = {
            "token": self.token,
            "session_id": self.session_id,
            "seq": self.sequence,
        }
        return self.send_payload(GatewayOpcode.RESUME, d)

    def update_presence(
        self,
        status: str = "online",
        activities: list[dict] | None = None,
        afk: bool = False,
        since: int = 0,
    ):
        """Send Opcode 3 Presence Update (rate-limited to 5 requests per 20 seconds)."""
        while True:
            with self._sub_rate_limit_lock:
                now = time.monotonic()
                while self._presence_history and (now - self._presence_history[0] > PRESENCE_RATE_LIMIT_PERIOD):
                    self._presence_history.popleft()
                if len(self._presence_history) >= PRESENCE_RATE_LIMIT_MAX:
                    wait_time = PRESENCE_RATE_LIMIT_PERIOD - (now - self._presence_history[0]) + 0.05
                else:
                    wait_time = 0.0
                    self._presence_history.append(now)
                    break

            if wait_time > 0:
                logger.warning("Presence update rate limit (5 per 20s): sleeping %.2fs", wait_time)
                time.sleep(wait_time)
        d = {
            "status": status,
            "activities": activities or [],
            "afk": afk,
            "since": since,
        }
        return self.send_payload(GatewayOpcode.PRESENCE_UPDATE, d)
    def update_voice_state(
        self,
        guild_id: str | None,
        channel_id: str | None,
        self_mute: bool = False,
        self_deaf: bool = False,
        self_video: bool = False,
        preferred_region: str | None = None,
        preferred_regions: list[str] | None = None,
        flags: int | None = None,
    ):
        """Send Opcode 4 Voice State Update."""
        d: dict[str, Any] = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "self_mute": self_mute,
            "self_deaf": self_deaf,
            "self_video": self_video,
        }
        if preferred_region is not None:
            d["preferred_region"] = preferred_region
        if preferred_regions is not None:
            d["preferred_regions"] = preferred_regions
        if flags is not None:
            d["flags"] = flags
        return self.send_payload(GatewayOpcode.VOICE_STATE_UPDATE, d)

    def request_guild_members(
        self,
        guild_id: str | list[str],
        query: str = "",
        limit: int = 0,
        presences: bool = False,
        user_ids: list[str] | None = None,
        nonce: str | None = None,
    ):
        """Send Opcode 8 Request Guild Members (rate-limited to 1 request per guild per 30 seconds)."""
        if isinstance(guild_id, (str, int)):
            guild_keys = [str(guild_id)]
        else:
            guild_keys = [str(gid) for gid in guild_id]
        while True:
            with self._sub_rate_limit_lock:
                now = time.monotonic()
                max_wait = 0.0
                for gid in guild_keys:
                    last = self._guild_members_rate_limit.get(gid, 0.0)
                    if now - last < GUILD_MEMBERS_RATE_LIMIT_PERIOD:
                        max_wait = max(max_wait, GUILD_MEMBERS_RATE_LIMIT_PERIOD - (now - last))
                if max_wait <= 0:
                    for gid in guild_keys:
                        self._guild_members_rate_limit[gid] = now
                    break

            if max_wait > 0:
                logger.warning("Opcode 8 sub-rate limit: sleeping %.2fs for guild(s) %s", max_wait, guild_keys)
                time.sleep(max_wait)
        d: dict[str, Any] = {
            "guild_id": guild_id,
            "presences": presences,
        }
        if user_ids is not None:
            d["user_ids"] = user_ids
        else:
            d["query"] = query
            d["limit"] = limit
        if nonce is not None:
            d["nonce"] = nonce
        return self.send_payload(GatewayOpcode.REQUEST_GUILD_MEMBERS, d)

    def ping_voice_server(self):
        """Send Opcode 5 Voice Server Ping."""
        return self.send_payload(GatewayOpcode.VOICE_SERVER_PING, None)

    def request_call_connect(self, channel_id: str):
        """Send Opcode 13 Call Connect."""
        return self.send_payload(GatewayOpcode.CALL_CONNECT, {"channel_id": channel_id})

    def request_deleted_entity_ids(
        self,
        guild_id: str,
        channel_ids_hash: str = "",
        role_ids_hash: str = "",
        emoji_ids_hash: str = "",
        sticker_ids_hash: str = "",
    ):
        """Send Opcode 30 Request Deleted Entity IDs."""
        d = {
            "guild_id": guild_id,
            "channel_ids_hash": channel_ids_hash,
            "role_ids_hash": role_ids_hash,
            "emoji_ids_hash": emoji_ids_hash,
            "sticker_ids_hash": sticker_ids_hash,
        }
        return self.send_payload(GatewayOpcode.REQUEST_DELETED_ENTITY_IDS, d)

    def request_soundboard_sounds(self, guild_ids: list[str]):
        """Send Opcode 31 Request Soundboard Sounds."""
        return self.send_payload(GatewayOpcode.REQUEST_SOUNDBOARD_SOUNDS, {"guild_ids": guild_ids})

    def request_last_messages(self, guild_id: str, channel_ids: list[str]):
        """Send Opcode 34 Request Last Messages."""
        return self.send_payload(GatewayOpcode.REQUEST_LAST_MESSAGES, {"guild_id": guild_id, "channel_ids": channel_ids})

    def search_recent_members(
        self,
        guild_id: str,
        query: str = "",
        limit: int = 100,
        continuation_token: str | None = None,
        nonce: str | None = None,
    ):
        """Send Opcode 35 Search Recent Members."""
        d: dict[str, Any] = {
            "guild_id": guild_id,
            "query": query,
            "limit": limit,
            "continuation_token": continuation_token,
        }
        if nonce is not None:
            d["nonce"] = nonce
        return self.send_payload(GatewayOpcode.SEARCH_RECENT_MEMBERS, d)

    def guild_subscriptions_bulk(self, subscriptions: dict):
        """Send Opcode 37 Guild Subscriptions Bulk."""
        return self.send_payload(GatewayOpcode.GUILD_SUBSCRIPTIONS_BULK, {"subscriptions": subscriptions})

    def guild_channels_resync(self, guild_id: str, obfuscated_channel_ids: list[str] | None = None):
        """Send Opcode 38 Guild Channels Resync."""
        return self.send_payload(GatewayOpcode.GUILD_CHANNELS_RESYNC, {
            "guild_id": guild_id,
            "obfuscated_channel_ids": obfuscated_channel_ids or [],
        })

    def request_channel_member_count(self, guild_id: str, channel_id: str):
        """Send Opcode 39 Request Channel Member Count."""
        return self.send_payload(GatewayOpcode.REQUEST_CHANNEL_MEMBER_COUNT, {
            "guild_id": guild_id,
            "channel_id": channel_id,
        })

    def request_channel_info(self, guild_id: str, fields: list[str] | None = None):
        """Send Opcode 43 Request Channel Info."""
        return self.send_payload(GatewayOpcode.REQUEST_CHANNEL_INFO, {
            "guild_id": guild_id,
            "fields": fields or ["status", "voice_start_time"],
        })

    def update_lobby_voice_states(self, lobbies: list[dict]):
        """Send Opcode 17 Update Lobby Voice States."""
        return self.send_payload(GatewayOpcode.LOBBY_VOICE_STATES, lobbies)

    def create_stream(
        self,
        type: str,
        channel_id: str,
        guild_id: str | None = None,
        preferred_region: str | None = None,
    ):
        """Send Opcode 18 Create Stream."""
        d: dict[str, Any] = {
            "type": type,
            "guild_id": guild_id,
            "channel_id": channel_id,
        }
        if preferred_region is not None:
            d["preferred_region"] = preferred_region
        return self.send_payload(GatewayOpcode.STREAM_CREATE, d)

    def delete_stream(self, stream_key: str):
        """Send Opcode 19 Delete Stream."""
        return self.send_payload(GatewayOpcode.STREAM_DELETE, {"stream_key": stream_key})

    def watch_stream(self, stream_key: str):
        """Send Opcode 20 Watch Stream."""
        return self.send_payload(GatewayOpcode.STREAM_WATCH, {"stream_key": stream_key})

    def ping_stream_server(self, stream_key: str):
        """Send Opcode 21 Ping Stream Server."""
        return self.send_payload(GatewayOpcode.STREAM_PING, {"stream_key": stream_key})

    def set_stream_paused(self, stream_key: str, paused: bool = True):
        """Send Opcode 22 Set Stream Paused."""
        return self.send_payload(GatewayOpcode.STREAM_SET_PAUSED, {"stream_key": stream_key, "paused": paused})

    def request_forum_unreads(self, guild_id: str, channel_id: str, threads: list[dict] | None = None):
        """Send Opcode 28 Request Forum Unreads."""
        d = {
            "guild_id": guild_id,
            "channel_id": channel_id,
            "threads": threads or [],
        }
        return self.send_payload(GatewayOpcode.REQUEST_FORUM_UNREADS, d)

    def remote_command(self, target_session_id: str, payload: dict):
        """Send Opcode 29 Remote Command."""
        d = {
            "target_session_id": target_session_id,
            "payload": payload,
        }
        return self.send_payload(GatewayOpcode.REMOTE_COMMAND, d)

    def ping_lobby_voice_server(self, lobby_id: str):
        """Send Opcode 42 Ping Lobby Voice Server."""
        return self.send_payload(GatewayOpcode.LOBBY_VOICE_SERVER_PING, {"lobby_id": lobby_id})

    # -------------------------------------------------------------------------
    # Heartbeat Worker Loop
    # -------------------------------------------------------------------------
    def _start_heartbeat(self, interval_ms: float):
        """Start the background heartbeat thread."""
        self._stop_heartbeat.set()
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=1.0)

        self._stop_heartbeat.clear()
        self.heartbeat_interval = interval_ms / 1000.0
        self.heartbeat_acked = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            daemon=True,
            name="DiscordGateway-Heartbeat",
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        """Continually send heartbeats every heartbeat_interval seconds."""
        interval = self.heartbeat_interval or 41.25

        # First heartbeat may have jitter per Discord documentation:
        # "The first heartbeat may be offset by a value between 0 and heartbeat_interval"
        jitter = interval * random.uniform(0.0, 1.0)
        logger.debug("Initial heartbeat scheduled in %.2fs (jitter)", jitter)
        if self._stop_heartbeat.wait(timeout=jitter):
            return

        while not self._stop_heartbeat.is_set():
            if not self.heartbeat_acked:
                logger.warning(
                    "Heartbeat ACK not received between intervals; connection zombied. Forcing reconnect."
                )
                self._force_reconnect.set()
                return

            self.heartbeat_acked = False
            if self.use_qos:
                self.send_qos_heartbeat()
            else:
                self.send_heartbeat()

            # Every 30 minutes, send Opcode 41 Update Time Spent if using QoS
            now = time.monotonic()
            if self.use_qos and (now - self._last_time_spent >= TIME_SPENT_INTERVAL):
                try:
                    self.rotate_analytics_session()
                    self.update_time_spent()
                    self._last_time_spent = now
                except Exception as e:
                    logger.debug("Failed to send scheduled Opcode 41 update_time_spent: %s", e)

            # Wait for interval or until server requests an immediate heartbeat
            # Limit resets to prevent server-spammed Op 1 from suppressing ACK check
            resets = 0
            max_resets = 3
            while not self._stop_heartbeat.is_set():
                if self._heartbeat_reset.wait(timeout=interval):
                    # Server sent Opcode 1, which already dispatched a heartbeat.
                    # Clear flag and reset interval timer to wait full interval before checking ACK.
                    self._heartbeat_reset.clear()
                    resets += 1
                    if resets >= max_resets:
                        logger.debug("Heartbeat reset limit reached (%d); proceeding to ACK check", resets)
                        break
                    continue
                else:
                    # Interval elapsed normally without server request
                    break

    # -------------------------------------------------------------------------
    # Decompression & Frame Processing
    # -------------------------------------------------------------------------
    def _decompress_frame(self, data: bytes) -> str | None:
        """Decompress zlib-stream frame according to RFC1950 & Discord specs."""
        self._zlib_buffer.extend(data)
        if len(self._zlib_buffer) < 4 or self._zlib_buffer[-4:] != ZLIB_SUFFIX:
            return None

        try:
            decompressed = self._inflator.decompress(self._zlib_buffer)
            self._zlib_buffer.clear()
            return decompressed.decode("utf-8")
        except (zlib.error, UnicodeDecodeError) as e:
            logger.error("Decompression/decode error on zlib frame: %s; forcing reconnect and resume", e)
            self._zlib_buffer.clear()
            self._force_reconnect.set()
            return None

    # -------------------------------------------------------------------------
    # Packet Handler
    # -------------------------------------------------------------------------
    def _handle_packet(self, raw_json: str) -> bool:
        """Process an incoming Gateway packet. Returns False if loop should terminate."""
        try:
            payload = json.loads(raw_json)
        except Exception as e:
            logger.error("Failed to parse gateway JSON packet: %s (%r)", e, raw_json[:200])
            return True

        op = payload.get("op")
        d = payload.get("d")
        s = payload.get("s")
        t = payload.get("t")

        if s is not None:
            self.sequence = s

        if op == GatewayOpcode.DISPATCH:
            self._handle_dispatch(t, d)
        elif op == GatewayOpcode.HELLO:
            interval = d.get("heartbeat_interval", 41250)
            logger.info("Received Opcode 10 Hello: heartbeat_interval=%d ms", interval)
            self._start_heartbeat(interval)
            # Perform initial handshake or resume
            if self.session_id and self.sequence is not None:
                self.resume()
            else:
                self.identify()
        elif op == GatewayOpcode.HEARTBEAT_ACK:
            logger.debug("Received Opcode 11 Heartbeat ACK")
            self.last_heartbeat_ack = time.monotonic()
            self.heartbeat_acked = True
        elif op == GatewayOpcode.HEARTBEAT:
            logger.info("Received Opcode 1 Heartbeat request from server; heartbeating immediately")
            if self.use_qos:
                self.send_qos_heartbeat()
            else:
                self.send_heartbeat()
            self._heartbeat_reset.set()
        elif op == GatewayOpcode.RECONNECT:
            logger.info("Received Opcode 7 Reconnect; server requested reconnect and resume")
            self._server_requested_reconnect = True
            return False
        elif op == GatewayOpcode.INVALID_SESSION:
            can_resume = bool(d)
            if not can_resume:
                logger.warning(
                    "Received Opcode 9 Invalid Session (resumable: False). Session invalidated; disconnecting to start fresh session on base gateway URL."
                )
                self.session_id = None
                self.sequence = None
                self.resume_gateway_url = None
                self._resume_attempts = 0
                self._invalid_session_delay = random.uniform(1.0, 5.0)
                return False

            delay = random.uniform(1.0, 5.0)
            logger.warning(
                "Received Opcode 9 Invalid Session (resumable: True). Re-resuming in %.2fs on existing connection.",
                delay,
            )

            def _reauth():
                if not self._running or self._closed_manually or self._force_reconnect.is_set():
                    return
                # _send_raw checks ws liveness atomically under _ws_lock;
                # no separate pre-check needed.
                try:
                    if self.session_id and self.sequence is not None:
                        logger.info("Re-sending Opcode 6 Resume after Invalid Session delay")
                        self.resume()
                    else:
                        logger.info("Re-sending Opcode 2 Identify after Invalid Session delay")
                        self.identify()
                except Exception as e:
                    logger.debug("Failed to re-authenticate after Invalid Session delay: %s", e)

            if self._invalid_session_timer is not None:
                try:
                    self._invalid_session_timer.cancel()
                except Exception:
                    pass
            self._invalid_session_timer = threading.Timer(delay, _reauth)
            self._invalid_session_timer.daemon = True
            self._invalid_session_timer.start()
            return True
        else:
            logger.debug("Received unhandled Gateway Opcode %s", op)

        return True

    def _handle_dispatch(self, event_name: str | None, data: Any):
        """Dispatch received Opcode 0 event to listeners and state handlers."""
        if not event_name:
            return

        ev = event_name.upper()

        if ev == "READY":
            self.session_id = data.get("session_id")
            self.resume_gateway_url = data.get("resume_gateway_url")
            self.user = data.get("user")
            logger.info(
                "Connected to Gateway as %s (session_id=%s, resume_url=%s)",
                self.user.get("username", "Unknown") if isinstance(self.user, dict) else "Unknown",
                self.session_id,
                self.resume_gateway_url,
            )
            new_token = data.get("auth_token")
            if new_token:
                logger.info("Received updated auth_token from Gateway; refreshing client token")
                self.token = new_token
                if self.session and hasattr(self.session, "headers"):
                    self.session.headers["Authorization"] = new_token
                if self._on_auth_token_refresh is not None:
                    self._on_auth_token_refresh(new_token)
            self._reconnect_attempts = 0
            self._resume_attempts = 0
            if self.use_qos:
                try:
                    self.update_time_spent()
                    self._last_time_spent = time.monotonic()
                except Exception as e:
                    logger.debug("Failed to send initial Opcode 41 update_time_spent: %s", e)
            self.ready.set()
        elif ev == "READY_SUPPLEMENTAL":
            logger.info("Received READY_SUPPLEMENTAL event")
            self.ready_supplemental.set()
        elif ev == "RESUMED":
            logger.info("Gateway session resumed successfully!")
            self._reconnect_attempts = 0
            self._resume_attempts = 0
            self._session_resumed = True
            if self.use_qos:
                try:
                    self.update_time_spent()
                    self._last_time_spent = time.monotonic()
                except Exception as e:
                    logger.debug("Failed to send post-resume Opcode 41 update_time_spent: %s", e)
            self.resumed.set()
            self.ready.set()
        elif ev == "RATE_LIMITED":
            opcode = data.get("opcode") if isinstance(data, dict) else None
            retry_after = data.get("retry_after", 0.0) if isinstance(data, dict) else 0.0
            meta = data.get("meta", {}) if isinstance(data, dict) else {}
            logger.warning(
                "Received Gateway RATE_LIMITED event: opcode=%s, retry_after=%.2fs, meta=%s",
                opcode,
                retry_after,
                meta,
            )
            now = time.monotonic()
            self.last_rate_limit = {
                "opcode": opcode,
                "retry_after": retry_after,
                "meta": meta,
                "timestamp": now,
            }
            if retry_after > 0:
                with self._sub_rate_limit_lock:
                    if opcode == GatewayOpcode.REQUEST_GUILD_MEMBERS:
                        gid = meta.get("guild_id")
                        if gid:
                            self._guild_members_rate_limit[str(gid)] = now + retry_after - 30.0
                    elif opcode == GatewayOpcode.PRESENCE_UPDATE:
                        self._presence_history.clear()
                        for _ in range(5):
                            self._presence_history.append(now + retry_after - 20.0)

        # Notify waiting threads — evaluate check predicates outside the lock
        matched_waiters: list[tuple[str, Callable[[dict], bool] | None, threading.Event, list]] = []
        with self._waiters_lock:
            for waiter in list(self._waiters):
                target_ev, check, event_ready, container = waiter
                if target_ev == ev or target_ev == "*":
                    matched_waiters.append(waiter)

        for target_ev, check, event_ready, container in matched_waiters:
            try:
                if check is None or check(data):
                    container.append(data)
                    event_ready.set()
            except Exception as e:
                logger.exception("Error in waiter check for event %s: %s", ev, e)
                # Signal the waiter so wait_for() returns None instead of deadlocking
                event_ready.set()

        # Execute registered callbacks in thread pool to avoid blocking receive loop
        with self._listeners_lock:
            handlers = list(self._listeners.get(ev, []))
            wildcards = list(self._wildcard_listeners)

        for handler in handlers:
            self._get_listener_pool().submit(self._run_handler, handler, ev, data)

        for handler, single_arg in wildcards:
            self._get_listener_pool().submit(self._run_wildcard_handler, handler, single_arg, ev, data)

    @staticmethod
    def _run_handler(handler: Callable, ev: str, data: Any):
        """Run a single event handler safely."""
        try:
            handler(data)
        except Exception as e:
            logger.exception("Error in listener for event %s: %s", ev, e)

    @staticmethod
    def _is_single_arg(handler: Callable) -> bool:
        """Check if a handler accepts only a single positional argument."""
        try:
            sig = inspect.signature(handler)
            params = [
                p for p in sig.parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            has_var_args = any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values())
            return len(params) == 1 and not has_var_args
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _run_wildcard_handler(handler: Callable, single_arg: bool, ev: str, data: Any):
        """Run a single wildcard event handler safely, using cached arity."""
        try:
            if single_arg:
                handler(data)
            else:
                handler(ev, data)
        except Exception as e:
            logger.exception("Error in wildcard listener for event %s: %s", ev, e)
    # -------------------------------------------------------------------------
    # WebSocket Frame Receiver & Single Connection Cycle
    # -------------------------------------------------------------------------
    def _recv_frame(
        self, ws: WebSocket, sock_fd: int | None = None, sock_obj: socket.socket | None = None
    ) -> tuple[bytes, int] | None:
        """Receive a full WebSocket frame under _ws_lock, releasing lock during socket wait."""
        chunks: list[bytes] = []
        flags: int = 0

        if sock_fd is None:
            sock_fd = ws.curl.getinfo(CurlInfo.ACTIVESOCKET)
            if sock_fd == CURL_SOCKET_BAD:
                raise WebSocketError("Invalid active socket", CurlECode.NO_CONNECTION_AVAILABLE)

        poll_target = sock_obj if sock_obj is not None else sock_fd

        while self._running and not self._closed_manually and not self._force_reconnect.is_set():
            need_poll = False
            with self._ws_lock:
                if not self.ws or self.ws.closed:
                    return None
                try:
                    chunk, frame = ws.recv_fragment()
                    flags = frame.flags
                    if flags & CurlWsFlag.CLOSE:
                        if frame.bytesleft == 0:
                            return b"", flags
                        continue
                    if flags & CurlWsFlag.PING:
                        continue
                    chunks.append(chunk)
                    if frame.bytesleft == 0 and (flags & CurlWsFlag.CONT) == 0:
                        return b"".join(chunks), flags
                    # Fragment received with bytes/fragments remaining; continue immediately
                    continue
                except CurlError as e:
                    if e.code != CurlECode.AGAIN:
                        raise
                    need_poll = True
                except (WebSocketClosed, WebSocketError):
                    raise

            if need_poll:
                # Wait on socket outside _ws_lock so heartbeat/sender threads can send
                try:
                    select.select([poll_target], [], [], 0.5)
                except Exception:
                    # If polling fails (e.g. WinSock 10038), sleep briefly instead of aborting
                    time.sleep(0.05)

        return None

    def _run_connection(self, is_resuming: bool) -> tuple[int | None, str | None]:
        """Perform a single WebSocket connection cycle until it closes.

        Returns:
            (close_code, close_reason)
        """
        url = self._build_ws_url(resume=is_resuming)
        logger.info("Connecting to Gateway: %s", url)

        self._force_reconnect.clear()
        self._session_resumed = False

        # Reset zlib if transport compression is active
        if self.compress == "zlib-stream":
            self._inflator = zlib.decompressobj()
            self._zlib_buffer.clear()

        try:
            # autoclose is a WebSocket constructor kwarg, not a connect() kwarg;
            # Session.ws_connect forwards kwargs to connect() and would reject it.
            ws = self.session.ws_connect(url)
            ws.autoclose = False
        except Exception as e:
            logger.error("Failed to establish WebSocket connection to %s: %s", url, e)
            # Attempt dynamic discovery if connecting to base URL failed
            if not is_resuming:
                self.discover_gateway_url()
            return None, str(e)

        with self._ws_lock:
            self.ws = ws
            self.is_connected = True

        sock_fd = ws.curl.getinfo(CurlInfo.ACTIVESOCKET)
        sock_obj: socket.socket | None = None
        if sock_fd != CURL_SOCKET_BAD:
            for family in (socket.AF_INET, socket.AF_INET6):
                try:
                    sock_obj = socket.fromfd(sock_fd, family, socket.SOCK_STREAM)
                    break
                except Exception:
                    continue
        close_code: int | None = None
        close_reason: str | None = None

        try:
            while self._running and not self._closed_manually and not self._force_reconnect.is_set():
                try:
                    frame_data = self._recv_frame(ws, sock_fd, sock_obj)
                except WebSocketClosed:
                    close_code = ws.close_code
                    close_reason = ws.close_reason
                    break
                except WebSocketError as e:
                    close_code = ws.close_code or e.code
                    close_reason = ws.close_reason or str(e)
                    break
                except Exception as e:
                    logger.debug("Receive error: %s", e)
                    break

                if frame_data is None:
                    if ws.close_code is not None:
                        close_code = ws.close_code
                        close_reason = ws.close_reason
                    break

                data, flags = frame_data

                if flags & CurlWsFlag.CLOSE:
                    close_code = ws.close_code
                    close_reason = ws.close_reason
                    logger.info("Gateway sent CLOSE frame: %s (%s)", close_code, close_reason)
                    break

                if self.compress == "zlib-stream":
                    text = self._decompress_frame(data)
                    if text is None:
                        if self._force_reconnect.is_set():
                            break
                        continue
                else:
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError as e:
                        logger.error("Failed to decode text payload: %s", e)
                        continue

                keep_running = self._handle_packet(text)
                if not keep_running:
                    break

        finally:
            if self._invalid_session_timer is not None:
                try:
                    self._invalid_session_timer.cancel()
                except Exception:
                    pass
                self._invalid_session_timer = None
            if sock_obj is not None:
                try:
                    sock_obj.detach()
                except Exception:
                    pass
            self._stop_heartbeat.set()
            self.is_connected = False
            self.ready.clear()
            self.resumed.clear()
            self.ready_supplemental.clear()

            with self._ws_lock:
                if self.ws:
                    try:
                        if self._closed_manually:
                            self.ws.close(GatewayCloseCode.NORMAL)
                        else:
                            self.ws.close(GatewayCloseCode.UNKNOWN_ERROR)
                    except Exception:
                        pass
                    self.ws = None

        return close_code, close_reason

    # -------------------------------------------------------------------------
    # Main Reconnecting Loop
    # -------------------------------------------------------------------------
    def run(self):
        """Connect to the Gateway and manage reconnection logic (blocking)."""
        self._running = True
        self._closed_manually = False
        self._reconnect_attempts = 0
        self._server_requested_reconnect = False

        # Re-create managed session if it was closed by a previous run()
        if self._managed_session and self.session is None:
            self.session = requests.Session(impersonate="chrome")
            if self.user_agent:
                self.session.headers["User-Agent"] = self.user_agent

        # Re-create listener pool if it was shut down by a previous stop()
        if self._listener_pool is None or self._listener_pool._shutdown:
            self._listener_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=8, thread_name_prefix="DiscordGateway-Listener"
            )

        logger.info("Starting Discord Gateway client")

        try:
            while self._running and not self._closed_manually:
                can_resume = bool(self.session_id and self.sequence is not None)
                close_code, close_reason = self._run_connection(is_resuming=can_resume)

                if self._closed_manually:
                    logger.info("Gateway connection stopped manually")
                    break

                if close_code in FATAL_CLOSE_CODES:
                    logger.critical(
                        "Gateway closed with fatal code %s (%s). Reconnection aborted.",
                        close_code,
                        close_reason,
                    )
                    break

                if close_code == GatewayCloseCode.RATE_LIMITED:
                    logger.warning(
                        "Gateway closed with rate limit code 4008 (%s). Will reconnect with backoff.",
                        close_reason,
                    )

                if close_code in NON_RESUMABLE_CLOSE_CODES:
                    logger.info(
                        "Gateway closed with non-resumable code %s (%s). Starting fresh session.",
                        close_code,
                        close_reason,
                    )
                    self.session_id = None
                    self.sequence = None
                    self.resume_gateway_url = None
                    self._resume_attempts = 0
                elif can_resume and not self._session_resumed and not self._server_requested_reconnect:
                    self._resume_attempts += 1
                    if self._resume_attempts >= MAX_RESUME_ATTEMPTS:
                        logger.info(
                            "Resume connection failed (%d attempts); falling back to base gateway URL with fresh identify.",
                            self._resume_attempts,
                        )
                        self.session_id = None
                        self.sequence = None
                        self.resume_gateway_url = None
                        self._resume_attempts = 0
                if not self.auto_reconnect:
                    logger.info("Auto-reconnect disabled. Terminating Gateway loop.")
                    break

                # Opcode 7 Reconnect: reconnect immediately without backoff
                if self._server_requested_reconnect:
                    self._server_requested_reconnect = False
                    logger.info("Server-requested reconnect; reconnecting immediately.")
                    continue
                # Opcode 9 Invalid Session backoff (1-5s per spec)
                if self._invalid_session_delay is not None:
                    delay = self._invalid_session_delay
                    self._invalid_session_delay = None
                    logger.info("Opcode 9 Invalid Session: waiting %.2fs before reconnecting", delay)
                    time.sleep(delay)
                    continue

                self._reconnect_attempts += 1
                if self.max_reconnect_attempts > 0 and self._reconnect_attempts > self.max_reconnect_attempts:
                    logger.error("Maximum reconnection attempts (%d) reached. Aborting.", self.max_reconnect_attempts)
                    break

                backoff = min(self.max_reconnect_delay, self.reconnect_delay * (2 ** (self._reconnect_attempts - 1)))
                jitter = random.uniform(0.0, 1.0)
                wait_time = backoff + jitter
                logger.info("Reconnecting in %.2fs (attempt %d)...", wait_time, self._reconnect_attempts)
                time.sleep(wait_time)

        finally:
            self._running = False
            self.is_connected = False
            self._stop_heartbeat.set()
            if self._invalid_session_timer is not None:
                try:
                    self._invalid_session_timer.cancel()
                except Exception:
                    pass
                self._invalid_session_timer = None
            self._drain_waiters()
            if self._managed_session:
                self.session.close()
                self.session = None

    def start(self) -> threading.Thread:
        """Start the Gateway connection in a background thread and return the thread."""
        if self._runner_thread and self._runner_thread.is_alive():
            return self._runner_thread

        # Re-create managed session if it was closed by a previous run()
        if self._managed_session and self.session is None:
            self.session = requests.Session(impersonate="chrome")
            if self.user_agent:
                self.session.headers["User-Agent"] = self.user_agent

        self._runner_thread = threading.Thread(
            target=self.run,
            daemon=True,
            name="DiscordGateway-Runner",
        )
        self._runner_thread.start()
        return self._runner_thread

    def stop(self, timeout: float = 5.0):
        """Disconnect and stop the Gateway client."""
        logger.info("Stopping Discord Gateway client")
        self._closed_manually = True
        self._running = False
        self._stop_heartbeat.set()
        if self._invalid_session_timer is not None:
            try:
                self._invalid_session_timer.cancel()
            except Exception:
                pass
            self._invalid_session_timer = None
        # Drain pending waiters so blocked threads don't hang
        self._drain_waiters()

        # Shut down background event listener thread pool
        if self._listener_pool:
            try:
                self._listener_pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._listener_pool = None
        with self._ws_lock:
            if self.ws and not self.ws.closed:
                try:
                    self.ws.close(GatewayCloseCode.NORMAL)
                except Exception:
                    pass

        if self._runner_thread and self._runner_thread.is_alive():
            self._runner_thread.join(timeout=timeout)

    def _drain_waiters(self):
        """Signal all pending waiters so they unblock."""
        with self._waiters_lock:
            for _ev, _check, event_ready, _container in self._waiters:
                event_ready.set()
            self._waiters.clear()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
