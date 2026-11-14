#!/usr/bin/env python3

import base64
import json
import re
import threading
import time
import ua_parser
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from curl_cffi import requests
from curl_cffi.requests.exceptions import ConnectionError, Timeout, CurlError as RequestError
from json import JSONDecodeError
import utils

BASE_URL = "https://discord.com/api/v9"
HEARTBEAT_ROTATION_SECONDS = 30 * 60
FALLBACK_ANDROID_VERSION = (353, 11)


FALLBACK_ANDROID_USER_AGENT = (
    f"Discord-Android/{utils.pack_android_version(FALLBACK_ANDROID_VERSION)};RNA"
)
OKHTTP3_JA3 = "771,4865-4866-4867-49195-49196-52393-49199-49200-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-51-45-43-21,29-23-24,0"
OKHTTP3_AKAMAI = "4:16777216|16711681|0|m,p,a,s"
OKHTTP3_EXTRA_FP = {
    "tls_signature_algorithms": [
        "ecdsa_secp256r1_sha256",
        "rsa_pss_rsae_sha256",
        "rsa_pkcs1_sha256",
        "ecdsa_secp384r1_sha384",
        "rsa_pss_rsae_sha384",
        "rsa_pkcs1_sha384",
        "rsa_pss_rsae_sha512",
        "rsa_pkcs1_sha512",
        "rsa_pkcs1_sha1",
    ]
}


def _create_session(
    impersonation: str,
    user_agent: str | None = None,
) -> requests.Session:
    profile = impersonation.lower()
    if profile == "chrome":
        return requests.Session(impersonate="chrome")
    if profile == "okhttp3":
        return requests.Session(
            ja3=OKHTTP3_JA3,
            akamai=OKHTTP3_AKAMAI,
            extra_fp=OKHTTP3_EXTRA_FP,
            headers={"User-Agent": user_agent or FALLBACK_ANDROID_USER_AGENT},
        )
    raise ValueError(
        f"Unsupported impersonation profile {impersonation!r}; "
        "expected 'okhttp3' or 'chrome'"
    )


def _fetch_user_agent(session: requests.Session) -> str | None:
    class _UAHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(self.headers.get("User-Agent", "").encode())

        def log_message(self, *args):
            pass

    server = None
    try:
        server = HTTPServer(("127.0.0.1", 0), _UAHandler)
        server.timeout = 5
        threading.Thread(target=server.handle_request, daemon=True).start()

        r = session.get(f"http://127.0.0.1:{server.server_address[1]}", timeout=5)
        return r.text.strip() or None
    except Exception:
        return None
    finally:
        if server:
            server.server_close()


def _fetch_android_version(session: requests.Session) -> tuple[str, str] | None:
    try:
        response = session.get(
            "https://play.google.com/store/apps/details?id=com.discord&hl=en",
            timeout=10,
        )
        response.raise_for_status()
    except Exception:
        return None

    match = re.search(r'\[\[\["(\d+)\.(\d+) - Stable"\]\]', response.text)
    if not match:
        return None

    return match.groups()

def _fetch_build_number(session: requests.Session) -> int | None:
    try:
        r = session.get("https://discord.com/login", timeout=10)
        r.raise_for_status()
    except Exception:
        return None

    m = re.search(r'"BUILD_NUMBER"\s*:\s*"?(\d+)"?', r.text)
    if not m:
        return None
    return int(m.group(1))


