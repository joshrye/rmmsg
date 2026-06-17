# rmmsg

rmmsg deletes your own Discord messages in bulk — across every server and DM — so you can clean your account's history without clicking through years of chats one by one.

You choose what gets deleted: everything, only messages from a certain time period, or everything except a few servers and conversations you want to keep.

> ⚠️ **Read this first**
> - **Deletion is permanent.** Once messages are gone, they're gone. There is no undo.
> - **Automating a user account breaks Discord's Terms of Service.** There is a real risk of the account being flagged or suspended. Don't use your main account unless you accept that risk.

---

## Quick start

### 1. Install

You need [Python](https://python.org) 3.10 or newer. Then install the dependencies:

```
pip install curl_cffi ua-parser pyyaml
```

### 2. Get your Discord token

The tool needs your account's auth token to act as you.

1. Open Discord in your browser (or the desktop app) and press `Ctrl+Shift+I` to open Developer Tools.
2. Go to the **Network** tab.
3. Click around in Discord (open a channel, send a message) until requests to `discord.com/api` appear.
4. Click one, open the **Headers** section, and copy the value of the `Authorization` header.

> Treat your token like a password — anyone who has it has full access to your account. Never share it or post it anywhere.

### 3. Configure

Copy `config.example.yaml` to `config.yaml` and paste your token into the `token:` field:

```yaml
token: "your-token-here"
```

That's all you need for a full wipe of everything. To limit what gets deleted, see [Choosing what gets deleted](#choosing-what-gets-deleted) below, or copy the examples from `config.example.yaml`.

### 4. Run it

```
python main.py
```

By default it reads `config.yaml` from the same folder. To use a different file:

```
python main.py -c path/to/my-config.yaml
```

While it runs you'll see each message as it's found and deleted.

**To stop:** press `Ctrl+C` once and the tool finishes its current request, then exits cleanly. Press `Ctrl+C` twice to stop immediately.

---

## Choosing what gets deleted

By default the tool deletes **everything you've ever said, everywhere**. Two settings narrow that down.

### Time range

Under `range`, set `mode` to `date` to only delete messages sent in a period:

```yaml
range:
  mode: date
  date:
    after: "2024-01-01T00:00:00Z"    # delete messages sent AFTER this...
    before: "2026-01-01T00:00:00Z"   # ...and BEFORE this
```

Leave `mode: none` (or remove the section) to ignore time and cover all history.

Alternatively, `mode: msgid` lets you give the boundaries as Discord message IDs instead of dates, if you happen to have them.

### Keep or target list

Under `rules`, `mode` decides how the list is used:

- **`preserve`** *(default)* — delete everything **except** what you list. Good for "wipe everything but my current server and my partner's DMs."
- **`target`** — delete **only** what you list. Good for "clean out these two abandoned servers, touch nothing else."

Examples:

```yaml
rules:
  mode: preserve
  preserve:
    guilds:
      - id: "123456789012345678"        # this whole server is spared
      - id: "222222222222222222"
        channels:                        # only these channels in this server are spared
          - "987654321098765432"
    dms:
      - "123456789012345678"            # this DM conversation is spared
```

```yaml
rules:
  mode: target
  target:
    guilds:
      - id: "222222222222222222"        # wipe only this server...
        channels:
          - "333333333333333333"        # ...and only this channel in it
    dms:
      - "123456789012345678"            # and only this DM conversation
```

A few details:

- In `preserve` mode, a server listed **without** a `channels` list spares the entire server. With a `channels` list, only those channels are spared and the rest is wiped.
- In `target` mode, if you don't include a `dms:` section at all, DMs are skipped entirely.
- DM entries are the **DM channel ID**, not the person's user ID (they're usually the same number, but copy it from the DM itself).

**Finding IDs:** in Discord, go to *Settings → Advanced* and enable **Developer Mode**. Then right-click any server, channel, or DM and choose **Copy ID**.

---

## Settings reference

Everything in `config.yaml`:

| Setting | Default | What it does |
|---|---|---|
| `token` | — | Your Discord auth token. **Required.** |
| `sleepDelay` | `0.5` | Seconds to wait between deletions. The tool adds slight random variation automatically. Raise this (e.g. `1.0`) if you're worried about rate limits. |
| `maxRetries` | `15` | How many times to retry when Discord's search reports messages but won't return them. |
| `maxConnRetries` | `5` | How many times to retry when the connection drops. |
| `range` | off | Time or message-ID window — see above. |
| `rules` | delete all | Keep/target lists — see above. |

---

## Good to know

- **The tool only deletes your own messages.** Other people's messages are never touched.
- **It runs at a polite pace.** Deletions are spaced out and Discord's rate limits are respected automatically — if Discord says "slow down," the tool waits and resumes.
- **If something fails to delete** (e.g. a message is in a server you can no longer view), it's skipped and the run continues.
- **If your login becomes invalid** (error `401`), the tool stops immediately rather than hammering a dead session.
- **Recently "deleted for everyone" messages** can still show up in search results for a while; the tool skips any it can't delete and moves on.
- **Archived threads** containing your messages are temporarily unarchived so the messages inside can be deleted.

## License

ISC — see [LICENSE](LICENSE).
