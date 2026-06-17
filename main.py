#!/usr/bin/env python3

import argparse
import signal
import sys
from discord_api import DiscordAPI
import utils

MAX_RETRIES = 15


def _sleep_interruptible(seconds: float, shutdown) -> bool:
    """Sleep, aborting early when shutdown is signalled. Returns True if aborted."""
    return shutdown.wait(seconds)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Wipe Discord messages")
    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="Path to the config file (default: config.yaml)",
    )
    return parser.parse_args(argv)


def process_guild(api: DiscordAPI, guild: dict, user_id: str, rules_mode: str = "preserve", rule_channels: set = None, min_id: str = None, max_id: str = None) -> bool:
    guild_id, guild_name = str(guild["id"]), guild.get("name", "Unknown")
    print(f"\n=== Searching {guild_name} (Desktop Client) ===")

    channels = api.get_channels(guild_id=guild_id)
    if not channels or not channels.ok:
        print("  Failed to get channels")
        return True

    channels = channels.json()
    rule_channels = rule_channels or set()
    channel_query = ""

    if rules_mode == "target":
        if rule_channels:
            target_ids = [c["id"] for c in channels if str(c["id"]) in rule_channels]
            if not target_ids:
                print("  No targeted channels found in this guild, skipping")
                return True
            channel_query = "&" + "&".join(f"channel_id={i}" for i in target_ids)
    else:  # preserve
        if rule_channels:
            target_ids = [c["id"] for c in channels if str(c["id"]) not in rule_channels]
            if not target_ids:
                print("  All channels in this guild are preserved, skipping")
                return True
            channel_query = "&" + "&".join(f"channel_id={i}" for i in target_ids)
    skipped = 0
    retries = 0

    if api.shutdown.is_set():
        return False

    search = api.search_guild(guild_id=guild_id, user_id=user_id, channel_query=channel_query, offset=0, min_id=min_id, max_id=max_id)
    search_data = search.json() if search and search.ok else {}
    if not search or not search.ok or "total_results" not in search_data:
        print("  No messages found or search failed")
        return True

    while search and search_data.get("total_results", 0) - skipped > 0:
        if api.shutdown.is_set():
            return False
        for thread in search_data.get("threads", []):
            if thread.get("thread_metadata", {}).get("archived"):
                api.unarchive_thread(thread=thread, guild_id=guild_id)

        messages = search_data.get("messages", [])
        if not messages:
            retries += 1
            if retries >= MAX_RETRIES:
                print(f"  No messages returned after {MAX_RETRIES} retries, moving on")
                break
            print(f"  No messages in response but {search_data.get('total_results', 0)} reported, retrying ({retries}/{MAX_RETRIES})...")
            if _sleep_interruptible(5, api.shutdown):
                return False
            search = api.search_guild(guild_id=guild_id, user_id=user_id, channel_query=channel_query, offset=skipped, min_id=min_id, max_id=max_id)
            search_data = search.json() if search and search.ok else {}
            continue

        retries = 0

        for content in messages:
            if api.shutdown.is_set():
                return False
            msg = content[0]
            # Channel / Guild message deletion using Desktop Chrome client
            res = api.delete(msg["channel_id"], msg["id"], guild_id=guild_id, use_mobile=False)
            if api.shutdown.is_set():
                return False
            if res and res.ok:
                print(f"  Deleted: {msg['content'][:50]}")
            else:
                if res and res.status_code == 401:
                    print(f"  Critical error 401 encountered. Aborting to protect account.")
                    return False
                skipped += 1
                print("  Skipping message due to error")
            if utils.sleep_jittered(api.sleep_delay, shutdown=api.shutdown):
                return False
        if messages:
            lastid = messages[-1][0]["id"]
            search = api.search_guild(guild_id=guild_id, user_id=user_id, channel_query=channel_query, offset=0, min_id=min_id, max_id=lastid)
            if not search or not search.ok:
                print("  Search failed, stopping")
                return True
            search_data = search.json()
        else:
            return True
    return True


def process_dm(api: DiscordAPI, user_id: str, rules_mode: str = "preserve", dm_rules: set = None) -> bool:
    dm_rules = dm_rules or set()
    print("\n=== Searching Direct Messages (Android Client) ===")
    dms = api.search_dm(user_id=user_id)
    if not dms or not dms.ok:
        if dms and dms.status_code == 401:
            print(f"Critical error 401 encountered in DM search. Aborting.")
            return False
        return True

    dms_data = dms.json()
    retries = 0

    while dms and dms_data.get("tabs", {}).get("messages", {}).get("total_results", 0) > 0:
        if api.shutdown.is_set():
            return False
        messages = dms_data["tabs"]["messages"]["messages"]

        if not messages:
            retries += 1
            if retries >= MAX_RETRIES:
                print(f"  No messages returned after {MAX_RETRIES} retries, moving on")
                break
            print(f"  No messages returned but {dms_data['tabs']['messages']['total_results']} reported, retrying ({retries}/{MAX_RETRIES})...")
            if _sleep_interruptible(5, api.shutdown):
                return False
            dms = api.search_dm(user_id=user_id)
            if not dms or not dms.ok:
                return True
            dms_data = dms.json()
            continue

        retries = 0
        timestamp = None

        for message in messages:
            if api.shutdown.is_set():
                return False
            msg = message[0]
            channel_id = str(msg.get("channel_id", ""))
            if rules_mode == "preserve" and channel_id in dm_rules:
                print(f"  Skipping preserved DM channel: {channel_id}")
                timestamp = msg["id"]
                continue
            if rules_mode == "target" and dm_rules and channel_id not in dm_rules:
                print(f"  Skipping untargeted DM channel: {channel_id}")
                timestamp = msg["id"]
                continue
            # DM message deletion using Android OkHttp3 client
            res = api.delete(msg["channel_id"], msg["id"], use_mobile=True)
            if api.shutdown.is_set():
                return False
            if res and res.ok:
                print(f"  Deleted DM message: {msg['id']}")
            else:
                if res and res.status_code == 401:
                    print(f"  Critical error 401 encountered in DM delete. Aborting.")
                    return False
                timestamp = msg["id"]
                print("  Skipping message due to error")
            if utils.sleep_jittered(api.sleep_delay, shutdown=api.shutdown):
                return False
        if messages:
            dms = api.search_dm(user_id=user_id, timestamp=timestamp, max_id=messages[-1][0]["id"])
            if not dms or not dms.ok:
                return True
            dms_data = dms.json()
        else:
            return True
    return True