def build_web_xsuperproperties(
    user_agent: str | None = None,
    session: requests.Session | None = None,
    client_build_number: int | None = None,
    client_launch_id: str | None = None,
) -> str:
    if user_agent is None or client_build_number is None:
        managed_session = session is None
        active_session = session or _create_session("chrome")
        try:
            if user_agent is None:
                user_agent = (
                    active_session.headers.get("User-Agent")
                    or _fetch_user_agent(active_session)
                )
                if not user_agent:
                    raise RuntimeError("Unable to determine the Chrome session user agent")
            if client_build_number is None:
                client_build_number = _fetch_build_number(active_session)
        finally:
            if managed_session:
                active_session.close()

    parsed = ua_parser.parse(user_agent)
    payload = {
        "os": parsed.os.family if (parsed and parsed.os and parsed.os.family) else "Windows",
        "browser": parsed.user_agent.family if (parsed and parsed.user_agent and parsed.user_agent.family) else "Chrome",
        "device": "",
        "system_locale": "en-US",
        "has_client_mods": False,
        "browser_user_agent": user_agent,
        "browser_version": utils.get_version(parsed.user_agent) if parsed else "",
        "os_version": utils.get_version(parsed.os) if parsed else "",
        "referrer": "https://discord.com/",
        "referring_domain": "discord.com",
        "referrer_current": "https://discord.com/",
        "referring_domain_current": "discord.com",
        "release_channel": "stable",
        "client_build_number": client_build_number or utils.FALLBACK_CLIENT_BUILD_NUMBER,
        "client_launch_id": client_launch_id or str(uuid.uuid4()),
        "launch_signature": utils.generate_launch_signature(),
        "client_app_state": "focused",
        "client_heartbeat_session_id": str(uuid.uuid4()),
    }
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def build_mobile_xsuperproperties(
    user_agent: str | None = None,
    session: requests.Session | None = None,
    android_version: tuple[str, str] | None = None,
    client_build_number: int | None = None,
    device: str | None = None,
    device_vendor_id: str | None = None,
    client_launch_id: str | None = None,
) -> str:
    if android_version is None or client_build_number is None:
        managed_session = session is None
        active_session = session or _create_session("chrome")
        try:
            if android_version is None:
                android_version = (
                    _fetch_android_version(active_session)
                    or FALLBACK_ANDROID_VERSION
                )
            if client_build_number is None:
                client_build_number = _fetch_build_number(active_session)
        finally:
            if managed_session:
                active_session.close()

    if not user_agent:
        user_agent = f"Discord-Android/{utils.pack_android_version(android_version)};RNA"

    payload = {
        "os": "Android",
        "browser": "Discord Android",
        "device": device or utils.get_random_android_device(),
        "system_locale": "en-US",
        "has_client_mods": False,
        "client_version": utils.pack_android_client_version(android_version),
        "release_channel": "googleRelease",
        "device_vendor_id": device_vendor_id or str(uuid.uuid4()),
        "design_id": 2,
        "browser_user_agent": "",
        "browser_version": "",
        "os_version": "34",
        "client_build_number": utils.pack_android_build_number(
            android_version, client_build_number
        ),
        "client_launch_id": client_launch_id or str(uuid.uuid4()),
        "launch_signature": str(time.time_ns()),
        "client_heartbeat_session_id": str(uuid.uuid4()),
        "client_app_state": "active",
    }
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def _rotate_heartbeat_session_id(x_super_properties: str) -> str:
    payload = json.loads(base64.b64decode(x_super_properties).decode("utf-8"))
    payload["client_heartbeat_session_id"] = str(uuid.uuid4())
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")



