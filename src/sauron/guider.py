#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-06-18
# @Filename: nps.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from sauron import log
from sauron.core import SauronDevice, SauronDeviceSet
from sauron.exceptions import SauronError


if TYPE_CHECKING:
    from sauron.sauron import Sauron


class Guider(SauronDevice):
    """Class representing a guider."""

    def __init__(self, sauron: Sauron, name: str, actor: str, **kwargs):
        super().__init__(sauron, name, actor)

    def print_reply(self, reply):
        """Outputs command replies."""

        if reply.body:
            log.debug(f"{self.actor.name}: {reply.body}")

    async def focus(self):
        """Focus the telescope."""

        try:
            await self.actor.commands.focus(reply_callback=self.print_reply)
        except SauronError as err:
            log.error(f"{self.actor.name}: failed focusing with error {err}")


class GuiderSet(SauronDeviceSet[Guider]):
    """A set of telescope guiders."""

    __DEVICE_CLASS__ = Guider

    async def take_darks(self):
        """Takes AG darks."""

        # Move telescopes to park to prevent light, since we don't have shutters.
        # We use goto_named_position to prevent disabling the telescope and having
        # to rehome.
        log.debug("Moving telescopes to park position.")
        await self.sauron.telescopes.goto_named_position("park")

        # Take darks.
        log.debug("Taking darks.")

        cmds = []
        for ag in self.values():
            cmds.append(
                ag.actor.commands.guide.commands.expose(
                    flavour="dark",
                    reply_callback=ag.print_reply,
                )
            )

        if len(cmds) > 0:
            await asyncio.gather(*cmds)

    async def focus(self, inplace=False):
        """Focus all the telescopes."""

        # Send telescopes to zenith.
        if not inplace:
            await self.sauron.telescopes.goto_named_position("zenith")

        jobs = [ag.focus() for ag in self.values()]
        await asyncio.gather(*jobs)