def _run(api: DiscordAPI, cfg: dict) -> int:
    # Start a Gateway connection to maintain a live session during the wipe.
    # This keeps the account appearing online (like a real client) and catches
    # any auth token refreshes Discord may issue mid-session.
    gw = api.create_gateway(compress="zlib-stream")

    gw.start()
    if not gw.ready.wait(timeout=30):
        print("Warning: Gateway did not become ready in time, proceeding without it")

    try:
        if gw.user:
            user = gw.user
            print(f"Logged in as: {user.get('username', 'Unknown')}")
        else:
            resp = api.get_current_user()
            if not resp or not resp.ok:
                print("Failed to get user data")
                return 1
            user = resp.json()
            print(f"Logged in as: {user.get('username', 'Unknown')}")

        range_cfg = cfg.get("range", {})
        range_mode = range_cfg.get("mode", "none")
        min_id = None
        max_id = None
        if range_mode == "date":
            date_range = range_cfg.get("date", {})
            start_date = date_range.get("after", None)
            end_date = date_range.get("before", None)
            if start_date and end_date:
                start_unix = utils.iso_to_unix(start_date)
                end_unix = utils.iso_to_unix(end_date)
                min_id = str(utils.to_snowflake(start_unix))
                max_id = str(utils.to_snowflake(end_unix))
                print(f"Searching for messages between {start_date} and {end_date}")
        elif range_mode == "msgid":
            msgid_range = range_cfg.get("msgid", {})
            min_id = msgid_range.get("after", None)
            max_id = msgid_range.get("before", None)
            if min_id and max_id:
                print(f"Searching for messages between msgid {min_id} and {max_id}")

        rules = cfg.get("rules", {})
        raw_mode = str(rules.get("mode", "preserve")).lower()
        # Support "preserve" (spares listed items) and "target" (only deletes listed items)
        # Backward compatibility: "blacklist" -> preserve, "whitelist" -> target
        if raw_mode in ("target", "whitelist"):
            rules_mode = "target"
            rule_section = rules.get("target") or rules.get("whitelist") or {}
        else:
            rules_mode = "preserve"
            rule_section = rules.get("preserve") or rules.get("blacklist") or {}

        guild_filters = {}
        for guild_entry in rule_section.get("guilds", []):
            gid = str(guild_entry.get("id", ""))
            ch_set = set(str(c) for c in guild_entry.get("channels", []))
            guild_filters[gid] = ch_set

        dm_rules = set(str(d) for d in rule_section.get("dms", []))

        guilds = api.get_user_guilds()
        if not guilds or not guilds.ok:
            print("Failed to get guilds")
            return 1

        guilds = guilds.json()

        if rules_mode == "target":
            guilds = [g for g in guilds if str(g["id"]) in guild_filters]
        else:
            # Preserve mode: skip guilds where entire guild is preserved (empty channel list)
            guilds = [
                g for g in guilds
                if not (str(g["id"]) in guild_filters and len(guild_filters[str(g["id"])]) == 0)
            ]

        if rules_mode == "target" and "dms" not in rule_section:
            print("\n=== Skipping Direct Messages (not targeted) ===")
        else:
            if not process_dm(api, user["id"], rules_mode=rules_mode, dm_rules=dm_rules):
                if api.shutdown.is_set():
                    print("\nInterrupted — stopping.")
                    return 130
                print("DM processing halted due to critical error.")
                return 1

        for g in guilds:
            if api.shutdown.is_set():
                print("\nInterrupted — stopping.")
                return 130
            gid = str(g["id"])
            rule_channels = guild_filters.get(gid, set())
            if not process_guild(api, g, user["id"], rules_mode=rules_mode, rule_channels=rule_channels, min_id=min_id, max_id=max_id):
                if api.shutdown.is_set():
                    print("\nInterrupted — stopping.")
                    return 130
                print(f"Guild processing halted on {g.get('name', 'guild')} due to critical error.")
                return 1
        print(f"Guilds processed: {len(guilds)}")
        return 0
    finally:
        gw.stop()


def main(argv=None):
    args = parse_args(argv)
    try:
        import yaml
        with open(args.config) as f:
            cfg = yaml.safe_load(f) or {}

        api = DiscordAPI(
            token=cfg.get("token", ""),
            sleep_delay=cfg.get("sleepDelay", 0.5),
            max_retries=cfg.get("maxRetries", 15),
            max_conn_retries=cfg.get("maxConnRetries", 5),
        )

        def _handle_sigint(signum, frame):
            if api.shutdown.is_set():
                # Second Ctrl+C: force exit immediately.
                print("\nForce exit.", flush=True)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                raise KeyboardInterrupt
            print("\nCtrl+C received — finishing current request, then stopping. Press Ctrl+C again to force quit.", flush=True)
            api.shutdown.set()

        signal.signal(signal.SIGINT, _handle_sigint)

        try:
            return _run(api, cfg)
        finally:
            api.close()

    except KeyError as e:
        print(f"Missing required configuration key: {e}")
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
