#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2024-04-09
# @Filename: notifications.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from gort.maskbits import Event
from gort.overwatcher.core import OverwatcherModule, OverwatcherModuleTask
from gort.overwatcher.overwatcher import Overwatcher
from gort.pubsub import GortMessage, GortSubscriber
from gort.tools import insert_to_database


class MonitorNotifications(OverwatcherModuleTask["NotificationsOverwatcher"]):
    """Processes the notification queue."""

    name = "monitor_notifications"
    keep_alive = True
    restart_on_error = True

    _running_tasks: list[asyncio.Task] = []

    async def task(self):
        """Runs the task."""

        async for message in GortSubscriber().iterator(decode=True):
            # Clean done tasks.
            self._running_tasks = [t for t in self._running_tasks if not t.done()]

            task = asyncio.create_task(self.process(message))
            self._running_tasks.append(task)

    async def process(self, message: GortMessage):
        """Processes a notification"""

        message_type = message.message_type

        if message_type != "event":
            return

        event = Event(message.event or Event.UNCATEGORISED)
        event_name = event.name
        payload = message.payload

        try:
            self.write_to_db(event, payload)
        except Exception as ee:
            self.log.error(f"Failed to write event {event_name} to the database: {ee}")


    def write_to_db(self, event: Event, payload: dict):
        """Writes the event to the database."""

        dt = datetime.now(tz=UTC)

        insert_to_database(
            self.gort.config["overwatcher"]["events_table"],
            [{"date": dt, "event": event.name.upper(), "payload": json.dumps(payload)}],
        )


class NotificationsOverwatcher(OverwatcherModule):
    name = "notifications"

    tasks = [MonitorNotifications()]

    def __init__(self, overwatcher: Overwatcher):
        super().__init__(overwatcher)

        self.queue = asyncio.Queue()
