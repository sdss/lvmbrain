#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-03-26
# @Filename: overwatcher.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import dataclasses
import pathlib
from copy import copy
from time import time

from typing import cast

from sdsstools import Configuration
from sdsstools.utils import GatheringTaskGroup

from gort.core import LogNamespace
from gort.exceptions import GortError
from gort.gort import Gort
from gort.overwatcher.core import OverwatcherBaseTask, OverwatcherModule
from gort.overwatcher.helpers import DomeHelper
from gort.overwatcher.notifier import NotifierMixIn


@dataclasses.dataclass
class OverwatcherState:
    """Dataclass with the overwatcher state values."""

    running: bool = False
    enabled: bool = False
    observing: bool = False
    calibrating: bool = False
    night: bool = False
    safe: bool = False
    allow_dome_calibrations: bool = True
    dry_run: bool = False


class OverwatcherTask(OverwatcherBaseTask):
    """Overwatcher task that is aware of the overwatcher instance."""

    def __init__(self, overwatcher: Overwatcher):
        super().__init__()

        self.overwatcher = overwatcher
        self.log = self.overwatcher.log


class OverwatcherMainTask(OverwatcherTask):
    """The main overwatcher task."""

    name = "overwatcher_task"
    keep_alive = True
    restart_on_error = True

    def __init__(self, overwatcher: Overwatcher):
        super().__init__(overwatcher)

        self._lock = asyncio.Lock()

        self.previous_state = OverwatcherState()
        self._pending_close_dome: bool = False

    async def task(self):
        """Main overwatcher task."""

        await asyncio.sleep(1)

        ow = self.overwatcher

        while True:
            self.previous_state = copy(ow.state)

            try:
                is_safe = ow.alerts.is_safe()
                is_night = ow.ephemeris.is_night()

                ow.state.night = is_night
                ow.state.safe = is_safe
                ow.state.observing = ow.observer.is_observing

                running_calibration = ow.calibrations.get_running_calibration()
                ow.state.calibrating = running_calibration is not None

                async with self._lock:
                    if not is_safe:
                        await self.handle_unsafe()
                    elif not is_night:
                        await self.handle_daytime()
                    elif not ow.state.enabled:
                        await self.handle_disabled()
                    elif ow.observer.is_cancelling and ow.state.enabled:
                        await self.handle_reenable()

            except Exception as err:
                await ow.notify(
                    f"Error in main overwatcher task: {err!r}",
                    level="error",
                )

                # Avoid rapid fire errors. Sleep a bit longer before trying again.
                await asyncio.sleep(30)

            await asyncio.sleep(5)

    async def handle_unsafe(self):
        """Closes the dome if the conditions are unsafe."""

        closed = await self.overwatcher.dome.is_closing()

        observing = self.overwatcher.observer.is_observing
        cancelling = self.overwatcher.observer.is_cancelling

        # TODO: cancel calibrations if any are running.
        # TODO: should this only happen if the overwatcher is enabled?

        if not closed or observing:
            await self.overwatcher.notify(
                "Closing the dome due to unsafe conditions.",
                level="warning",
            )

            if observing and not cancelling:
                try:
                    await self.overwatcher.observer.stop_observing(
                        immediate=True,
                        reason="unsafe conditions",
                    )
                except Exception as err:
                    await self.overwatcher.notify(
                        f"Error stopping observing: {err!r}",
                        level="error",
                    )
                    await self.overwatcher.notify(
                        "I will close the dome anyway.",
                        level="warning",
                    )

            if not closed:
                await self.overwatcher.dome.shutdown(retry=True)

    async def handle_daytime(self):
        """Handles daytime."""

        # Don't do anything if we are calibrating. If a calibration script opened the
        # dome it should close it afterwards.
        if self.overwatcher.state.calibrating:
            return

        # Also don't do anything if the overwatcher is not enabled.
        if not self.overwatcher.state.enabled:
            return

        # Only close the dome if we have changed from night to day. handle_unsafe()
        # will close it if it's really unsafe.
        if not self.previous_state.night and not self.overwatcher.state.night:
            self._pending_close_dome = True

        observing = self.overwatcher.observer.is_observing
        cancelling = self.overwatcher.observer.is_cancelling

        if observing and not cancelling:
            # Decide whether to complete the current exposure or stop immediately.
            exposure_finishes_at = self.overwatcher.observer.next_exposure_completes
            now = time()

            if exposure_finishes_at > 0 and (exposure_finishes_at - now) < 300:
                immediate = False
            else:
                immediate = True

            await self.overwatcher.observer.stop_observing(
                immediate=immediate,
                reason="daytime conditions",
            )

            if not immediate:
                return

        if not observing and self._pending_close_dome:
            closed = await self.overwatcher.dome.is_closing()
            if not closed:
                await self.overwatcher.notify("Daytime conditions. Closing the dome.")
                await self.overwatcher.dome.shutdown(retry=True)
            self._pending_close_dome = False

    async def handle_disabled(self):
        """Handles the disabled state."""

        observing = self.overwatcher.observer.is_observing
        cancelling = self.overwatcher.observer.is_cancelling

        if observing and not cancelling:
            # Disable after this tile.
            await self.overwatcher.observer.stop_observing(
                immediate=False,
                reason="overwatcher was disabled",
            )

    async def handle_reenable(self):
        """Re-enables the overwatcher after cancelling."""

        observer = self.overwatcher.observer
        if not self.overwatcher.state.enabled or not observer.is_cancelling:
            return

        self.log.info("Undoing the cancellation of the observing loop.")
        observer._cancelling = False
        self.overwatcher.gort.observer.cancelling = False


