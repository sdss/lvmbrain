#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-13
# @Filename: nps.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

from typing import TYPE_CHECKING

from gort.gort import GortDevice, GortDeviceSet


if TYPE_CHECKING:
    from gort import ActorReply
    from gort.gort import GortClient


__all__ = ["NPS", "NPSSet"]


class NPS(GortDevice):
    """Class representing a networked power switch."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

    async def status(self):
        """Retrieves the status of the power outlet."""

        reply: ActorReply = await self.actor.commands.status()
        return reply.flatten()["status"][self.name]

    async def on(self, outlet: str):
        """Turns an outlet on."""

        await self.actor.commands.on(outlet)

    async def off(self, outlet: str):
        """Turns an outlet on."""

        await self.actor.commands.off(outlet)

    async def all_off(self):
        """Turns off all the outlets."""

        for outlet_number in range(1, 9):
            self.write_to_log(f"Turning off outlet {outlet_number}.")
            await self.actor.commands.off("", outlet_number)


class NPSSet(GortDeviceSet[NPS]):
    """A set of networked power switches."""

    __DEVICE_CLASS__ = NPS
