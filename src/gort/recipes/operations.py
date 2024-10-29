#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-08-13
# @Filename: operations.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from rich.prompt import Confirm

from sdsstools.utils import GatheringTaskGroup

from .base import BaseRecipe


if TYPE_CHECKING:
    from gort.devices.spec import Spectrograph


__all__ = ["StartupRecipe", "ShutdownRecipe", "CleanupRecipe"]


OPEN_DOME_MESSAGE = """Do not open the dome if you have not checked the following:
* Humidity is below 80%
* Dew point is below the temperature by > 5 degrees (?)
* Wind is below 35 mph
* There is no-one inside the enclosure
* No rain/good conditions confirmed with the Du Pont observers

Du Pont control room:
   (US) +1 626-310-0436
   (Chile) +56 51-2203-609
Slack:
   #lvm-dupont-observing
"""

SHUTDOWN_MESSAGE = """The shutdown recipe has completed.

Please confirm that the dome is closed and the telescopes
are parked by turning on the dome lights with

await g.enclosure.lights.telescope_bright.on()

and checking webcam LVM-TEL06. Then turn off the lights with

await g.enclosure.lights.telescope_bright.off()

If the dome is not closed, please run

await g.enclosure.close(force=True)

If that does not work, please contact the Du Pont observers.

Du Pont control room:
   (US) +1 626-310-0436
   (Chile) +56 51-2203-609
Slack:
   #lvm-dupont-observing
"""


class StartupRecipe(BaseRecipe):
    """Starts the telescopes, runs the calibration sequence, and opens the enclosure."""

    name = "startup"

    async def recipe(
        self,
        open_enclosure: bool = True,
        confirm_open: bool = True,
        focus: bool = True,
    ):
        """Runs the startup sequence.

        Parameters
        ----------
        gort
            The `.Gort` instance to use.
        open_enclosure
            Whether to open the enclosure.
        confirm_open
            If :obj:`True`, asks the user to confirm opening the enclosure.
        focus
            Whether to focus after the enclosure has open.

        """

        self.gort.log.warning("Running the startup sequence.")

        await self.gort.telescopes.home(
            home_telescopes=True,
            home_kms=True,
            home_focusers=True,
            home_fibsel=True,
        )

        self.gort.log.info("Turning off all calibration lamps and dome lights.")
        await self.gort.nps.calib.all_off()
        await self.gort.enclosure.lights.dome_all_off()
        await self.gort.enclosure.lights.spectrograph_room.off()

        self.gort.log.info("Reconnecting AG cameras.")
        await self.gort.ags.reconnect()

        self.gort.log.info("Taking AG darks.")
        await self.gort.guiders.take_darks()

        if open_enclosure:
            if confirm_open:
                self.gort.log.warning(OPEN_DOME_MESSAGE)
                if not Confirm.ask(
                    "Open the dome?",
                    default=False,
                    console=self.gort._console,
                ):
                    return

            self.gort.log.info("Opening the dome ...")
            await self.gort.enclosure.open()

        if open_enclosure and focus:
            self.gort.log.info("Focusing telescopes.")
            await self.gort.guiders.focus()

        self.gort.log.info("The startup recipe has completed.")


class ShutdownRecipe(BaseRecipe):
    """Closes the telescope for the night."""

    name = "shutdown"

    async def recipe(
        self,
        park_telescopes: bool = True,
        additional_close: bool = False,
    ):
        """Shutdown the telescope, closes the dome, etc.

        Parameters
        ----------
        park_telescopes
            Park telescopes (and disables axes). Set to :obj:`False` if only
            closing for a brief period of time. If the dome fails to close with
            ``park_telescopes=True``, it will try again without parking the
            telescopes.
        additional_close
            Issues an additional ``close`` command after the dome is closed.
            This is a temporary solution to make sure the dome is closed
            while we investigate the issue with the dome not fully closing
            sometimes.

        """

        self.gort.log.warning("Running the shutdown sequence.")

        async with GatheringTaskGroup() as group:
            self.gort.log.info("Turning off all lamps.")
            group.create_task(self.gort.nps.calib.all_off())

            self.gort.log.info("Making sure guiders are idle.")
            group.create_task(self.gort.guiders.stop())

            self.gort.log.info("Closing the dome.")
            group.create_task(self.gort.enclosure.close())

        if park_telescopes:
            self.gort.log.info("Parking telescopes for the night.")
            await self.gort.telescopes.park()

        if additional_close:
            self.gort.log.info("Closing the dome again.")
            await asyncio.sleep(5)
            await self.gort.enclosure.close(force=True)

        self.gort.log.warning(SHUTDOWN_MESSAGE)


class CleanupRecipe(BaseRecipe):
    """Stops guiders, aborts exposures, and makes sure the system is ready to go."""

    name = "cleanup"

    async def recipe(self, readout: bool = True, turn_lamps_off: bool = True):
        """Runs the cleanup recipe.

        Parameters
        ----------
        readout
            If the spectrographs are idle and with a readout pending,
            reads the spectrographs.
        turn_lamps_off
            If :obj:`True`, turns off the dome lights and calibration lamps.

        """

        self.gort.log.info("Stopping the guiders.")
        await self.gort.guiders.stop()

        if not (await self.gort.specs.are_idle()):
            cotasks = []

            for spec in self.gort.specs.values():
                status = await spec.status()
                names = status["status_names"]

                if await spec.is_reading():
                    self.gort.log.warning(f"{spec.name} is reading. Waiting.")
                    cotasks.append(self._wait_until_spec_is_idle(spec))
                elif await spec.is_exposing():
                    self.gort.log.warning(f"{spec.name} is exposing. Aborting.")
                    cotasks.append(spec.abort())
                elif "IDLE" in names and "READOUT_PENDING" in names:
                    msg = f"{spec.name} has a pending exposure."
                    if readout is False:
                        self.gort.log.warning(f"{msg} Aborting it.")
                        cotasks.append(spec.abort())
                    else:
                        self.gort.log.warning(f"{msg} Reading it.")
                        cotasks.append(spec.actor.commands.read())
                        cotasks.append(self._wait_until_spec_is_idle(spec))

            try:
                await asyncio.gather(*cotasks)
            except Exception as ee:
                self.gort.log.error(f"Error during cleanup: {ee}")
                self.gort.log.warning("Resetting the spectrographs.")

        await self.gort.specs.reset(full=True)

        if turn_lamps_off:
            self.gort.log.info("Turning off all calibration lamps and dome lights.")
            await self.gort.nps.calib.all_off()
            await self.gort.enclosure.lights.dome_all_off()

        # Turn off lights in the dome.
        await asyncio.gather(
            self.gort.enclosure.lights.telescope_red.off(),
            self.gort.enclosure.lights.telescope_bright.off(),
        )

    async def _wait_until_spec_is_idle(self, spec: Spectrograph):
        """Waits until an spectrograph is idle."""

        while True:
            if await spec.is_idle():
                return

            await asyncio.sleep(3)