class OverwatcherPingTask(OverwatcherTask):
    """Emits a ping notification every five minutes."""

    name = "overwatcher_ping"
    keep_alive = True
    restart_on_error = True

    delay: float = 900

    async def task(self):
        """Ping task."""

        while True:
            await asyncio.sleep(self.delay)
            await self.overwatcher.notify("I am alive!", log=False)


class Overwatcher(NotifierMixIn):
    """Monitors the observatory."""

    instance: Overwatcher | None = None

    def __new__(cls, *args, **kwargs):
        if not cls.instance:
            cls.instance = super(Overwatcher, cls).__new__(cls)
        return cls.instance

    def __init__(
        self,
        gort: Gort | None = None,
        verbosity: str = "debug",
        calibrations_file: str | pathlib.Path | None = None,
        dry_run: bool = False,
        **kwargs,
    ):
        from gort.overwatcher import (
            AlertsOverwatcher,
            CalibrationsOverwatcher,
            EphemerisOverwatcher,
            EventsOverwatcher,
            ObserverOverwatcher,
        )

        # Check if the instance already exists, in which case do nothing.
        if hasattr(self, "gort"):
            return

        self.gort = gort or Gort(verbosity=verbosity, **kwargs)
        self.config = cast(Configuration, self.gort.config)
        self.log = LogNamespace(self.gort.log, header=f"({self.__class__.__name__}) ")

        self.state = OverwatcherState()
        self.state.dry_run = dry_run

        self.dome = DomeHelper(self)

        self.tasks: list[OverwatcherTask] = [
            OverwatcherMainTask(self),
            OverwatcherPingTask(self),
        ]

        self.ephemeris = EphemerisOverwatcher(self)
        self.calibrations = CalibrationsOverwatcher(self, calibrations_file)
        self.observer = ObserverOverwatcher(self)
        self.alerts = AlertsOverwatcher(self)
        self.events = EventsOverwatcher(self)

    async def run(self):
        """Starts the overwatcher tasks."""

        if self.state.running:
            raise GortError("Overwatcher is already running.")

        if not self.gort.is_connected():
            await self.gort.init()

        async with GatheringTaskGroup() as group:
            for module in OverwatcherModule.instances:
                self.log.info(f"Starting overwatcher module {module.name!r}")
                group.create_task(module.run())

        async with GatheringTaskGroup() as group:
            for task in self.tasks:
                group.create_task(task.run())

        self.state.running = True
        await self.notify(
            "Overwatcher is starting.",
            payload={"dry-run": self.state.dry_run},
        )

        if self.state.dry_run:
            self.log.warning("Overatcher is running in dry-mode.")

        return self

    async def shutdown(self, reason: str = "undefined", retry: bool = True):
        """Shuts down the observatory."""

        # Check if the dome is already closed, then do nothing.
        if await self.dome.is_closing():
            return

        if not reason.endswith("."):
            reason += "."

        await self.notify(f"Triggering shutdown. Reason: {reason}", level="warning")

        if not self.state.dry_run:
            stop = asyncio.create_task(self.observer.stop_observing(immediate=True))
            shutdown = asyncio.create_task(self.dome.shutdown(retry=retry))
        else:
            self.log.warning("Dry run enabled. Not shutting down.")
            return

        try:
            await asyncio.gather(stop, shutdown)
        except Exception as err:
            await self.notify(
                f"Error during shutdown: {err!r}",
                level="error",
                error=err,
            )

    async def cancel(self):
        """Cancels the overwatcher tasks."""

        for module in OverwatcherModule.instances:
            self.gort.log.debug(f"Cancelling overwatcher module {module.name!r}")
            await module.cancel()

        for task in self.tasks:
            await task.cancel()

        self.state.running = False
