"""Discord approval bot (discord.py, bot token DISCORD_BOT_TOKEN).

Runs inside the main bot process (Bot.run) and never takes it down:

  #ready-to-post  each review round: one message with the 3 previews (A/B/C), a card per
                  variant (title, YT/IG/TikTok captions, style), and buttons:
                  Approve A/B/C · Redo · Reject · Feedback · Edit caption.
                  A thread per clip holds re-renders, errors and the live links.
  #tiktok-queue   the approved file + captions for manual posting (TikTok, and any
                  platform not automated for that mode).
  #inspiration    links or uploads -> style analysis -> preset (spikebot/inspiration.py).
  /style          preferred settings (learned + pinned), pin or unpin them.
  /preset         apply | list | show | rename | edit | delete | combine.

Only approvers (review.approver_ids, or server managers if empty) can press buttons or
use the commands. Layouts are built as plain dicts by pure functions (tested); the
discord.py glue is kept thin.
"""
from __future__ import annotations

import asyncio
import json
import re
import logging
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import feedback as fb
from . import presets as presets_
from . import review
from . import style as st

log = logging.getLogger("discord_bot")

MODE_LABEL = {"lacy": ("Lacy", 0x9146FF), "tokyo_girl": ("Tokyo Girl", 0xFF4FA3)}
VARIANT_COLOUR = {"A": 0x3BA55D, "B": 0x5865F2, "C": 0xFAA61A, "P": 0xEB459E}
SEP = "|"


# --------------------------------------------------------------------------
# Pure layout helpers (tested without Discord)
# --------------------------------------------------------------------------
def cid(action: str, item_id: str, n: int = 0, label: str = "") -> str:
    return SEP.join(["rv", action, item_id, str(n), label])


def parse_cid(custom_id: str) -> dict | None:
    parts = (custom_id or "").split(SEP)
    if len(parts) != 5 or parts[0] != "rv":
        return None
    return {"action": parts[1], "item": parts[2], "round": int(parts[3] or 0), "label": parts[4]}


def code(text: str, limit: int = 380) -> str:
    t = (text or "").replace("```", "'''")
    if len(t) > limit:
        t = t[:limit - 1] + "…"
    return f"```{t}```"


def ts(t: float, style: str = "f") -> str:
    return f"<t:{int(t)}:{style}>"


def round_payload(item: dict, rnd: dict, extra: str = "") -> dict:
    """Message for one review round: header + one embed per variant + button rows."""
    label, colour = MODE_LABEL.get(item["mode"], (item["mode"], 0x5865F2))
    kind = {"initial": "", "redo": " · redo", "feedback": " · after your feedback",
            "preset": " · preset"}.get(rnd["kind"], "")
    head = [f"**{label}** · `{item['id']}` · round {rnd['n']}{kind}"]
    src = item.get("source") or {}
    if src.get("vod_link"):
        head.append(f"Source: <{src['vod_link']}>")
    if rnd.get("comment"):
        head.append(rnd["comment"])
    if item.get("expires"):
        head.append(f"Expires {ts(item['expires'], 'R')} if not approved.")
    if any(v.get("silent") for v in rnd["variants"]):
        head.append("Silent edits: add the official sound in each app when posting.")
    if extra:
        head.append(extra)
    embeds = []
    for v in rnd["variants"]:
        name = f"{v['label']}" + (" · 🧪 experimental" if v.get("experimental") else "") + \
               (f" · preset {v['preset']}" if v.get("preset") else "") + (" · ✏️ edited" if v.get("edited") else "")
        caps = v.get("captions") or {}
        embeds.append({
            "title": name[:250], "color": VARIANT_COLOUR.get(v["label"][0], 0x99AAB5),
            "description": (f"{v.get('duration', '?')}s · {v.get('cuts', 0)} cuts\n{st.describe(v['style'])}"
                            + (f"\n{v['music_note']}" if v.get("music_note") else ""))[:1500],
            "fields": [{"name": "YouTube title", "value": code(v.get("title", ""), 120), "inline": False},
                       {"name": "Instagram caption", "value": code(caps.get("instagram", "")), "inline": False},
                       {"name": "TikTok caption", "value": code(caps.get("tiktok", "")), "inline": False}],
        })
    return {"content": "\n".join(head)[:1900], "embeds": embeds[:9],
            "rows": buttons(item, rnd), "allowed_mentions": {"parse": []}}


def buttons(item: dict, rnd: dict, disabled: bool = False) -> list[list[dict]]:
    n = rnd["n"]
    labels = [v["label"] for v in rnd["variants"]]
    row1 = [{"label": f"Approve {lab}", "style": "success", "custom_id": cid("approve", item["id"], n, lab),
             "disabled": disabled} for lab in labels]
    row2 = [{"label": "Redo (3 new)", "style": "secondary", "custom_id": cid("redo", item["id"], n), "disabled": disabled},
            {"label": "Reject", "style": "danger", "custom_id": cid("reject", item["id"], n), "disabled": disabled},
            {"label": "Feedback", "style": "primary", "custom_id": cid("feedback", item["id"], n), "disabled": disabled},
            {"label": "Edit caption", "style": "secondary", "custom_id": cid("edit", item["id"], n), "disabled": disabled}]
    return [row1, row2]


def picker(action: str, item: dict, rnd: dict) -> list[list[dict]]:
    """'Which variant?' buttons for Feedback / Edit caption."""
    return [[{"label": f"{'Feedback on' if action == 'fbpick' else 'Edit'} {v['label']}", "style": "primary",
              "custom_id": cid(action, item["id"], rnd["n"], v["label"]), "disabled": False}
             for v in rnd["variants"]]]


