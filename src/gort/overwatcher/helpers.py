#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-10-27
# @Filename: helpers.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import enum

from typing import TYPE_CHECKING

from sdsstools.utils import GatheringTaskGroup

from gort.exceptions import GortError
from gort.overwatcher.notifier import BasicNotifier
from gort.tools import get_lvmapi_route


if TYPE_CHECKING:
    from gort import Gort
    from gort.overwatcher.overwatcher import Overwatcher


class DomeStatus(enum.Flag):
    """An enumeration of dome statuses."""

    OPEN = enum.auto()
    CLOSED = enum.auto()
    OPENING = enum.auto()
    CLOSING = enum.auto()
    MOVING = enum.auto()
    UNKNOWN = enum.auto()


class DomeHelper:
    """Handle dome movement."""

    def __init__(self, overwatcher: Overwatcher):
        self.overwatcher = overwatcher
        self.gort = overwatcher.gort
        self.log = overwatcher.log

        self._action_lock = asyncio.Lock()
        self._move_lock = asyncio.Lock()

    async def status(self):
        """Returns the status of the dome."""

        status = await self.gort.enclosure.status()
        labels = status["dome_status_labels"]

        if "MOTOR_OPENING" in labels:
            return DomeStatus.OPENING | DomeStatus.MOVING
        elif "MOTOR_CLOSING" in labels:
            return DomeStatus.CLOSING | DomeStatus.MOVING
        elif "OPEN" in labels:
            if "MOVING" in labels:
                return DomeStatus.OPENING | DomeStatus.MOVING
            return DomeStatus.OPEN
        elif "CLOSED" in labels:
            if "MOVING" in labels:
                return DomeStatus.CLOSING | DomeStatus.MOVING
            return DomeStatus.CLOSED

        return DomeStatus.UNKNOWN

    async def is_opening(self):
        """Returns True if the dome is open or opening."""

        status = await self.status()

        if status & DomeStatus.OPENING:
            return True
        if status & DomeStatus.OPEN:
            return True

        return False

    async def is_closing(self):
        """Returns True if the dome is closed or closed."""

        status = await self.status()

        if status & DomeStatus.CLOSING:
            return True
        if status & DomeStatus.CLOSED:
            return True

        return False

    async def _move(
        self,
        open: bool = False,
        park: bool = True,
        retry: bool = False,
    ):
        """Moves the dome."""

        try:
            await self.stop()

            is_local = await self.gort.enclosure.is_local()
            if is_local:
                raise GortError("Cannot move the dome in local mode.")

            if open:
                if not self.overwatcher.state.safe:
                    raise GortError("Cannot open the dome when conditions are unsafe.")

                await self.gort.enclosure.open(park_telescopes=park)
            else:
                await self.gort.enclosure.close(
                    park_telescopes=park,
                    retry_without_parking=retry,
                )

        except Exception:
            await asyncio.sleep(3)

            status = await self.status()

            if status & DomeStatus.MOVING:
                self.log.warning("Dome is still moving after an error. Stopping.")
                await self.stop()

            # Sometimes the open/close could fail but actually the dome is open/closed.
            if open and not (status & DomeStatus.OPEN):
                raise
            elif not open and not (status & DomeStatus.CLOSED):
                raise

    async def open(self, park: bool = True):
        """Opens the dome."""

        async with self._move_lock:
            current = await self.status()
            if current == DomeStatus.OPEN:
                self.log.debug("Dome is already open.")
                return

            if current == DomeStatus.OPENING:
                self.log.debug("Dome is already opening.")
                return

            if current == DomeStatus.UNKNOWN:
                self.log.warning("Dome is in an unknown status. Stopping and opening.")
                await self.stop()

            self.log.info("Opening the dome ...")
            await self._move(open=True, park=park)

    async def close(self, park: bool = True, retry: bool = False):
        """Closes the dome."""

        async with self._move_lock:
            current = await self.status()
            if current == DomeStatus.CLOSED:
                self.log.debug("Dome is already closed.")
                return

            if current == DomeStatus.CLOSING:
                self.log.debug("Dome is already closing.")
                return

            if current == DomeStatus.UNKNOWN:
                self.log.warning("Dome is in an unknown status. Stopping and closing.")
                await self.stop()

            self.log.info("Closing the dome ...")
            await self._move(
                open=False,
                park=park,
                retry=retry,
            )

    async def stop(self):
        """Stops the dome."""

        await self.gort.enclosure.stop()

        await asyncio.sleep(1)
        status = await self.status()

        if status & DomeStatus.MOVING:
            raise GortError("Dome is still moving after a stop command.")

    async def startup(self):
        """Runs the startup sequence."""

        async with self._action_lock:
            self.log.info("Starting the dome startup sequence.")
            await self.gort.startup(open_enclosure=False, focus=False)

            # Now we manually open. We do not focus here since that's handled
            # by the observer module.
            await self.open()

    async def shutdown(self, retry: bool = False, force: bool = False):
        """Runs the shutdown sequence."""

        is_closing = await self.is_closing()
        if is_closing and not force:
            return

        async with self._action_lock:
            self.log.info("Running the shutdown sequence.")

            async with GatheringTaskGroup() as group:
                self.log.info("Turning off all lamps.")
                group.create_task(self.gort.nps.calib.all_off())

                self.log.info("Making sure guiders are idle.")
                group.create_task(self.gort.guiders.stop())

                self.log.info("Closing the dome.")
                group.create_task(self.close(retry=retry))

            self.log.info("Parking telescopes for the night.")
            await self.gort.telescopes.park()


async def post_observing(gort: Gort):
    """Runs the post-observing tasks.

    These include:

    - Closing the dome.
    - Parking the telescopes.
    - Turning off all lamps.
    - Stopping the guiders.
    - Sending the night log email.

    """

    notifier = BasicNotifier(gort)

    await notifier.notify("Running post-observing tasks.")

    closed = await gort.enclosure.is_closed()
    if not closed:
        await gort.enclosure.close(retry_without_parking=True)

    await gort.telescopes.park()
    await gort.nps.calib.all_off()
    await gort.guiders.stop()

    notifier.log.info("Sending night log email.")
    result = await get_lvmapi_route("/logs/night-logs/0/email?only_if_not_sent=true")
    if not result:
        notifier.log.warning("Night log had already been sent.")
