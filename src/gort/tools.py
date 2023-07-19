#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-10
# @Filename: tools.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import pathlib
import re
import warnings
from contextlib import suppress
from functools import partial

from typing import TYPE_CHECKING, Callable, Coroutine

import httpx
import peewee
from astropy import units as uu
from astropy.coordinates import angular_separation as astropy_angular_separation
from rich.console import Console
from rich.logging import RichHandler
from rich.text import Text
from rich.theme import Theme

from gort import config


if TYPE_CHECKING:
    from pyds9 import DS9

    from clu import AMQPClient, AMQPReply

    from gort.devices.telescope import FibSel
    from gort.gort import GortClient


__all__ = [
    "get_valid_variable_name",
    "ds9_agcam_monitor",
    "parse_agcam_filename",
    "ds9_display_frames",
    "get_next_tile_id",
    "get_calibrators",
    "get_next_tile_id_sync",
    "get_calibrators_sync",
    "register_observation",
    "tqdm_timer",
    "get_ccd_frame_path",
    "move_mask_interval",
    "angular_separation",
    "get_db_connection",
    "run_in_executor",
    "is_interactive",
    "is_notebook",
    "cancel_task",
]

CAMERAS = [
    "sci.west",
    "sci.east",
    "skye.west",
    "skye.east",
    "skyw.west",
    "skyw.east",
    "spec.east",
]


def get_valid_variable_name(var_name: str):
    """Converts a string to a valid variable name."""

    return re.sub(r"\W|^(?=\d)", "_", var_name)


async def ds9_agcam_monitor(
    amqp_client: AMQPClient,
    cameras: list[str] | None = None,
    replace_path_prefix: tuple[str, str] | None = None,
    **kwargs,
):
    """Shows guider images in DS9."""

    images_handled = set([])

    # Clear all frames and get an instance of DS9.
    ds9 = await ds9_display_frames([], clear_frames=True, preserve_frames=False)

    if cameras is None:
        cameras = CAMERAS.copy()

    agcam_actors = set(
        [
            "lvm." + (cam.split(".")[0] if "." in cam else cam) + ".agcam"
            for cam in cameras
        ]
    )

    async def handle_reply(reply: AMQPReply):
        sender = reply.sender
        if sender not in agcam_actors:
            return

        message: dict | None = None
        if "east" in reply.body:
            message = reply.body["east"]
        elif "west" in reply.body:
            message = reply.body["west"]
        else:
            return

        if message is None or message.get("state", None) != "written":
            return

        filename: str = message["filename"]
        if filename in images_handled:
            return
        images_handled.add(filename)

        if replace_path_prefix is not None:
            filename = filename.replace(replace_path_prefix[0], replace_path_prefix[1])

        await ds9_display_frames([filename], ds9=ds9, **kwargs)

    amqp_client.add_reply_callback(handle_reply)

    while True:
        await asyncio.sleep(1)


async def ds9_display_frames(
    files: list[str | pathlib.Path] | dict[str, str | pathlib.Path],
    ds9: DS9 | None = None,
    order=CAMERAS,
    ds9_target: str = "DS9:*",
    show_all_frames=True,
    preserve_frames=True,
    clear_frames=False,
    adjust_zoom=True,
    adjust_scale=True,
    show_tiles=True,
):
    """Displays a series of images in DS9."""

    if ds9 is None:
        try:
            import pyds9
        except ImportError:
            raise ImportError("pyds9 is not installed.")

        ds9 = pyds9.DS9(target=ds9_target)

    if clear_frames:
        ds9.set("frame delete all")

    files_dict: dict[str, str] = {}
    if not isinstance(files, dict):
        for file_ in files:
            tel_cam = parse_agcam_filename(file_)
            if tel_cam is None:
                raise ValueError(f"Cannot parse type of file {file_!s}.")
            files_dict[".".join(tel_cam)] = str(file_)
    else:
        files_dict = {k: str(v) for k, v in files.items()}

    nframe = 1
    for cam in order:
        if cam in files_dict:
            file_ = files_dict[cam]
            ds9.set(f"frame {nframe}")
            ds9.set(f"fits {file_}")
            if adjust_scale:
                ds9.set("zscale")
            if adjust_zoom:
                ds9.set("zoom to fit")
            nframe += 1
        else:
            if show_all_frames:
                if preserve_frames is False:
                    ds9.set(f"frame {nframe}")
                    ds9.set("frame clear")
                nframe += 1

    if show_tiles:
        ds9.set("tile")

    return ds9