def manual_payload(item: dict, v: dict, platforms: list[str], link: str | None = None,
                   path_note: str | None = None) -> dict:
    label, colour = MODE_LABEL.get(item["mode"], (item["mode"], 0x5865F2))
    names = [review.NAMES[p] for p in platforms]
    lines = [f"📱 **{label}** `{item['id']}` approved ({v['label']}) · post on **{', '.join(names)}**"]
    if v.get("silent"):
        lines.append(v["music_note"] if v.get("music_note")
                     else "Silent edit: add the official Tokyo Girl sound in the app.")
    caps = v.get("captions") or {}
    blocks = []
    if "tiktok" in platforms:
        blocks.append(("TikTok caption", caps.get("tiktok", "")))
    if "instagram" in platforms:
        blocks.append(("Instagram caption", caps.get("instagram", "")))
    if "youtube" in platforms:
        blocks.append(("YouTube title", v.get("title", "")))
    job = (item.get("source") or {}).get("job_id")
    if item["mode"] == "lacy" and job:
        flags = " ".join(f"--{p} URL" for p in platforms if p in ("tiktok", "instagram"))
        blocks.append(("Then log the links (Content Rewards)",
                       f"cd /opt/spikebot && sudo -u spikebot venv/bin/python -m spikebot.lacy posted {job} {flags}"))
    content = "\n".join(lines)
    for k, text in blocks:
        content += f"\n{k}:\n```{text.replace('```', '')}```"
    out = {"content": content[:1990], "allowed_mentions": {"parse": []}}
    if link:
        out["embeds"] = [{"title": "Download the file", "url": link, "color": colour,
                          "description": "Too big to attach; link works for 7 days."}]
    elif path_note:
        out["embeds"] = [{"title": "Too big for Discord", "color": colour,
                          "description": f"Copy it from the server:\n`{path_note}`"}]
    return out


def style_overview(profile: st.Profile, presets: dict[str, dict]) -> str:
    """/style text: what variant A uses now, per mode, with scores and pins."""
    lines = ["**Preferred settings** (variant A uses these; 📌 = pinned with /style)"]
    pins = profile.data["pinned"]
    for mode in ("lacy", "tokyo_girl"):
        pref = profile.preferred(mode)
        bits = []
        for dim, val in pref.items():
            key = st.stat_key(dim, mode)
            pinned = (pins.get(f"hook:{mode}") if dim == "hook" else pins.get(dim)) == val
            row = profile.data["stats"].get(key, {}).get(val, {})
            bits.append(f"{dim} **{val}**{' 📌' if pinned else ''} ({row.get('approved', 0):g}/{row.get('shown', 0):g})")
        lines.append(f"__{MODE_LABEL.get(mode, (mode,))[0]}__: " + " · ".join(bits))
    intents = profile.data["feedback"]["intents"]
    if intents:
        top = sorted(intents.items(), key=lambda kv: kv[1], reverse=True)[:6]
        lines.append("**Recurring feedback**: " + ", ".join(f"{k} ×{v}" for k, v in top))
    if presets:
        ranked = profile.top_presets(list(presets), n=5)
        rows = profile.data["presets"]
        lines.append("**Presets by approvals**: " + ", ".join(
            f"{n} ({rows.get(n, {}).get('approved', 0)}/{rows.get(n, {}).get('shown', 0)})" for n in ranked))
    decided = [d for d in profile.data["decisions"] if d["action"] in ("approve", "reject")]
    if decided:
        appr = sum(1 for d in decided if d["action"] == "approve")
        lines.append(f"Decisions logged: {len(profile.data['decisions'])} ({appr} approved, {len(decided) - appr} rejected)")
    lines.append("Pin: `/style setting:<name> value:<option>` · unpin: `value:auto`.")
    return "\n".join(lines)[:1990]


STYLE_SETTINGS = ["grade", "pace", "font", "size", "position", "colour", "transition", "ending",
                  "text_level", "intro", "hook:lacy", "hook:tokyo_girl"]


def style_options(setting: str) -> list[str]:
    if setting.startswith("hook:"):
        return list(st.HOOKS.get(setting[5:], ()))
    opts = st.dims("lacy").get(setting) or st.dims("tokyo_girl").get(setting, ())
    return list(opts)


