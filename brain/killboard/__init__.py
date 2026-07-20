"""Albion Online killboard module for the "Dead" Discord bot.

This is an add-on built on the ``dead/`` bot kernel, not part of CORTANA's
voice/fleet-intel core. It polls the Albion Online gameinfo API, renders
kill/death cards, tracks rankings and battles, and posts them to Discord.

It is **off by default**: the module only runs when the operator enables it in
config (``cfg.killboard.*``). It owns its own SQLite database
(``cfg.killboard.storage.db_path``) with its own migrations under
``brain/killboard/migrations`` and never touches CORTANA's connection or
tables. Like every other surface in this bot, it sends Discord messages with
``allowed_mentions=discord.AllowedMentions.none()`` — the killboard is purely
informational and never pings anyone.

No public exports; see the submodules for the module implementation.
"""

from __future__ import annotations