def parse_agcam_filename(file_: str | pathlib.Path) -> tuple[str, str] | None:
    """Returns the type of an ``agcam`` file in the form ``(telescope, camera)``."""

    file_ = pathlib.Path(file_)
    basename = file_.name

    match = re.match(".+(sci|spec|skyw|skye).+(east|west)", basename)
    if not match:
        return None

    return match.groups()


def get_next_tile_id_sync() -> dict:
    """Retrieves the next ``tile_id`` from the scheduler API. Synchronous version."""

    sch_config = config["scheduler"]
    host = sch_config["host"]
    port = sch_config["port"]

    with httpx.Client(base_url=f"http://{host}:{port}/") as client:
        resp = client.get("next_tile")
        if resp.status_code != 200:
            raise httpx.RequestError("Failed request to /next_tile")
        tile_id_data = resp.json()

    return tile_id_data


async def get_next_tile_id() -> dict:
    """Retrieves the next ``tile_id`` from the scheduler API."""

    sch_config = config["scheduler"]
    host = sch_config["host"]
    port = sch_config["port"]

    async with httpx.AsyncClient(base_url=f"http://{host}:{port}/") as client:
        resp = await client.get("next_tile")
        if resp.status_code != 200:
            raise httpx.RequestError("Failed request to /next_tile")
        tile_id_data = resp.json()

    return tile_id_data


def get_calibrators_sync(
    tile_id: int | None = None,
    ra: float | None = None,
    dec: float | None = None,
):
    """Get calibrators for a ``tile_id`` or science pointing. Synchronous version."""

    sch_config = config["scheduler"]
    host = sch_config["host"]
    port = sch_config["port"]

    with httpx.Client(base_url=f"http://{host}:{port}/") as client:
        if tile_id:
            resp = client.get("cals", params={"tile_id": tile_id})
        elif ra is not None and dec is not None:
            resp = client.get("cals", params={"ra": ra, "dec": dec})
        else:
            raise ValueError("ra and dec are required.")
        if resp.status_code != 200:
            raise httpx.RequestError("Failed request to /cals")

    return resp.json()


async def get_calibrators(
    tile_id: int | None = None,
    ra: float | None = None,
    dec: float | None = None,
):
    """Get calibrators for a ``tile_id`` or science pointing."""

    sch_config = config["scheduler"]
    host = sch_config["host"]
    port = sch_config["port"]

    async with httpx.AsyncClient(base_url=f"http://{host}:{port}/") as client:
        if tile_id:
            resp = await client.get("cals", params={"tile_id": tile_id})
        elif ra is not None and dec is not None:
            resp = await client.get("cals", params={"ra": ra, "dec": dec})
        else:
            raise ValueError("ra and dec are required.")
        if resp.status_code != 200:
            raise httpx.RequestError("Failed request to /cals")

    return resp.json()


async def register_observation(payload: dict):
    """Registers an observation with the scheduler."""

    sch_config = config["scheduler"]
    host = sch_config["host"]
    port = sch_config["port"]

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"http://{host}:{port}/register_observation",
            json=payload,
            follow_redirects=True,
        )

        if resp.status_code != 200 or not resp.json()["success"]:
            raise RuntimeError("Failed registering observation.")


def is_notebook() -> bool:
    """Returns `True` if the code is run inside a Jupyter Notebook.

    https://stackoverflow.com/questions/15411967/how-can-i-check-if-code-is-executed-in-the-ipython-notebook

    """

    try:
        shell = get_ipython().__class__.__name__  # type: ignore
        if shell == "ZMQInteractiveShell":
            return True  # Jupyter notebook or qtconsole
        elif shell == "TerminalInteractiveShell":
            return False  # Terminal running IPython
        else:
            return False  # Other type (?)
    except NameError:
        return False  # Probably standard Python interpreter