class DiscordAPI:
    def __init__(
        self,
        token: str,
        sleep_delay: float = 0.5,
        max_retries: int = 15,
        max_conn_retries: int = 5,
        user_agent: str | None = None,
    ):
        self.desktop_session = _create_session("chrome")

        android_version = (
            _fetch_android_version(self.desktop_session) or FALLBACK_ANDROID_VERSION
        )
        android_user_agent = f"Discord-Android/{utils.pack_android_version(android_version)};RNA"
        self.mobile_session = _create_session(
            "okhttp3",
            user_agent=android_user_agent,
        )

        chrome_user_agent = (
            user_agent
            or self.desktop_session.headers.get("User-Agent")
            or _fetch_user_agent(self.desktop_session)
        )
        if chrome_user_agent is None:
            self.close()
            raise RuntimeError("Unable to determine the Chrome session user agent")

        client_build_number = _fetch_build_number(self.desktop_session)
        self.desktop_launch_id = str(uuid.uuid4())
        self.mobile_launch_id = str(uuid.uuid4())
        chrome_x_super = build_web_xsuperproperties(
            user_agent=chrome_user_agent,
            client_build_number=client_build_number,
            client_launch_id=self.desktop_launch_id,
        )
        mobile_device = utils.get_random_android_device()
        mobile_vendor_id = str(uuid.uuid4())
        mobile_x_super = build_mobile_xsuperproperties(
            user_agent=android_user_agent,
            android_version=android_version,
            client_build_number=client_build_number,
            device=mobile_device,
            device_vendor_id=mobile_vendor_id,
            client_launch_id=self.mobile_launch_id,
        )

        parsed = ua_parser.parse(chrome_user_agent)
        chrome_major = (
            parsed.user_agent.major
            if (parsed and parsed.user_agent and parsed.user_agent.major)
            else "134"
        )
        os_platform = (
            parsed.os.family if (parsed and parsed.os and parsed.os.family) else "Windows"
        )

        self.desktop_headers = {
            "Authorization": token,
            "User-Agent": chrome_user_agent,
            "X-Super-Properties": chrome_x_super,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": f'"Google Chrome";v="{chrome_major}", "Chromium";v="{chrome_major}", "Not:A-Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": f'"{os_platform}"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "X-Discord-Locale": "en-US",
            "X-Discord-Timezone": "UTC",
        }
        self.mobile_headers = {
            "Authorization": token,
            "User-Agent": android_user_agent,
            "X-Super-Properties": mobile_x_super,
            "Accept-Language": "en-US,en;q=0.9",
            "X-Discord-Locale": "en-US",
            "X-Discord-Timezone": "UTC",
        }

        # Aliases for backward compatibility
        self.session = self.desktop_session
        self.dm_search_session = self.mobile_session
        self.headers = self.desktop_headers
        self.dm_search_headers = self.mobile_headers

        self._desktop_heartbeat_rotated_at = time.monotonic()
        self._mobile_heartbeat_rotated_at = time.monotonic()
        # Cooperative shutdown flag: set by the SIGINT handler in main.py.
        # Request loops and sleeps poll this instead of relying on
        # KeyboardInterrupt, which is swallowed by curl_cffi's cffi callbacks.
        self.shutdown = threading.Event()
        self.sleep_delay = sleep_delay
        self.max_retries = max_retries
        self.max_conn_retries = max_conn_retries

    def close(self):
        self.desktop_session.close()
        self.mobile_session.close()

    def _rotate_heartbeat_if_due(self):
        now = time.monotonic()
        if now - self._desktop_heartbeat_rotated_at >= HEARTBEAT_ROTATION_SECONDS:
            self.desktop_headers["X-Super-Properties"] = _rotate_heartbeat_session_id(
                self.desktop_headers["X-Super-Properties"]
            )
            self.headers = self.desktop_headers
            self._desktop_heartbeat_rotated_at = now

        if now - self._mobile_heartbeat_rotated_at >= HEARTBEAT_ROTATION_SECONDS:
            self.mobile_headers["X-Super-Properties"] = _rotate_heartbeat_session_id(
                self.mobile_headers["X-Super-Properties"]
            )
            self.dm_search_headers = self.mobile_headers
            self._mobile_heartbeat_rotated_at = now

    def get_desktop_heartbeat_session_id(self) -> str | None:
        """Return the current client_heartbeat_session_id from desktop X-Super-Properties."""
        xsp = self.desktop_headers.get("X-Super-Properties")
        if not xsp:
            return None
        try:
            payload = json.loads(base64.b64decode(xsp).decode("utf-8"))
            return payload.get("client_heartbeat_session_id")
        except Exception:
            return None

    def rotate_desktop_heartbeat_session_id(self) -> str:
        """Rotate the desktop client_heartbeat_session_id and return the new value."""
        self.desktop_headers["X-Super-Properties"] = _rotate_heartbeat_session_id(
            self.desktop_headers["X-Super-Properties"]
        )
        self.headers = self.desktop_headers
        self._desktop_heartbeat_rotated_at = time.monotonic()
        return self.get_desktop_heartbeat_session_id()

    def _request(
        self,
        method: str,
        url: str,
        body=None,
        referer=None,
        use_mobile: bool = False,
        use_dm_search_client: bool = False,
    ):
        conn_retries = 0
        is_mobile = use_mobile or use_dm_search_client

        while True:
            if self.shutdown.is_set():
                return None
            self._rotate_heartbeat_if_due()
            if is_mobile:
                session = self.mobile_session
                headers = self.mobile_headers.copy()
            else:
                session = self.desktop_session
                headers = self.desktop_headers.copy()
                if referer:
                    headers["Referer"] = referer
                if method.upper() in ("POST", "PATCH", "DELETE", "PUT"):
                    headers["Origin"] = "https://discord.com"

            try:
                kwargs = {"headers": headers, "timeout": 15}
                if body is not None:
                    kwargs["json"] = body
                r = session.request(method.upper(), url, **kwargs)
                conn_retries = 0

                # 429 Rate Limit Handling
                if r.status_code == 429:
                    retry_header = r.headers.get("Retry-After") or r.headers.get("X-RateLimit-Reset-After")
                    retry = None
                    if retry_header:
                        try:
                            retry = float(retry_header)
                        except ValueError:
                            pass
                    if retry is None:
                        try:
                            retry = float(r.json().get("retry_after", 5))
                        except (JSONDecodeError, ValueError, TypeError):
                            retry = 5.0

                    scope = r.headers.get("X-RateLimit-Scope", "unknown")
                    is_global = r.headers.get("X-RateLimit-Global") == "true"
                    print(f"Rate limited (scope: {scope}, global: {is_global}). Sleeping for {retry + 0.5:.2f}s...")
                    if self.shutdown.wait(retry + 0.5):
                        return None
                    continue

                if r.status_code >= 400:
                    if r.status_code == 401:
                        print(f"\n[UNAUTHORIZED] {r.text}")
                    else:
                        print(f"[HTTP ERROR] {r.status_code}: {r.text}")
                        print(url)

                return r

            except ConnectionError as e:
                conn_retries += 1
                if conn_retries > self.max_conn_retries:
                    print(f"[ERROR] Max connection retries reached for {url}")
                    return None
                wait_time = 5 * conn_retries
                print(f"[ERROR] Connection error: {e}. Retrying {conn_retries}/{self.max_conn_retries} in {wait_time}s...")
                if self.shutdown.wait(wait_time):
                    return None

            except Timeout as e:
                conn_retries += 1
                if conn_retries > self.max_conn_retries:
                    print(f"[ERROR] Max timeout retries reached for {url}")
                    return None
                wait_time = 5 * conn_retries
                print(f"[ERROR] Timeout: {e}. Retrying {conn_retries}/{self.max_conn_retries} in {wait_time}s...")
                if self.shutdown.wait(wait_time):
                    return None

            except RequestError as e:
                print(f"[ERROR] {e}")
                return None

    def get(self, endpoint: str, use_mobile: bool = False, **kwargs):
        return self._request("GET", f"{BASE_URL}{endpoint}", use_mobile=use_mobile, **kwargs)

    def post(self, endpoint: str, body=None, use_mobile: bool = False, **kwargs):
        return self._request("POST", f"{BASE_URL}{endpoint}", body=body, use_mobile=use_mobile, **kwargs)

    def patch(self, endpoint: str, body=None, use_mobile: bool = False, **kwargs):
        return self._request("PATCH", f"{BASE_URL}{endpoint}", body=body, use_mobile=use_mobile, **kwargs)

    def delete(self, channel_id: str, message_id: str, guild_id: str = None, use_mobile: bool = False):
        endpoint = f"/channels/{channel_id}/messages/{message_id}"
        if use_mobile or guild_id is None:
            # DM message deletion via Android OkHttp3 client
            return self._request("DELETE", f"{BASE_URL}{endpoint}", use_mobile=True)
        # Guild channel deletion via Desktop Chrome client
        referer = f"https://discord.com/channels/{guild_id}/{channel_id}"
        return self._request("DELETE", f"{BASE_URL}{endpoint}", referer=referer, use_mobile=False)

    def unarchive_thread(self, thread: dict, guild_id: str = None):
        referer = f"https://discord.com/channels/{guild_id}/{thread['id']}" if guild_id else None
        return self.patch(f"/channels/{thread['id']}", body={"archived": False}, referer=referer, use_mobile=False)

    def search_guild(self, guild_id: str, user_id: str, channel_query: str = "", offset: int = 0, include_nsfw: bool = True, min_id: str = None, max_id: str = None):
        parts = [f"author_id={user_id}", "sort_by=timestamp", "sort_order=desc"]
        if min_id:
            parts.append(f"min_id={min_id}")
        if max_id:
            parts.append(f"max_id={max_id}")
        query = "&".join(parts)
        referer = f"https://discord.com/channels/{guild_id}"
        return self.get(
            f"/guilds/{guild_id}/messages/search?{query}{channel_query}&include_nsfw={'true' if include_nsfw else 'false'}&offset={offset}",
            referer=referer,
            use_mobile=False,
        )

    def search_dm(self, user_id: str, timestamp: str = None, max_id: str = None, min_id: str = None):
        cursor = {"timestamp": timestamp, "type": "timestamp"} if timestamp else None
        body = {
            "tabs": {
                "messages": {
                    "sort_by": "timestamp",
                    "sort_order": "desc",
                    "author_id": [user_id],
                    "cursor": cursor,
                    "limit": 25
                }
            },
            "track_exact_total_hits": False
        }
        if max_id is not None:
            body["tabs"]["messages"]["max_id"] = max_id
        if min_id is not None:
            body["tabs"]["messages"]["min_id"] = min_id
        return self.post(
            "/users/@me/messages/search/tabs",
            body=body,
            use_mobile=True,
        )

    def get_channels(self, guild_id: str):
        referer = f"https://discord.com/channels/{guild_id}"
        return self.get(f"/guilds/{guild_id}/channels", referer=referer, use_mobile=False)

    def get_current_user(self):
        return self.get("/users/@me", use_mobile=False)

    def get_user_guilds(self):
        return self.get("/users/@me/guilds", use_mobile=False)

    def get_gateway(self):
        return self.get("/gateway", use_mobile=False)

    def get_gateway_bot(self):
        return self.get("/gateway/bot", use_mobile=False)

    def create_gateway(
        self,
        intents: int | None = None,
        capabilities: int | None = None,
        compress: str | None = None,
        auto_reconnect: bool = True,
        **kwargs,
    ):
        from gateway import DiscordGateway

        res = self.get_gateway()
        gw_url = "wss://gateway.discord.gg"
        if res and res.ok:
            try:
                data = res.json()
                if isinstance(data, dict) and data.get("url"):
                    gw_url = data["url"]
            except Exception:
                pass

        xsp = self.desktop_headers.get("X-Super-Properties")
        if not xsp:
            raise RuntimeError("Cannot create Gateway: missing X-Super-Properties header")
        try:
            props = json.loads(base64.b64decode(xsp).decode("utf-8"))
            # Remove session ID from static props; gateway will fetch it dynamically
            props.pop("client_heartbeat_session_id", None)
        except Exception as e:
            raise RuntimeError(f"Cannot create Gateway: failed to decode X-Super-Properties: {e}") from e

        def _update_auth_token(new_token: str):
            self.desktop_headers["Authorization"] = new_token
            self.mobile_headers["Authorization"] = new_token

        return DiscordGateway(
            token=self.desktop_headers.get("Authorization", ""),
            gateway_url=gw_url,
            properties=props,
            intents=intents,
            capabilities=capabilities,
            compress=compress,
            auto_reconnect=auto_reconnect,
            user_agent=self.desktop_headers.get("User-Agent"),
            session=self.desktop_session,
            get_heartbeat_session_id=self.get_desktop_heartbeat_session_id,
            on_rotate_heartbeat_session=self.rotate_desktop_heartbeat_session_id,
            on_auth_token_refresh=_update_auth_token,
            **kwargs,
        )
