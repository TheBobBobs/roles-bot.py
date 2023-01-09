import asyncio
import re
from typing import Union
import os
import sys

from dotenv import load_dotenv
import emoji as emoji_lib


import volty
from volty import Client, LRUCache

from constants import *


ROLE_REGEX = re.compile(r'\{ROLE:([ a-z0-9_-]+)}', flags=re.IGNORECASE)
EMOJI_ROLE_REGEX = re.compile(r':([a-z0-9_-]+):\[]\(([0-9A-HJKMNP-TV-Z]{26})\)', flags=re.IGNORECASE)

CHECKMARK_TEXT = 'white_check_mark'
CHECKMARK_UNICODE = emoji_lib.emojize(f':{CHECKMARK_TEXT}:', language='alias')

RESTRICT = volty.Interactions(reactions=None, restrict_reactions=True)


def get_emoji_text(unicode: str):
    if data := emoji_lib.EMOJI_DATA.get(unicode):
        if aliases := data.get('alias'):
            text = aliases[-1]
        elif 'en' in data:
            text = data['en']
        else:
            raise ValueError
        return text[1:-1]
    raise ValueError


class SetupMessage:
    def __init__(self, owner_id: str, server_id: str, content: str):
        self.owner_id = owner_id
        self.server_id = server_id
        self.content = content
        self.matches = list(ROLE_REGEX.finditer(content))

    def with_emojis(self, emojis: list[str], roles: dict[str, tuple[str, str]]):
        offset = 0
        content = self.content
        for i, emoji_id in enumerate(emojis):
            if i >= len(self.matches):
                break
            match = self.matches[i]
            start = match.regs[0][0] + offset
            end = match.regs[0][1] + offset
            name_or_id = match.groups()[0]
            if role := roles.get(name_or_id):
                role_id = role[0]
                role_name = role[1]
                length = end - start
                text = f':{emoji_id}:[]({role_id}) __{role_name}__'
                new_length = len(text)
                offset += new_length - length
                content = (
                    content[:start]
                    + text
                    + content[end:]
                )

        return content


class ReactionRolesMessage:
    def __init__(self, content: str):
        self.emoji_roles: dict[str, str] = {}
        for match in EMOJI_ROLE_REGEX.finditer(content):
            emoji_id, role_id = match.groups()
            self.emoji_roles[emoji_id] = role_id