def is_interactive():
    """Returns `True` is we are in an interactive session."""

    import __main__ as main

    return not hasattr(main, "__file__")


def tqdm_timer(seconds: float):
    """Creates a task qith a tqdm progress bar."""

    if is_notebook():
        from tqdm.notebook import tqdm
    else:
        from tqdm import tqdm

    bar_format = "{l_bar}{bar}| {n_fmt}/{total_fmt}s"

    async def _progress():
        for _ in tqdm(range(int(seconds)), bar_format=bar_format):
            await asyncio.sleep(1)

    return asyncio.create_task(_progress())


def get_ccd_frame_path(
    frame_id: int,
    sjd: int | None = None,
    cameras: str | list[str] | None = None,
    spectro_path="/data/spectro",
) -> list[str]:
    """Returns the paths for the files for a spectrograph frame.

    Parameters
    ----------
    frame_id
        The spectrograph frame for which the paths are searched.
    mjd
        The SJD in which the frames where taken. If not provided, all the
        directories under ``spectro_path`` are searched.
    cameras
        The cameras to be returned. If `None`, all cameras found are returned.
    spectro_path
        The path to the ``spectro`` directory where spectrograph files are
        stored under an SJD structure.

    Returns
    -------
    paths
        The list of paths to CCD frames that match ``frame_id``.

    """

    if isinstance(cameras, str):
        cameras = [cameras]

    base_path = pathlib.Path(spectro_path)
    recursive = True
    if sjd:
        base_path /= str(sjd)
        recursive = False

    # Glob all files that match the frame_id.
    globp = f"*{frame_id}.fits.*"
    if recursive:
        globp = f"**/{globp}"

    files = [str(path) for path in base_path.glob(globp)]

    if cameras is None:
        return files

    files_camera = []
    for camera in cameras:
        for file in files:
            if f"-{camera}-" in file:
                files_camera.append(file)

    return files_camera


async def move_mask_interval(
    gort: GortClient,
    positions: str | list[str] = "P1-*",
    order_by_steps: bool = False,
    total_time: float | None = None,
    time_per_position: float | None = None,
    notifier: Callable[[str], None] | Callable[[str], Coroutine] | None = None,
):
    """Moves the fibre mask in the spectrophotometric telescope at intervals.

    Parameters
    ----------
    gort
        The instance of `.Gort` to communicate with the actor system.
    positions
        The positions to iterate over. It can be a string in which case it will
        be treated as a regular expression and any mask position that matches the
        value will be iterated, in alphabetic order. Alternative it can be a list
        of positions to move to which will be executed in that order.
    order_by_steps
        If `True`, the positions are iterated in order of smaller to larger
        number of step motors.
    total_time
        The total time to spend iterating over positions, in seconds. Each position
        will  be visited for an equal amount of time. The time required to move the
        mask will not be taken into account, which means the total execution
        time will be longer than ``total_time``.
    time_per_position
        The time to spend on each mask position, in seconds. The total execution
        time will be ``len(positions)*total_time+overhead`` where ``overhead`` is
        the time required to move the mask between positions.
    notifier
        A function or coroutine to call every time a new position is reached.
        If it's a coroutine, it is scheduled as a task. If it is a normal
        callback it should run quickly to not perceptibly affect the total
        execution time.

    """

    try:
        fibsel: FibSel = gort.telescopes.spec.fibsel
    except Exception as err:
        raise RuntimeError(f"Cannot find fibre selector: {err}")

    if total_time is not None and time_per_position is not None:
        raise ValueError("Only one of total_time or time_per_position can be used.")

    if total_time is None and time_per_position is None:
        raise ValueError("One of total_time or time_per_position needs to be passed.")

    mask_config = config["telescopes"]["mask_positions"]
    all_positions = list(mask_config)

    if isinstance(positions, str):
        regex = positions
        all_positions = fibsel.list_positions()
        positions = [pos for pos in all_positions if re.match(regex, pos)]

        if order_by_steps:
            positions = sorted(positions, key=lambda p: mask_config[p])

    fibsel.write_to_log(f"Iterating over positions {positions}.")

    if total_time:
        time_per_position = total_time / len(positions)

    assert time_per_position is not None

    for position in positions:
        await fibsel.move_to_position(position)

        # Notify.
        if notifier is not None:
            if asyncio.iscoroutinefunction(notifier):
                asyncio.create_task(notifier(position))
            else:
                notifier(position)

        await asyncio.sleep(time_per_position)


