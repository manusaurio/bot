import asyncio
import datetime
import logging
import re
import textwrap
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Iterator, Optional

import aiohttp
import discord
from discord import Color, Embed, Message, Object, errors
from discord.ext.commands import BadArgument, Bot, Context

from bot.cogs.modlog import ModLog
from bot.constants import BigBrother as BigBrotherConfig, Guild as GuildConfig, Icons
from bot.pagination import LinePaginator
from bot.utils import messages
from bot.utils.time import time_since

log = logging.getLogger(__name__)

URL_RE = re.compile(r"(https?://[^\s]+)")


def proxy_user(user_id: str) -> Object:
    """A proxy user object that mocks a real User instance for when the later is not available."""
    try:
        user_id = int(user_id)
    except ValueError:
        raise BadArgument

    user = Object(user_id)
    user.mention = user.id
    user.display_name = f"<@{user.id}>"
    user.avatar_url_as = lambda static_format: None
    user.bot = False

    return user


@dataclass
class MessageHistory:
    last_author: Optional[int] = None
    last_channel: Optional[int] = None
    message_count: int = 0

    def __iter__(self) -> Iterator:
        return iter((self.last_author, self.last_channel, self.message_count))


class WatchChannel(ABC):
    """ABC with functionality for relaying users' messages to a certain channel."""

    @abstractmethod
    def __init__(self, bot: Bot, destination, webhook_id, api_endpoint, api_default_params, logger) -> None:
        self.bot = bot

        self.destination = destination  # E.g., Channels.big_brother_logs
        self.webhook_id = webhook_id  # E.g.,  Webhooks.big_brother
        self.api_endpoint = api_endpoint  # E.g., 'bot/infractions'
        self.api_default_params = api_default_params  # E.g., {'active': 'true', 'type': 'watch'}
        self.log = logger  # Logger of the child cog for a correct name in the logs

        self._consume_task = None
        self.watched_users = defaultdict(dict)
        self.message_queue = defaultdict(lambda: defaultdict(deque))
        self.consumption_queue = {}
        self.retries = 5
        self.retry_delay = 10
        self.channel = None
        self.webhook = None
        self.message_history = MessageHistory()

        self._start = self.bot.loop.create_task(self.start_watchchannel())

    @property
    def modlog(self) -> ModLog:
        """Provides access to the ModLog cog for alert purposes."""
        return self.bot.get_cog("ModLog")

    @property
    def consuming_messages(self) -> bool:
        """Checks if a consumption task is currently running."""
        if self._consume_task is None:
            return False

        if self._consume_task.done():
            exc = self._consume_task.exception()
            if exc:
                self.log.exception(
                    f"The message queue consume task has failed with:",
                    exc_info=exc
                )
            return False

        return True

    async def start_watchchannel(self) -> None:
        """Starts the watch channel by getting the channel, webhook, and user cache ready."""
        await self.bot.wait_until_ready()

        # After updating d.py, this block can be replaced by `fetch_channel` with a try-except
        for attempt in range(1, self.retries+1):
            self.channel = self.bot.get_channel(self.destination)
            if self.channel is None:
                if attempt < self.retries:
                    await asyncio.sleep(self.retry_delay)
            else:
                break
        else:
            self.log.error(f"Failed to retrieve the text channel with id {self.destination}")

        # `get_webhook_info` has been renamed to `fetch_webhook` in newer versions of d.py
        try:
            self.webhook = await self.bot.get_webhook_info(self.webhook_id)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            self.log.exception(f"Failed to fetch webhook with id `{self.webhook_id}`")

        if self.channel is None or self.webhook is None:
            self.log.error("Failed to start the watch channel; unloading the cog.")

            message = textwrap.dedent(
                f"""
                An error occurred while loading the text channel or webhook.

                TextChannel: {"**Failed to load**" if self.channel is None else "Loaded successfully"}
                Webhook: {"**Failed to load**" if self.webhook is None else "Loaded successfully"}

                The Cog has been unloaded.
                """
            )

            await self.modlog.send_log_message(
                title=f"Error: Failed to initialize the {self.__class__.__name__} watch channel",
                text=message,
                ping_everyone=True,
                icon_url=Icons.token_removed,
                colour=Color.red()
            )

            self.bot.remove_cog(self.__class__.__name__)
            return

        if not await self.fetch_user_cache():
            await self.modlog.send_log_message(
                title=f"Warning: Failed to retrieve user cache for the {self.__class__.__name__} watch channel",
                text="Could not retrieve the list of watched users from the API and messages will not be relayed.",
                ping_everyone=True,
                icon=Icons.token_removed,
                color=Color.red()
            )

    async def fetch_user_cache(self) -> bool:
        """
        Fetches watched users from the API and updates the watched user cache accordingly.

        This function returns `True` if the update succeeded.
        """
        try:
            data = await self.bot.api_client.get(self.api_endpoint, params=self.api_default_params)
        except aiohttp.ClientResponseError as e:
            self.log.exception(f"Failed to fetch the watched users from the API", exc_info=e)
            return False

        self.watched_users = defaultdict(dict)

        for entry in data:
            user_id = entry.pop('user')
            self.watched_users[user_id] = entry

        return True

    async def on_message(self, msg: Message) -> None:
        """Queues up messages sent by watched users."""
        if msg.author.id in self.watched_users:
            if not self.consuming_messages:
                self._consume_task = self.bot.loop.create_task(self.consume_messages())

            self.log.trace(f"Received message: {msg.content} ({len(msg.attachments)} attachments)")
            self.message_queue[msg.author.id][msg.channel.id].append(msg)

    async def consume_messages(self, delay_consumption: bool = True) -> None:
        """Consumes the message queues to log watched users' messages."""
        if delay_consumption:
            self.log.trace(f"Sleeping {BigBrotherConfig.log_delay} seconds before consuming message queue")
            await asyncio.sleep(BigBrotherConfig.log_delay)

        self.log.trace(f"Started consuming the message queue")

        # If the previous consumption Task failed, first consume the existing comsumption_queue
        if not self.consumption_queue:
            self.consumption_queue = self.message_queue.copy()
            self.message_queue.clear()

        for user_channel_queues in self.consumption_queue.values():
            for channel_queue in user_channel_queues.values():
                while channel_queue:
                    msg = channel_queue.popleft()

                    self.log.trace(f"Consuming message {msg.id} ({len(msg.attachments)} attachments)")
                    await self.relay_message(msg)

        self.consumption_queue.clear()

        if self.message_queue:
            self.log.trace("Channel queue not empty: Continuing consuming queues")
            self._consume_task = self.bot.loop.create_task(self.consume_messages(delay_consumption=False))
        else:
            self.log.trace("Done consuming messages.")

    async def webhook_send(
        self,
        content: Optional[str] = None,
        username: Optional[str] = None,
        avatar_url: Optional[str] = None,
        embed: Optional[Embed] = None,
    ) -> None:
        """Sends a message to the webhook with the specified kwargs."""
        try:
            await self.webhook.send(content=content, username=username, avatar_url=avatar_url, embed=embed)
        except discord.HTTPException as exc:
            self.log.exception(
                f"Failed to send a message to the webhook",
                exc_info=exc
            )

    async def relay_message(self, msg: Message) -> None:
        """Relays the message to the relevant watch channel"""
        last_author, last_channel, count = self.message_history
        limit = BigBrotherConfig.header_message_limit

        if msg.author.id != last_author or msg.channel.id != last_channel or count >= limit:
            self.message_history = MessageHistory(last_author=msg.author.id, last_channel=msg.channel.id)

            await self.send_header(msg)

        cleaned_content = msg.clean_content

        if cleaned_content:
            # Put all non-media URLs in a code block to prevent embeds
            media_urls = {embed.url for embed in msg.embeds if embed.type in ("image", "video")}
            for url in URL_RE.findall(cleaned_content):
                if url not in media_urls:
                    cleaned_content = cleaned_content.replace(url, f"`{url}`")
            await self.webhook_send(
                cleaned_content,
                username=msg.author.display_name,
                avatar_url=msg.author.avatar_url
            )

        if msg.attachments:
            try:
                await messages.send_attachments(msg, self.webhook)
            except (errors.Forbidden, errors.NotFound):
                e = Embed(
                    description=":x: **This message contained an attachment, but it could not be retrieved**",
                    color=Color.red()
                )
                await self.webhook_send(
                    embed=e,
                    username=msg.author.display_name,
                    avatar_url=msg.author.avatar_url
                )
            except discord.HTTPException as exc:
                self.log.exception(
                    f"Failed to send an attachment to the webhook",
                    exc_info=exc
                )

        self.message_history.message_count += 1

    async def send_header(self, msg) -> None:
        """Sends a header embed with information about the relayed messages to the watch channel"""
        user_id = msg.author.id

        guild = self.bot.get_guild(GuildConfig.id)
        actor = guild.get_member(self.watched_users[user_id]['actor'])
        actor = actor.display_name if actor else self.watched_users[user_id]['actor']

        inserted_at = self.watched_users[user_id]['inserted_at']
        time_delta = self._get_time_delta(inserted_at)

        reason = self.watched_users[user_id]['reason']

        embed = Embed(description=f"{msg.author.mention} in [#{msg.channel.name}]({msg.jump_url})")
        embed.set_footer(text=f"Added {time_delta} by {actor} | Reason: {reason}")

        await self.webhook_send(embed=embed, username=msg.author.display_name, avatar_url=msg.author.avatar_url)

    async def list_watched_users(self, ctx: Context, update_cache: bool = True) -> None:
        """
        Gives an overview of the watched user list for this channel.

        The optional kwarg `update_cache` specifies whether the cache should
        be refreshed by polling the API.
        """
        if update_cache:
            if not await self.fetch_user_cache():
                await ctx.send(f":x: Failed to update {self.__class__.__name__} user cache, serving from cache")
                update_cache = False

        lines = []
        for user_id, user_data in self.watched_users.items():
            inserted_at = user_data['inserted_at']
            time_delta = self._get_time_delta(inserted_at)
            lines.append(f"• <@{user_id}> (added {time_delta})")

        lines = lines or ("There's nothing here yet.",)
        embed = Embed(
            title=f"{self.__class__.__name__} watched users ({'updated' if update_cache else 'cached'})",
            color=Color.blue()
        )
        await LinePaginator.paginate(lines, ctx, embed, empty=False)

    @staticmethod
    def _get_time_delta(time_string: str) -> str:
        """Returns the time in human-readable time delta format"""
        date_time = datetime.datetime.strptime(
            time_string,
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=None)
        time_delta = time_since(date_time, precision="minutes", max_units=1)

        return time_delta

    @staticmethod
    def _get_human_readable(time_string: str, output_format: str = "%Y-%m-%d %H:%M:%S") -> str:
        date_time = datetime.datetime.strptime(
            time_string,
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ).replace(tzinfo=None)
        return date_time.strftime(output_format)

    def _remove_user(self, user_id: int) -> None:
        """Removes user from the WatchChannel"""
        self.watched_users.pop(user_id, None)
        self.message_queue.pop(user_id, None)
        self.consumption_queue.pop(user_id, None)

    def cog_unload(self) -> None:
        """Takes care of unloading the cog and canceling the consumption task."""
        self.log.trace(f"Unloading the cog")
        if not self._consume_task.done():
            self._consume_task.cancel()
            try:
                self._consume_task.result()
            except asyncio.CancelledError as e:
                self.log.exception(
                    f"The consume task was canceled. Messages may be lost.",
                    exc_info=e
                )