class Bot(Client):
    def __init__(self, token: str):
        super().__init__(token)
        self.error_handlers.append(self.on_error)
        self.event_handlers['Ready'].append(self.on_ready)
        self.event_handlers['Message'].append(self.on_message)
        self.event_handlers['MessageDelete'].append(self.on_message_delete)
        self.event_handlers['MessageReact'].append(self.on_react)
        self.event_handlers['MessageUnreact'].append(self.on_react)

        self._setup_messages: LRUCache[str, SetupMessage] = LRUCache(max_length=1_024)
        self._reaction_messages: LRUCache[str, ReactionRolesMessage] = LRUCache(max_length=1_024)

    @staticmethod
    async def on_error(event: volty.events.Event, error: BaseException):
        print(error, event)

    async def on_ready(self, _: volty.events.Ready):
        print(f'Ready as {self.user.username}')
        if self.user.status['text'] != 'Mention Me!':
            await self.http.set_status(text='Mention Me!')

    async def on_message(self, event: volty.events.MessageCreate):
        message = event.data
        content = message.content
        if message.author == self.cache.user_id:
            return
        if not content or not content.startswith(f'<@{self.cache.user_id}>'):
            return
        channel = await self.http.fetch_channel(message.channel)
        if not channel.server:
            return

        text = content[29:].lstrip()
        if text in ('', 'help'):
            await self.http.reply_to(message, HELP_MESSAGE, interactions=RESTRICT)
            return

        user = await self.http.fetch_user(message.author)
        if user.bot:
            return
        await self.reaction_roles_command(message)

    async def on_message_delete(self, event: volty.events.MessageDelete):
        if event.id in self._setup_messages:
            self._setup_messages.pop(event.id)
        elif event.id in self._reaction_messages:
            self._reaction_messages.pop(event.id)

    async def on_react(self, event: Union[volty.events.MessageReact, volty.events.MessageUnreact]):
        message = await self.http.fetch_message(event.channel_id, event.id)
        if message.author != self.cache.user_id:
            return

        if message.replies:
            if event.id not in self._setup_messages:
                if original_message := await self.http.fetch_message(event.channel_id, message.replies[0]):
                    content = original_message.content[29:].lstrip()
                    if not ROLE_REGEX.findall(content):
                        return
                    channel = await self.http.fetch_channel(original_message.channel)
                    setup_message = SetupMessage(original_message.author, channel.server, content)
                    self._setup_messages[event.id] = setup_message
            await self.on_setup_react(event)
        else:
            if event.id not in self._reaction_messages:
                if not message.interactions:
                    return
                reaction_message = ReactionRolesMessage(message.content)
                self._reaction_messages[event.id] = reaction_message
            await self.on_role_react(event)

    async def reaction_roles_command(self, message: volty.Message):
        text = message.content[29:].lstrip()
        if not ROLE_REGEX.findall(text):
            return

        channel = await self.http.fetch_channel(message.channel)
        bot_permissions = await self.fetch_server_permissions(channel.server, self.cache.user_id)
        if not bot_permissions.has(volty.Permission.React):
            await self.http.reply_to(message, 'I don\'t have `React` permissions!', interactions=RESTRICT)
            return
        if not bot_permissions.has(volty.Permission.AssignRoles):
            await self.http.reply_to(message, 'I don\'t have `AssignRoles` permissions!', interactions=RESTRICT)
            return
        user_permissions = await self.fetch_server_permissions(channel.server, message.author)
        if not user_permissions.has(volty.Permission.AssignRoles):
            await self.http.reply_to(message, 'You don\'t have `AssignRoles` permissions!', interactions=RESTRICT)
            return

        server = self.cache.servers[channel.server]
        bot_member = await self.http.fetch_member(channel.server, self.cache.user_id)
        if role := bot_member.highest_role(server):
            bot_rank = role.rank
        else:
            bot_rank = sys.maxsize

        user_member = await self.http.fetch_member(channel.server, message.author)
        if user_member.id.user == server.owner:
            user_rank = -sys.maxsize
        elif role := user_member.highest_role(server):
            user_rank = role.rank
        else:
            user_rank = sys.maxsize

        server_roles = {v.name: v for v in server.roles.values()}
        server_roles.update(server.roles)
        for match in ROLE_REGEX.finditer(text):
            role_name = match.groups()[0]
            if role := server_roles.get(role_name):
                if role.rank <= bot_rank:
                    await self.http.reply_to(message, f'I can only assign roles below my own!\n{role_name}', interactions=RESTRICT)
                    return
                elif role.rank <= user_rank:
                    await self.http.reply_to(message, f'You can only assign roles below your own!\n{role_name}', interactions=RESTRICT)
                    return
            else:
                await self.http.reply_to(message, f'Role not found!\n{role_name}', interactions=RESTRICT)
                return

        interactions = volty.Interactions(reactions=[CHECKMARK_UNICODE])
        response = await self.http.reply_to(message, text, interactions=interactions)
        self._setup_messages[response.id] = SetupMessage(message.author, channel.server, response.content)

    async def on_setup_react(self, event: Union[volty.events.MessageReact, volty.events.MessageUnreact]):
        setup_message = self._setup_messages[event.id]
        if event.user_id != setup_message.owner_id:
            return
        message = await self.http.fetch_message(event.channel_id, event.id)
        server = self.cache.servers[setup_message.server_id]
        server_roles = {v.name: (k, v.name) for k, v in server.roles.items()}
        server_roles.update({k: (k, v.name) for k, v in server.roles.items()})

        emojis = []
        raw_emojis = []
        is_checkmarked = False
        for emoji, user_ids in message.reactions.items():
            if setup_message.owner_id in user_ids:
                if emoji == CHECKMARK_UNICODE:
                    is_checkmarked = True
                else:
                    raw_emojis.append(emoji)
                    if emoji_lib.is_emoji(emoji):
                        emoji_id = get_emoji_text(emoji)
                    else:
                        emoji_id = emoji
                    emojis.append(emoji_id)

        content = setup_message.with_emojis(emojis, server_roles)
        content = content[:2_000]

        if is_checkmarked and len(emojis) == len(setup_message.matches):
            reaction_roles_message = ReactionRolesMessage(content)
            channel = await self.http.fetch_channel(message.channel)
            server = self.cache.servers[channel.server]
            member = await self.http.fetch_member(channel.server, setup_message.owner_id)
            if server.owner != member.id.user:
                highest_role = member.highest_role(server)
                if highest_role is None:
                    raise ValueError('Setup: user has no roles')
                for role_id in reaction_roles_message.emoji_roles.values():
                    if role := server.roles.get(role_id):
                        if role.rank <= highest_role.rank:
                            raise ValueError('Setup: role above user\'s')

            interactions = volty.Interactions(reactions=[f'{e}' for e in raw_emojis], restrict_reactions=True)
            response = await self.http.send_message(message.channel, content, interactions=interactions)
            await self.http.delete_message(message.channel, message.id)
            self._reaction_messages[response.id] = reaction_roles_message
        else:
            await self.http.edit_message(message.channel, message.id, content)

    async def on_role_react(self, event: Union[volty.events.MessageReact, volty.events.MessageUnreact]):
        reaction_message = self._reaction_messages[event.id]
        if not reaction_message.emoji_roles:
            return
        if emoji_lib.is_emoji(event.emoji_id):
            emoji_id = get_emoji_text(event.emoji_id)
        else:
            emoji_id = event.emoji_id
        if role_id := reaction_message.emoji_roles.get(emoji_id):
            channel = await self.http.fetch_channel(event.channel_id, fail_on_ratelimit=False)
            server = self.cache.servers[channel.server]
            if role_id not in server.roles:
                print('ERROR: react: role doesn\'t exist')
                return
            role = server.roles[role_id]

            bot_permissions = await self.fetch_server_permissions(server.id, self.cache.user_id)
            if not bot_permissions.has(volty.Permission.AssignRoles):
                print('ERROR: react: bot doesn\'t have AssignRoles permission')
                return
            bot_member = await self.http.fetch_member(server.id, self.cache.user_id, fail_on_ratelimit=False)
            bot_highest_role = bot_member.highest_role(server)
            if not bot_highest_role:
                print('ERROR: react: bot doesn\'t have any roles')
                return
            if role.rank <= bot_highest_role.rank:
                print('ERROR: react: role\'s rank is above bot\'s')
                return

            user_member = await self.http.fetch_member(server.id, event.user_id, fail_on_ratelimit=False)
            user_highest_role = user_member.highest_role(server)
            if user_highest_role is not None and user_highest_role.rank <= bot_highest_role.rank:
                print('ERROR: react: member\'s rank is above bot\'s')
                return

            member_roles = user_member.roles.copy()
            if event.type == 'MessageReact':
                if role_id not in member_roles:
                    member_roles.append(role_id)
                    print(f'GIVING: user:{event.user_id} role:{role_id} server:{server.id}')
                    await self.http.edit_member(server.id, event.user_id, roles=member_roles, fail_on_ratelimit=False)
            else:
                if role_id in member_roles:
                    member_roles.remove(role_id)
                    print(f'REMOVING: user:{event.user_id} role:{role_id} server:{server.id}')
                    await self.http.edit_member(server.id, event.user_id, roles=member_roles, fail_on_ratelimit=False)


async def main():
    load_dotenv()
    token = os.getenv('REVOLT_TOKEN')
    client = Bot(token)
    await client.run()


if __name__ == '__main__':
    asyncio.run(main())