def angular_separation(lon1: float, lat1: float, lon2: float, lat2: float):
    """A wrapper around astropy's ``angular_separation``.

    Returns the separation between two sets of coordinates. All units must
    be degrees and the returned values is also the separation in degrees.

    """

    separation = astropy_angular_separation(
        lon1 * uu.degree,  # type: ignore
        lat1 * uu.degree,  # type: ignore
        lon2 * uu.degree,  # type: ignore
        lat2 * uu.degree,  # type: ignore
    )

    return separation.to("deg").value


def get_db_connection():
    """Returns a DB connection from the configuration file parameters."""

    conn = peewee.PostgresqlDatabase(**config["database"])
    assert conn.connect(), "Database connection failed."

    return conn


async def run_in_executor(fn, *args, catch_warnings=False, executor="thread", **kwargs):
    """Runs a function in an executor.

    In addition to streamlining the use of the executor, this function
    catches any warning issued during the execution and reissues them
    after the executor is done. This is important when using the
    actor log handler since inside the executor there is no loop that
    CLU can use to output the warnings.

    In general, note that the function must not try to do anything with
    the actor since they run on different loops.

    """

    fn = partial(fn, *args, **kwargs)

    if executor == "thread":
        executor = concurrent.futures.ThreadPoolExecutor
    elif executor == "process":
        executor = concurrent.futures.ProcessPoolExecutor
    else:
        raise ValueError("Invalid executor name.")

    if catch_warnings:
        with warnings.catch_warnings(record=True) as records:
            with executor() as pool:
                result = await asyncio.get_event_loop().run_in_executor(pool, fn)

        for ww in records:
            warnings.warn(ww.message, ww.category)

    else:
        with executor() as pool:
            result = await asyncio.get_running_loop().run_in_executor(pool, fn)

    return result


class CustomRichHandler(RichHandler):
    """A slightly custom ``RichHandler`` logging handler."""

    def get_level_text(self, record):
        """Get the level name from the record."""

        level_name = record.levelname
        level_text = Text.styled(
            f"[{level_name}]".ljust(9),
            f"logging.level.{level_name.lower()}",
        )
        return level_text


def get_rich_logger(verbosity_level: int = logging.WARNING):
    """Returns a logger with a custom rich handler."""

    from sdsstools.logger import get_logger

    log = get_logger("gort")

    # Remove normal console logger.
    log.removeHandler(log.sh)
    if log.warnings_logger:
        log.warnings_logger.removeHandler(log.sh)

    rich_handler = CustomRichHandler(
        level=verbosity_level,
        log_time_format="%X",
        show_path=False,
        console=Console(
            theme=Theme(
                {
                    "logging.level.debug": "magenta",
                    "logging.level.warning": "yellow",
                    "logging.level.critical": "red",
                    "logging.level.error": "red",
                }
            )
        ),
    )
    log.addHandler(rich_handler)

    # Use the sh attribute for the rich handler.
    log.sh = rich_handler  # type:ignore

    # Connect handler with the warnings.
    if log.warnings_logger:
        if rich_handler not in log.warnings_logger.handlers:
            log.warnings_logger.addHandler(rich_handler)

    return log


async def cancel_task(task: asyncio.Task | None):
    """Safely cancels a task."""

    if task is None or task.done():
        return

    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