# --------------------------------------------------------------------------
# The bot
# --------------------------------------------------------------------------
class ReviewBot:
    def __init__(self, cfg: dict, config_path: str | Path, notifier=None):
        self.cfg = cfg
        self.config_path = str(config_path)
        self.base = Path(config_path).resolve().parent if Path(config_path).suffix else Path(config_path)
        self.rcfg = review.settings(cfg)
        dd = Path(cfg.get("data_dir", "data"))
        self.data_dir = dd if dd.is_absolute() else self.base / dd
        self.store = review.Store(self.data_dir)
        self.notifier = notifier
        self.client = None
        self.render_q: asyncio.Queue | None = None
        self.queued: set[str] = set()
        self.wake = asyncio.Event()
        self.publish_lock = asyncio.Lock()   # one publish pass at a time: never double-upload
        self.profile_lock = threading.Lock()   # /feedback writes the style profile from a thread
        self.pending_uploads: dict[int, dict] = {}   # inspiration thread id -> name/copy
        self.started = False
        self._publisher = None

    # ---- helpers ------------------------------------------------------------
    @property
    def publisher(self):
        if self._publisher is None:
            self._publisher = review.Publisher(self.cfg, self.base, self.store)
        return self._publisher

    def allowed(self, user) -> bool:
        ids = self.rcfg["approver_ids"]
        if ids:
            return user.id in ids
        perms = getattr(user, "guild_permissions", None)
        return bool(perms and (perms.manage_guild or perms.administrator))

    async def channel(self, channel_id: int):
        if not channel_id:
            return None
        ch = self.client.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.client.fetch_channel(channel_id)
            except Exception as e:  # noqa: BLE001
                log.warning("channel %s not reachable: %s", channel_id, e)
                return None
        return ch

    async def say(self, item: dict, text: str, file=None) -> None:
        """Message in the clip's thread (or #ready-to-post if it has none)."""
        ch = await self.channel(item.get("thread_id") or self.rcfg["channel_id"])
        if ch is None:
            log.info("review %s: %s", item["id"], text)
            return
        import discord
        kw = {"allowed_mentions": discord.AllowedMentions.none()}
        if file is not None:
            kw["file"] = file
        try:
            await ch.send(text[:1990], **kw)
        except Exception as e:  # noqa: BLE001
            log.warning("review %s: thread message failed: %s", item["id"], e)

    def view(self, rows: list[list[dict]]):
        import discord
        v = discord.ui.View(timeout=None)
        styles = {"success": discord.ButtonStyle.success, "danger": discord.ButtonStyle.danger,
                  "primary": discord.ButtonStyle.primary, "secondary": discord.ButtonStyle.secondary}
        for r, row in enumerate(rows):
            for b in row:
                v.add_item(discord.ui.Button(label=b["label"], style=styles[b["style"]], custom_id=b["custom_id"],
                                             disabled=b["disabled"], row=r))
        return v

    @staticmethod
    def embeds(dicts: list[dict]):
        import discord
        return [discord.Embed.from_dict(d) for d in dicts]

    # ---- life cycle -----------------------------------------------------------
    async def run_forever(self) -> None:
        """Connect and stay connected. Never raises (a bad token is reported once)."""
        try:
            import discord
        except ImportError:
            log.warning("discord.py is not installed: pip install -r requirements.txt")
            return
        while True:
            self.client = self.build_client()
            try:
                await self.client.start(self.rcfg["token"])
            except discord.LoginFailure:
                log.error("DISCORD_BOT_TOKEN was refused by Discord; approval bot is off")
                if self.notifier:
                    await self.notifier.send("discord_token", "🤖 The Discord approval bot token was refused."
                                             " Reset it in the Developer Portal and update DISCORD_BOT_TOKEN in .env.")
                return
            except discord.PrivilegedIntentsRequired:
                log.error("enable MESSAGE CONTENT INTENT for the bot in the Developer Portal")
                if self.notifier:
                    await self.notifier.send("discord_intent", "🤖 Turn on **Message Content Intent** for the"
                                             " approval bot (Developer Portal > Bot), then restart spikebot.")
                return
            except Exception as e:  # noqa: BLE001
                log.warning("discord bot stopped: %s; reconnecting in 60s", e)
            finally:
                try:
                    await self.client.close()
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(60)

    def build_client(self):
        import discord
        from discord import app_commands
        intents = discord.Intents.default()
        intents.message_content = True  # links and "copy:" notes in #inspiration
        client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        tree = app_commands.CommandTree(client)
        self.register_commands(tree)
        bot = self

        @client.event
        async def on_ready():
            log.info("discord approval bot online as %s", client.user)
            try:
                if bot.rcfg["guild_id"]:
                    g = discord.Object(id=bot.rcfg["guild_id"])
                    tree.copy_global_to(guild=g)
                    await tree.sync(guild=g)
                else:
                    await tree.sync()
            except Exception as e:  # noqa: BLE001
                log.warning("slash command sync failed: %s", e)
            if not bot.started:
                bot.started = True
                bot.render_q = asyncio.Queue()
                for coro in (bot.scan_loop(), bot.render_worker(), bot.publish_loop()):
                    asyncio.create_task(coro)

        @client.event
        async def on_interaction(interaction):
            if interaction.type == discord.InteractionType.component:
                data = parse_cid((interaction.data or {}).get("custom_id", ""))
                if data:
                    await bot.on_button(interaction, data)

        @client.event
        async def on_message(message):
            try:
                await bot.on_message(message)
            except Exception as e:  # noqa: BLE001
                log.warning("inspiration message failed: %s", e)
        return client

    # ---- loops ----------------------------------------------------------------
    async def scan_loop(self, every: float = 15) -> None:
        while True:
            try:
                await self.scan(time.time())
            except Exception as e:  # noqa: BLE001
                log.warning("review scan: %s", e)
            try:
                await asyncio.wait_for(self.wake.wait(), every)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def scan(self, now: float) -> None:
        for item in self.store.all():
            st_ = item["status"]
            if st_ in ("open", "approved"):
                for rnd in item["rounds"]:
                    if rnd["status"] == "ready" and not rnd.get("message_id"):
                        await self.post_round(item["id"], rnd["n"])
                        item = self.store.load(item["id"])
            req = item.get("request") or {}
            if req.get("status") == "failed" and not req.get("told"):
                await self.say(item, f"⚠️ Re-render failed: {req.get('error')}")
                with self.store.edit(item["id"]) as it:
                    it["request"]["told"] = True
            if req.get("status") == "queued" and item["id"] not in self.queued and st_ == "open":
                self.queued.add(item["id"])
                self.render_q.put_nowait(item["id"])
            if st_ == "failed" and not item.get("told"):
                if self.notifier:
                    await self.notifier.send(f"review_failed:{item['id']}",
                                             f"🎬 `{item['id']}` could not be rendered: {item.get('error')}", kind="lacy")
                with self.store.edit(item["id"]) as it:
                    it["told"] = True
            if review.due_to_expire(item, now):
                await self.expire(item["id"], now)
            if st_ == "approved":
                if (item.get("publish") or {}).get("manual", {}).get("status") == "queued":
                    await self.post_manual(item["id"])
                if review.publish_finished(self.store.load(item["id"])):
                    await self.complete(item["id"])

    async def render_worker(self) -> None:
        """Re-renders one at a time in a subprocess (CPU heavy, keeps the bot responsive)."""
        while True:
            item_id = await self.render_q.get()
            try:
                p = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "spikebot.review", "--config", self.config_path, "render", item_id,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
                out, _ = await asyncio.wait_for(p.communicate(), 3600)
                log.info("review render %s: %s", item_id, out.decode(errors="replace")[-300:].strip())
                item = self.store.load(item_id)
                if (item.get("request") or {}).get("status") == "rendering":  # crashed
                    with self.store.edit(item_id) as it:
                        it["request"].update(status="failed", error=out.decode(errors="replace")[-300:])
            except Exception as e:  # noqa: BLE001
                log.warning("review render %s: %s", item_id, e)
            finally:
                self.queued.discard(item_id)
                self.wake.set()

    async def publish_loop(self, every: float = 60) -> None:
        while True:
            try:
                await self.publish_due(time.time())
            except Exception as e:  # noqa: BLE001
                log.warning("publish loop: %s", e)
            await asyncio.sleep(every)

    async def publish_due(self, now: float) -> None:
        async with self.publish_lock:
            await self._publish_due(now)

    async def _publish_due(self, now: float) -> None:
        for item_id, platform in self.publisher.due(now):
            row = await asyncio.to_thread(self.publisher.publish, item_id, platform, now)
            item = self.store.load(item_id)
            name = review.NAMES[platform]
            if row["status"] == "done":
                extra = f" ({row['note']})" if row.get("note") else ""
                await self.say(item, f"✅ **{name}** is live: {row.get('url')}{extra}")
            elif row["status"] == "failed":
                await self.say(item, f"❌ **{name}** failed {row.get('attempts')}×, giving up: {row.get('error')}")
                if self.notifier:
                    await self.notifier.send(f"publish_failed:{item_id}:{platform}",
                                             f"❌ `{item_id}` {name} upload failed: {row.get('error')}", kind="lacy")
            elif row["status"] == "waiting" and row.get("told") != row.get("reason"):
                when = review.next_window(self.rcfg["windows"].get(platform) or [], now)
                await self.say(item, f"⏳ **{name}** queued: {row.get('reason')}."
                               + (f" Next window {ts(when, 'R')}." if when > now else " Retrying hourly."))
                with self.store.edit(item_id) as it:
                    it["publish"][platform]["told"] = row.get("reason")
            elif row["status"] == "retry":
                await self.say(item, f"🔁 **{name}** failed ({row.get('error')}); retrying in a minute.")
        self.wake.set()

    # ---- posting rounds ---------------------------------------------------------
    async def post_round(self, item_id: str, n: int) -> None:
        import discord
        item = self.store.load(item_id)
        rnd = review.round_of(item, n)
        payload = round_payload(item, rnd)
        d = self.store.dir(item_id)
        files = [discord.File(d / v["preview"], filename=f"{v['label']}_{item_id}.mp4")
                 for v in rnd["variants"] if (d / v["preview"]).exists()]
        if item.get("thread_id"):
            target = await self.channel(item["thread_id"])
        else:
            target = await self.channel(self.rcfg["channel_id"])
        if target is None:
            return
        kw = dict(content=payload["content"], embeds=self.embeds(payload["embeds"]),
                  view=self.view(payload["rows"]), allowed_mentions=discord.AllowedMentions.none())
        try:
            msg = await target.send(files=files, **kw)
        except discord.HTTPException as e:
            if e.status != 413:
                raise
            links = await asyncio.to_thread(self.preview_links, item, rnd)
            kw["content"] = (payload["content"] + "\nPreviews (too big to attach): "
                             + " · ".join(f"[{k}]({u})" for k, u in links.items()))[:1990] if links else \
                payload["content"] + "\nPreviews too big to attach and no bucket set: approve from the cards."
            msg = await target.send(**kw)
        with self.store.edit(item_id) as it:
            r = review.round_of(it, n)
            r["message_id"], r["channel_id"], r["status"] = msg.id, target.id, "posted"
        if not item.get("thread_id"):
            try:
                th = await msg.create_thread(name=(item.get("title") or item_id)[:95], auto_archive_duration=4320)
                with self.store.edit(item_id) as it:
                    it["thread_id"] = th.id
            except Exception as e:  # noqa: BLE001
                log.warning("review %s: could not create a thread: %s", item_id, e)

    def preview_links(self, item: dict, rnd: dict) -> dict[str, str]:
        from . import delivery
        dcfg = delivery.settings(self.cfg, self.base)
        if not dcfg["gcs_bucket"]:
            return {}
        up = delivery.GCSUploader(dcfg["gcs_bucket"], dcfg["gcs_key_file"])
        d = self.store.dir(item["id"])
        return {v["label"]: up.upload(d / v["preview"], f"videos/review/{item['id']}_{v['preview']}", 2)
                for v in rnd["variants"]}

    async def close_round_messages(self, item: dict, note: str) -> None:
        """Grey out the buttons on every posted round of the item."""
        for rnd in item["rounds"]:
            if not rnd.get("message_id"):
                continue
            ch = await self.channel(rnd.get("channel_id"))
            if ch is None:
                continue
            try:
                msg = await ch.fetch_message(rnd["message_id"])
                content = (msg.content + f"\n{note}")[:1990] if note not in msg.content else msg.content
                await msg.edit(content=content, view=self.view(buttons(item, rnd, disabled=True)))
            except Exception as e:  # noqa: BLE001
                log.warning("review %s: could not update round %s: %s", item["id"], rnd["n"], e)

    # ---- decisions ----------------------------------------------------------------
    async def on_button(self, interaction, data: dict) -> None:
        if not self.allowed(interaction.user):
            await interaction.response.send_message("Only Tim can review clips.", ephemeral=True)
            return
        try:
            item = self.store.load(data["item"])
        except (FileNotFoundError, ValueError):
            await interaction.response.send_message("That clip no longer exists.", ephemeral=True)
            return
        a = data["action"]
        try:
            if a == "approve":
                await self.do_approve(interaction, item, data["round"], data["label"])
            elif a == "reject":
                await self.do_reject(interaction, item, data["round"])
            elif a == "redo":
                await self.do_redo(interaction, item, data["round"])
            elif a in ("feedback", "edit"):
                rnd = review.round_of(item, data["round"])
                review.require_open(item)
                pick = "fbpick" if a == "feedback" else "edpick"
                if len(rnd["variants"]) == 1:
                    await self.open_modal(interaction, pick, item, rnd["n"], rnd["variants"][0]["label"])
                else:
                    await interaction.response.send_message("Which variant?", ephemeral=True,
                                                            view=self.view(picker(pick, item, rnd)))
            elif a in ("fbpick", "edpick"):
                review.require_open(item)
                await self.open_modal(interaction, a, item, data["round"], data["label"])
        except ValueError as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"⚠️ {e}", ephemeral=True)

    async def do_approve(self, interaction, item: dict, n: int, label: str) -> None:
        await interaction.response.defer(thinking=False)
        available = await asyncio.to_thread(self.publisher.available)
        with self.store.edit(item["id"]) as it:
            review.approve(it, n, label, self.rcfg, available)
        item = self.store.load(item["id"])
        profile = self.store.profile()
        profile.decide("approve", item["mode"], item["id"], review.round_of(item, n)["variants"], label,
                       extra={"by": interaction.user.id})
        profile.save()
        await self.close_round_messages(item, f"✅ **Approved {label}** by {interaction.user.display_name}")
        auto = [review.NAMES[p] for p in item["publish"] if p != "manual"]
        manual = [review.NAMES[p] for p in item["publish"]["manual"]["platforms"]]
        await self.say(item, f"✅ Approved **{label}**. Auto: {', '.join(auto) or 'none'} (posting windows and"
                             f" daily caps apply). Manual via #tiktok-queue: {', '.join(manual)}.")
        self.wake.set()
        asyncio.create_task(self.publish_due(time.time()))

    async def do_reject(self, interaction, item: dict, n: int) -> None:
        review.require_open(item)
        await interaction.response.defer(thinking=False)
        profile = self.store.profile()
        profile.decide("reject", item["mode"], item["id"], review.round_of(item, n)["variants"],
                       extra={"by": interaction.user.id})
        profile.save()
        item = await asyncio.to_thread(review.finish, self.store, self.cfg, self.base, item["id"], "rejected",
                                       None, "rejected in Discord")
        await self.close_round_messages(item, f"🗑️ **Rejected** by {interaction.user.display_name}")

    async def do_redo(self, interaction, item: dict, n: int) -> None:
        with self.store.edit(item["id"]) as it:
            review.request(it, "redo", round=n)
        profile = self.store.profile()
        profile.decide("redo", item["mode"], item["id"], review.round_of(item, n)["variants"])
        profile.save()
        await interaction.response.send_message("🔄 Rendering 3 new variants; they'll appear in the thread.",
                                                ephemeral=True)
        await self.say(item, "🔄 Redo requested: rendering 3 new variants…")
        self.wake.set()

    async def open_modal(self, interaction, kind: str, item: dict, n: int, label: str) -> None:
        import discord
        bot = self
        v = review.variant_of(item, n, label)
        if kind == "fbpick":
            class Feedback(discord.ui.Modal, title=f"Feedback on {label}"):
                notes = discord.ui.TextInput(label="What should change?", style=discord.TextStyle.paragraph,
                                             max_length=800, placeholder="bigger text, faster, more colour,"
                                             " hook: Lacy did NOT see this coming")

                async def on_submit(self, inter):
                    try:
                        with bot.store.edit(item["id"]) as it:
                            review.request(it, "feedback", round=n, label=label, notes=str(self.notes))
                    except ValueError as e:
                        await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                        return
                    await inter.response.send_message("📝 Got it, re-rendering with your notes.", ephemeral=True)
                    await bot.say(item, f"📝 Feedback on **{label}**: {str(self.notes)[:400]}\nRe-rendering…")
                    bot.wake.set()
            await interaction.response.send_modal(Feedback())
            return

        caps = v.get("captions") or {}

        class Edit(discord.ui.Modal, title=f"Edit caption {label}"):
            title_in = discord.ui.TextInput(label="YouTube title", default=v.get("title", "")[:100], max_length=100)
            ig = discord.ui.TextInput(label="Instagram caption", style=discord.TextStyle.paragraph,
                                      default=caps.get("instagram", "")[:2000], max_length=2000)
            tt = discord.ui.TextInput(label="TikTok caption", style=discord.TextStyle.paragraph,
                                      default=caps.get("tiktok", "")[:2000], max_length=2000)

            async def on_submit(self, inter):
                try:
                    adapter = review.adapter_for(item["mode"], bot.cfg, bot.base)
                    with bot.store.edit(item["id"]) as it:
                        review.edit_caption(it, n, label, str(self.title_in), str(self.ig), str(self.tt),
                                            adapter.validate)
                except ValueError as e:
                    await inter.response.send_message(f"⚠️ Not saved: {e}", ephemeral=True)
                    return
                await inter.response.send_message("✏️ Saved. The card shows the new text.", ephemeral=True)
                await bot.refresh_round(item["id"], n)
                profile = bot.store.profile()
                profile.data["decisions"].append({"ts": time.time(), "action": "edit_caption", "mode": item["mode"],
                                                  "item": item["id"], "chosen": label})
                profile.save()
        await interaction.response.send_modal(Edit())

    async def refresh_round(self, item_id: str, n: int) -> None:
        item = self.store.load(item_id)
        rnd = review.round_of(item, n)
        ch = await self.channel(rnd.get("channel_id"))
        if ch is None or not rnd.get("message_id"):
            return
        payload = round_payload(item, rnd)
        try:
            msg = await ch.fetch_message(rnd["message_id"])
            await msg.edit(content=payload["content"], embeds=self.embeds(payload["embeds"]),
                           view=self.view(payload["rows"]))
        except Exception as e:  # noqa: BLE001
            log.warning("review %s: card refresh failed: %s", item_id, e)

    async def expire(self, item_id: str, now: float) -> None:
        item = self.store.load(item_id)
        rnd = review.latest_round(item)
        profile = self.store.profile()
        profile.decide("expire", item["mode"], item_id, rnd["variants"] if rnd else [], now=now)
        profile.save()
        item = await asyncio.to_thread(review.finish, self.store, self.cfg, self.base, item_id, "expired", now,
                                       "expired after 48 h without approval")
        await self.close_round_messages(item, "⌛ **Expired** (not approved within 48 h)")

    async def post_manual(self, item_id: str) -> None:
        """#tiktok-queue: the approved file + captions for the manual platforms."""
        import discord
        item = self.store.load(item_id)
        a = item["approved"]
        v = review.variant_of(item, a["round"], a["label"])
        platforms = item["publish"]["manual"]["platforms"]
        export = await asyncio.to_thread(review.tiktok_export, self.cfg, self.base, self.store, item)
        path = review.make_final(self.store, item)
        ch = await self.channel(self.rcfg["tiktok_channel_id"])
        status, link_text = "done", ""
        if ch is None:
            status = "no-channel"
            link_text = f"#tiktok-queue isn't set up; the file is on the server at `{export or path}`"
        else:
            limit = getattr(getattr(ch, "guild", None), "filesize_limit", 10 * 1024 * 1024) - 64 * 1024
            try:
                if path.stat().st_size <= limit:
                    p = manual_payload(item, v, platforms)
                    msg = await ch.send(p["content"], file=discord.File(path, filename=f"{item_id}.mp4"),
                                        allowed_mentions=discord.AllowedMentions.none())
                else:
                    link = await asyncio.to_thread(self.big_file_link, item, path)
                    p = manual_payload(item, v, platforms, link=link, path_note=None if link else str(export or path))
                    msg = await ch.send(p["content"], embeds=self.embeds(p.get("embeds", [])),
                                        allowed_mentions=discord.AllowedMentions.none())
                link_text = f"📱 Manual posting ({', '.join(review.NAMES[x] for x in platforms)}): {msg.jump_url}"
            except Exception as e:  # noqa: BLE001
                status = "retry"
                link_text = f"⚠️ Couldn't post to #tiktok-queue ({e}); retrying."
        with self.store.edit(item_id) as it:
            it["publish"]["manual"]["status"] = "done" if status in ("done", "no-channel") else "queued"
            it["publish"]["manual"]["export"] = str(export) if export else None
        await self.say(item, link_text)

    def big_file_link(self, item: dict, path: Path) -> str | None:
        from . import delivery
        dcfg = delivery.settings(self.cfg, self.base)
        if not dcfg["gcs_bucket"]:
            return None
        up = delivery.GCSUploader(dcfg["gcs_bucket"], dcfg["gcs_key_file"])
        return up.upload(path, f"videos/{item['mode']}/{item['id']}.mp4", int(dcfg["link_days"]))

    async def complete(self, item_id: str) -> None:
        item = self.store.load(item_id)
        lines = []
        for p, row in item["publish"].items():
            if p == "manual":
                continue
            lines.append(f"{review.NAMES[p]}: {row.get('url') or row.get('status')}")
        await asyncio.to_thread(review.finish, self.store, self.cfg, self.base, item_id, "done")
        await self.say(item, "🏁 Done. " + (" · ".join(lines) if lines else "Manual platforms only."))

    # ---- #inspiration -------------------------------------------------------------
    async def on_message(self, message) -> None:
        import discord
        if message.author.bot or not self.rcfg["inspiration_channel_id"]:
            return
        ch = message.channel
        in_thread = getattr(ch, "parent_id", None) is not None   # threads have a parent channel
        home = ch.parent_id if in_thread else ch.id
        if home != self.rcfg["inspiration_channel_id"]:
            return
        if not self.allowed(message.author):
            return
        from . import inspiration
        songs = [a for a in message.attachments
                 if (a.content_type or "").startswith("audio/")
                 or a.filename.lower().endswith((".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac", ".opus"))]
        if songs:
            await self.add_songs(message, songs)
            return
        videos = [a for a in message.attachments
                  if (a.content_type or "").startswith("video/") or a.filename.lower().endswith((".mp4", ".mov", ".webm", ".mkv"))]
        urls = inspiration.links(message.content)
        if not videos and not urls:
            if not in_thread:
                await message.reply("Send a TikTok / Instagram / YouTube link or upload a video. Optional notes:"
                                    " `copy: pacing, colour, text, transitions, hook, structure, music-sync, look`,"
                                    " `name: my-preset`, and anything about what you like in it.",
                                    mention_author=False)
            return
        pending = self.pending_uploads.pop(ch.id, None) if in_thread else None
        copy = presets_.parse_copy(message.content) if "copy:" in message.content.lower() or not pending \
            else pending["copy"]
        if in_thread:
            thread = ch
        else:
            thread = await message.create_thread(name=f"Inspiration {datetime.now():%d %b %H:%M}"[:95],
                                                 auto_archive_duration=1440)
        jobs = [("url", u) for u in urls] + [("file", a) for a in videos]
        for i, (kind, src) in enumerate(jobs):
            name = presets_.parse_name(message.content) or (pending or {}).get("name")
            if not name or len(jobs) > 1:
                plat = inspiration.platform_of(src) if kind == "url" else "upload"
                name = f"{name or plat}-{datetime.now():%m%d}" + (f"-{i + 1}" if len(jobs) > 1 else "")
            await thread.send(f"🔎 Analysing{' ' + src if kind == 'url' else ' your upload'} → preset"
                              f" `{name}` (copies: {', '.join(copy)})…", suppress_embeds=True)
            path = None
            if kind == "file":
                tmp = self.data_dir / "inspiration" / "uploads"
                tmp.mkdir(parents=True, exist_ok=True)
                path = tmp / f"{uuid.uuid4().hex}{Path(src.filename).suffix or '.mp4'}"
                await src.save(path)
            note = re.sub(r"https?://\S+|\b(copy|name)\s*:[^\n]*", " ", message.content or "", flags=re.I).strip()
            res = await self.analyse(name, copy, url=src if kind == "url" else None, file=path, note=note)
            if res.get("ok"):
                f = discord.File(res["strip"], filename=f"{res['name']}_strip.jpg") if res.get("strip") else None
                await thread.send(res["text"], file=f) if f else await thread.send(res["text"])
                if res.get("art"):
                    await thread.send(res["art"])
                if res.get("strip"):
                    Path(res["strip"]).unlink(missing_ok=True)
            elif res.get("ask_upload"):
                self.pending_uploads[thread.id] = {"name": name, "copy": copy}
                await thread.send(f"⚠️ I couldn't download that link ({res.get('error')}).\n"
                                  "**Please upload the video file directly here in this thread** and I'll analyse it.")
            else:
                await thread.send(f"⚠️ Analysis failed: {res.get('error')}")

    async def add_songs(self, message, songs) -> None:
        """Audio in #inspiration = the song to cut Tokyo Girl edits to. Only measured (beats,
        drop); edits stay silent and Tim adds the sound in the app."""
        for a in songs:
            name = presets_.parse_name(message.content) or Path(a.filename).stem
            tmp = self.data_dir / "music" / ".incoming"
            tmp.mkdir(parents=True, exist_ok=True)
            path = tmp / f"{uuid.uuid4().hex}{Path(a.filename).suffix.lower() or '.mp3'}"
            await a.save(path)
            out = tmp / f"{uuid.uuid4().hex}.json"
            try:
                p = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "spikebot.music", "--config", self.config_path, "add", str(path),
                    "--name", name, "--json", str(out), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
                text, _ = await asyncio.wait_for(p.communicate(), 600)
                res = json.loads(out.read_text(encoding="utf-8")) if out.exists() else \
                    {"ok": False, "error": text.decode(errors="replace")[-200:]}
            except asyncio.TimeoutError:
                res = {"ok": False, "error": "analysis took over 10 minutes"}
            finally:
                path.unlink(missing_ok=True)
                out.unlink(missing_ok=True)
            if res.get("ok"):
                from .music import mmss
                await message.reply(
                    f"🎵 **{res['name']}** is now the song for Tokyo Girl edits: {res['bpm']:.0f} BPM, drop at"
                    f" {mmss(res['drop_s'])}. Cuts will land on its beats and the drop; each card says where to"
                    f" start the sound (about {mmss(res['start_s'])}). The edits stay silent: add the sound in the"
                    " app.", mention_author=False)
            else:
                await message.reply(f"⚠️ Couldn't read that song: {res.get('error')}", mention_author=False)

    async def analyse(self, name: str, copy: list[str], url: str | None = None, file: Path | None = None,
                      note: str = "") -> dict:
        out = self.data_dir / "inspiration" / f"result_{uuid.uuid4().hex}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "spikebot.inspiration", "--config", self.config_path, "analyse",
               "--name", name, "--copy", ",".join(copy), "--json", str(out), "--note", note[:800]]
        cmd += ["--url", url] if url else ["--file", str(file)]
        try:
            p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT)
            text, _ = await asyncio.wait_for(p.communicate(), 1200)
            if out.exists():
                return json.loads(out.read_text(encoding="utf-8"))
            return {"ok": False, "error": text.decode(errors="replace")[-300:]}
        except asyncio.TimeoutError:
            return {"ok": False, "error": "analysis took over 20 minutes"}
        finally:
            out.unlink(missing_ok=True)
            if file is not None:
                Path(file).unlink(missing_ok=True)

    def learn_feedback(self, text: str, mode: str = "all", client=None) -> tuple[dict, list[str]]:
        """/feedback: read it (Claude, or the rule parser), save a rule, pin its settings.
        Runs in a thread (the API call takes a few seconds)."""
        with self.profile_lock:
            profile = self.store.profile()
            rule, problems = fb.learn(text, mode, profile, self.base, client=client)
            profile.save()
        return rule, problems

    # ---- slash commands -------------------------------------------------------------
    def register_commands(self, tree) -> None:
        import discord
        from discord import app_commands
        bot = self

        async def guard(inter) -> bool:
            if not bot.allowed(inter.user):
                await inter.response.send_message("Only Tim can use this.", ephemeral=True)
                return False
            return True

        async def preset_names(inter, current: str):
            names = bot.store.presets().names()
            return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n][:25]

        async def setting_choices(inter, current: str):
            return [app_commands.Choice(name=s, value=s) for s in STYLE_SETTINGS if current.lower() in s][:25]

        async def value_choices(inter, current: str):
            setting = getattr(inter.namespace, "setting", "") or ""
            opts = style_options(setting) + ["auto"]
            return [app_commands.Choice(name=o, value=o) for o in opts if current.lower() in o][:25]

        @tree.command(name="style", description="Show the learned style settings, or pin one")
        @app_commands.autocomplete(setting=setting_choices, value=value_choices)
        async def style_cmd(inter: discord.Interaction, setting: str | None = None, value: str | None = None):
            if not await guard(inter):
                return
            profile = bot.store.profile()
            if setting:
                if setting not in STYLE_SETTINGS:
                    await inter.response.send_message(f"Unknown setting. Try: {', '.join(STYLE_SETTINGS)}",
                                                      ephemeral=True)
                    return
                if value not in (None, "auto") and value not in style_options(setting):
                    await inter.response.send_message(f"{setting} options: {', '.join(style_options(setting))}, auto",
                                                      ephemeral=True)
                    return
                profile.pin(setting, value)
                profile.save()
            text = style_overview(profile, bot.store.presets().all())
            if setting:
                text = (f"📌 {setting} → **{value}**\n" if value not in (None, "auto") else f"↩️ {setting} unpinned\n") + text
            await inter.response.send_message(text[:1990], ephemeral=False)

        @tree.command(name="feedback", description="Tell the editor anything (plain words); it applies to every future edit")
        @app_commands.describe(text="What to change, in your own words",
                               mode="Which edits it's about (default: all)")
        @app_commands.choices(mode=[app_commands.Choice(name="All edits", value="all"),
                                    app_commands.Choice(name="Lacy clips", value="lacy"),
                                    app_commands.Choice(name="Tokyo Girl trailer edits", value="tokyo_girl")])
        async def feedback_cmd(inter: discord.Interaction, text: str, mode: str = "all"):
            if not await guard(inter):
                return
            await inter.response.defer(thinking=True)
            try:
                rule, problems = await asyncio.to_thread(bot.learn_feedback, text, mode)
            except Exception as e:  # noqa: BLE001 - never crash the bot on a command
                log.warning("/feedback failed: %s", e)
                await inter.followup.send(f"⚠️ Couldn't save that: {e}"[:1990])
                return
            msg = ["📝 Saved for every future edit" + (" (read by Claude)" if rule.get("engine") == "claude"
                                                      else " (rule parser: set ANTHROPIC_API_KEY for full understanding)"),
                   fb.format_rule(rule)]
            if problems:
                msg.append("Skipped: " + " | ".join(p[:100] for p in problems[:3]))
            if not rule["settings"] and not rule.get("notes"):
                msg.append("Nothing changed: try saying what to do differently, e.g. *no captions unless he says"
                           " something funny*.")
            msg.append("`/rules` lists everything I'm applying; `/rules remove:<n>` undoes one.")
            await inter.followup.send("\n".join(msg)[:1990])

        @tree.command(name="rules", description="What the editor has learned from /feedback (and undo one)")
        @app_commands.describe(remove="Number of a rule to undo")
        async def rules_cmd(inter: discord.Interaction, remove: int | None = None):
            if not await guard(inter):
                return
            profile = bot.store.profile()
            head = ""
            if remove is not None:
                gone = profile.remove_rule(remove)
                profile.save()
                head = f"↩️ Removed rule #{remove} and undid its settings.\n" if gone else f"No rule #{remove}.\n"
            rules = profile.data.get("rules") or []
            if not rules:
                await inter.response.send_message(head + "No rules yet: use `/feedback`.", ephemeral=False)
                return
            body = "\n\n".join(fb.format_rule(r) for r in reversed(rules[-12:]))
            await inter.response.send_message((head + body)[:1990], ephemeral=False)

        group = app_commands.Group(name="preset", description="Style presets from #inspiration")

        @group.command(name="apply", description="Re-render a clip with a preset (use it in the clip's thread)")
        @app_commands.autocomplete(name=preset_names)
        async def apply(inter: discord.Interaction, name: str, variant: str | None = None, item: str | None = None):
            if not await guard(inter):
                return
            it = None
            if item:
                try:
                    it = bot.store.load(item)
                except (FileNotFoundError, ValueError):
                    pass
            else:
                it = next((x for x in bot.store.all() if x.get("thread_id") == inter.channel_id), None)
            if it is None:
                await inter.response.send_message("Use this inside a #ready-to-post clip thread, or give `item:`.",
                                                  ephemeral=True)
                return
            rnd = review.latest_round(it)
            label = (variant or rnd["variants"][0]["label"]).upper()
            try:
                bot.store.presets().load(name)
                review.variant_of(it, rnd["n"], label)
                with bot.store.edit(it["id"]) as x:
                    review.request(x, "preset", round=rnd["n"], label=label, name=name)
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            await inter.response.send_message(f"🎨 Re-rendering **{label}** with preset `{name}`…")
            bot.wake.set()

        @group.command(name="list", description="All presets, most approved first")
        async def list_(inter: discord.Interaction):
            if not await guard(inter):
                return
            ps = bot.store.presets().all()
            if not ps:
                await inter.response.send_message("No presets yet: post a link in #inspiration.", ephemeral=True)
                return
            prof = bot.store.profile()
            rows = prof.data["presets"]
            names = prof.top_presets(list(ps), n=len(ps))
            text = "\n".join(f"• **{n}** ({', '.join(ps[n].get('copy') or [])}) · approved "
                             f"{rows.get(n, {}).get('approved', 0)}/{rows.get(n, {}).get('shown', 0)}" for n in names)
            await inter.response.send_message(text[:1990])

        @group.command(name="show", description="Settings of one preset")
        @app_commands.autocomplete(name=preset_names)
        async def show(inter: discord.Interaction, name: str):
            if not await guard(inter):
                return
            try:
                p = bot.store.presets().load(name)
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            row = bot.store.profile().data["presets"].get(p["name"])
            await inter.response.send_message(presets_.summary(p, row)[:1990])

        @group.command(name="rename", description="Rename a preset")
        @app_commands.autocomplete(name=preset_names)
        async def rename(inter: discord.Interaction, name: str, new_name: str):
            if not await guard(inter):
                return
            try:
                p = bot.store.presets().rename(name, new_name)
                prof = bot.store.profile()
                if name in prof.data["presets"]:
                    prof.data["presets"][p["name"]] = prof.data["presets"].pop(name)
                    prof.save()
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            await inter.response.send_message(f"Renamed `{name}` → `{p['name']}`.")

        @group.command(name="edit", description="Change one setting (e.g. pace fast, saturation 1.3, copy pacing,colour)")
        @app_commands.autocomplete(name=preset_names)
        async def edit(inter: discord.Interaction, name: str, setting: str, value: str):
            if not await guard(inter):
                return
            try:
                p = bot.store.presets().edit(name, setting, value)
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            await inter.response.send_message(presets_.summary(p)[:1990])

        @group.command(name="delete", description="Delete a preset")
        @app_commands.autocomplete(name=preset_names)
        async def delete(inter: discord.Interaction, name: str):
            if not await guard(inter):
                return
            try:
                bot.store.presets().delete(name)
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            await inter.response.send_message(f"Deleted preset `{name}`.")

        @group.command(name="combine", description="New preset from parts of others (e.g. pacing from one, colour from another)")
        @app_commands.autocomplete(pacing=preset_names, colour=preset_names, text=preset_names,
                                   transitions=preset_names, hook=preset_names, structure=preset_names,
                                   music_sync=preset_names)
        async def combine(inter: discord.Interaction, new_name: str, pacing: str | None = None,
                          colour: str | None = None, text: str | None = None, transitions: str | None = None,
                          hook: str | None = None, structure: str | None = None, music_sync: str | None = None):
            if not await guard(inter):
                return
            parts = {k: v for k, v in (("pacing", pacing), ("colour", colour), ("text", text),
                                        ("transitions", transitions), ("hook", hook), ("structure", structure),
                                        ("music-sync", music_sync)) if v}
            try:
                p = bot.store.presets().combine(new_name, parts)
            except ValueError as e:
                await inter.response.send_message(f"⚠️ {e}", ephemeral=True)
                return
            await inter.response.send_message(presets_.summary(p)[:1990])

        tree.add_command(group)
