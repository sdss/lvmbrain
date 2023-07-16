#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-03-10
# @Filename: telescope.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, ClassVar

import numpy

from gort import config
from gort.exceptions import GortTelescopeError
from gort.gort import GortDevice, GortDeviceSet
from gort.tools import angular_separation


if TYPE_CHECKING:
    from gort.core import ActorReply
    from gort.gort import GortClient


__all__ = ["Telescope", "TelescopeSet", "KMirror", "FibSel", "Focuser", "MoTanDevice"]


class MoTanDevice(GortDevice):
    """A TwiceAsNice device."""

    #: Artificial delay introduced to prevent all motors to slew at the same time.
    SLEW_DELAY: ClassVar[float | dict[str, float]] = 0

    async def slew_delay(self):
        """Sleeps the :obj:`.SLEW_DELAY` amount."""

        if isinstance(self.SLEW_DELAY, (float, int)):
            await asyncio.sleep(self.SLEW_DELAY)
        else:
            await asyncio.sleep(self.SLEW_DELAY[self.name.split(".")[0]])


class KMirror(MoTanDevice):
    """A device representing a K-mirror."""

    SLEW_DELAY = {"sci": 1, "skye": 2, "skyw": 3}

    async def status(self):
        """Returns the status of the k-mirror."""

        return await self.actor.commands.status()

    async def home(self):
        """Homes the k-mirror."""

        await self.slew_delay()

        self.write_to_log("Homing k-mirror.", level="info")
        await self.actor.commands.moveToHome()
        self.write_to_log("k-mirror homing complete.")

    async def park(self):
        """Park the k-mirror at 90 degrees."""

        await self.slew_delay()

        await self.actor.commands.slewStop()
        await self.move(90)

    async def move(self, degs: float):
        """Move the k-mirror to a position in degrees. Does NOT track after the move.

        Parameters
        ----------
        degs
            The position to which to move the k-mirror, in degrees.

        """

        await self.slew_delay()

        self.write_to_log(f"Moving k-mirror to {degs:.3f} degrees.", level="info")

        self.write_to_log("Stopping slew.")
        await self.actor.commands.slewStop()

        self.write_to_log("Moving k-mirror to absolute position.")
        await self.actor.commands.moveAbsolute(degs, "deg")

    async def slew(self, ra: float, dec: float):
        """Moves the mirror to the position for ``ra, dec`` and starts slewing.

        Parameters
        ----------
        ra
            Right ascension of the field to track, in degrees.
        dec
            Declination of the field to track, in degrees.

        """

        await self.slew_delay()

        self.write_to_log(
            f"Slewing k-mirror to ra={ra:.6f} dec={dec:.6f} and tracking.",
            level="info",
        )

        await self.actor.commands.slewStart(ra / 15.0, dec)


class Focuser(MoTanDevice):
    """A device representing a focuser."""

    SLEW_DELAY = {"spec": 0, "sci": 1, "skye": 2, "skyw": 3}

    async def status(self):
        """Returns the status of the focuser."""

        return await self.actor.commands.status()

    async def home(self):
        """Homes the focuser."""

        await self.slew_delay()

        self.write_to_log("Homing focuser.", level="info")
        await self.actor.commands.moveToHome()
        self.write_to_log("Focuser homing complete.")

    async def move(self, dts: float):
        """Move the focuser to a position in DT."""

        await self.slew_delay()

        self.write_to_log(f"Moving focuser to {dts:.3f} DT.", level="info")
        await self.actor.commands.moveAbsolute(dts, "DT")


class FibSel(MoTanDevice):
    """A device representing the fibre mask in the spectrophotometric telescope."""

    # We really don't want a delay here because it would slow down the acquisition
    # of new standards, and anyway the fibre selector usually moves by itself.
    SLEW_DELAY = 0

    async def status(self):
        """Returns the status of the fibre selector."""

        return await self.actor.commands.status()

    async def home(self):
        """Homes the fibre selector."""

        await self.slew_delay()

        self.write_to_log("Homing fibsel.", level="info")
        await self.actor.commands.moveToHome()
        self.write_to_log("Fibsel homing complete.")

    def list_positions(self) -> list[str]:
        """Returns a list of valid positions."""

        return list(config["telescopes"]["mask_positions"])

    async def move_to_position(self, position: str | int):
        """Moves the spectrophotometric mask to the desired position.

        Parameters
        ----------
        position
            A position in the form `PN-M` where ``N=1,2`` and ``M=1-12``, in which
            case the mask will rotate to expose the fibre with that name. If
            ``position`` is a number, moves the mask to that value.

        """

        if isinstance(position, str):
            mask_positions = config["telescopes"]["mask_positions"]
            if position not in mask_positions:
                raise GortTelescopeError(
                    f"Cannot find position {position!r}.",
                    error_code=201,
                )

            steps = mask_positions[position]
            self.write_to_log(f"Moving mask to {position}: {steps} DT.", level="info")

        else:
            steps = position
            self.write_to_log(f"Moving mask to {steps} DT.", level="info")

        await self.slew_delay()
        await self.actor.commands.moveAbsolute(steps)

    async def move_relative(self, steps: float):
        """Move the mask a number of motor steps relative to the current position."""

        self.write_to_log(f"Moving fibre mask {steps} steps.")

        await self.slew_delay()
        await self.actor.commands.moveRelative(steps)


class Telescope(GortDevice):
    """Class representing an LVM telescope functionality."""

    def __init__(self, gort: GortClient, name: str, actor: str, **kwargs):
        super().__init__(gort, name, actor)

        self.config = self.gort.config["telescopes"]
        self.pwi = self.actor

        kmirror_actor = kwargs.get("kmirror", None)
        if kmirror_actor:
            self.has_kmirror = True
            self.km = KMirror(self.gort, f"{self.name}.km", kmirror_actor)
        else:
            self.has_kmirror = False
            self.km = None

        self.focuser = Focuser(self.gort, f"{self.name}.focuser", kwargs["focuser"])

        self.fibsel = (
            FibSel(self.gort, f"{self.name}.fibsel", "lvm.spec.fibsel")
            if self.name == "spec"
            else None
        )

        if self.name in self.gort.guiders:
            self.guider = self.gort.guiders[name]
        else:
            self.guider = None

    async def status(self):
        """Retrieves the status of the telescope."""

        reply: ActorReply = await self.pwi.commands.status()
        return reply.flatten()

    async def is_ready(self):
        """Checks if the telescope is ready to be moved."""

        status = await self.status()

        is_connected = status.get("is_connected", False)
        is_enabled = status.get("is_enabled", False)

        return is_connected and is_enabled

    async def initialise(self, home: bool | None = None):
        """Connects to the telescope and initialises the axes.

        Parameters
        ----------
        home
            If `True`, runs the homing routine after initialising.

        """

        if not (await self.is_ready()):
            self.write_to_log("Initialising telescope.")
            await self.pwi.commands.setConnected(True)
            await self.pwi.commands.setEnabled(True)

        if home is True:
            await self.home()

    async def home(self, home_subdevices: bool = False):
        """Initialises and homes the telescope.

        Parameters
        ---------
        home_subdevices
            Homes telecope k-mirror, focuser, and fibre selector (if
            applicable).

        """

        home_subdevices_task: asyncio.Future | None = None
        if home_subdevices:
            subdev_tasks = []
            if self.km is not None:
                subdev_tasks.append(self.km.home())
            if self.fibsel is not None:
                subdev_tasks.append(self.fibsel.home())
            if self.focuser is not None:
                subdev_tasks.append(self.focuser.home())

            home_subdevices_task = asyncio.gather(*subdev_tasks)

        if await self.gort.enclosure.is_local():
            raise GortTelescopeError("Cannot home in local mode.", error_code=101)

        self.write_to_log("Homing telescope.", level="info")

        if not (await self.is_ready()):
            await self.pwi.commands.setConnected(True)
            await self.pwi.commands.setEnabled(True)

        await self.pwi.commands.findHome()

        # findHome does not block, so wait a reasonable amount of time.
        await asyncio.sleep(30)

        if home_subdevices_task is not None and not home_subdevices_task.done():
            await home_subdevices_task

    async def park(
        self,
        disable=True,
        use_pw_park=False,
        alt_az: tuple[float, float] | None = None,
        kmirror: bool = True,
        force: bool = False,
    ):
        """Parks the telescope.

        Parameters
        ----------
        disable
            Disables the axes after reaching the park position.
        use_pw_park
            Uses the internal park position stored in the PlaneWave mount.
        alt_az
            A tuple with the alt and az position at which to park the telescope.
            If not provided, defaults to the ``park`` named position.
        kmirror
            Whether to park the mirror at 90 degrees.
        force
            Moves the telescope even if the mode is local.

        """

        if await self.gort.enclosure.is_local():
            raise GortTelescopeError("Cannot home in local mode.", error_code=101)

        await self.initialise()

        if use_pw_park:
            self.write_to_log("Parking telescope to PW default position.", level="info")
            await self.pwi.commands.park()
        elif alt_az is not None:
            self.write_to_log(f"Parking telescope to alt={alt_az[0]}, az={alt_az[1]}.")
            await self.goto_coordinates(
                alt=alt_az[0],
                az=alt_az[1],
                kmirror=False,
                altaz_tracking=False,
                force=force,
            )
        else:
            coords = config["telescopes"]["named_positions"]["park"]["all"]
            alt = coords["alt"]
            az = coords["az"]
            self.write_to_log(f"Parking telescope to alt={alt}, az={az}.", level="info")
            await self.goto_coordinates(
                alt=alt,
                az=az,
                kmirror=False,
                altaz_tracking=False,
                force=force,
            )

        if disable:
            self.write_to_log("Disabling telescope.")
            await self.pwi.commands.setEnabled(False)

        if kmirror and self.km:
            self.write_to_log("Parking k-mirror.", level="info")
            await self.km.park()

    async def stop(self):
        """Stops the mount."""

        self.write_to_log("Stopping the mount.", "warning")
        await self.actor.commands.stop()

    async def goto_coordinates(
        self,
        ra: float | None = None,
        dec: float | None = None,
        alt: float | None = None,
        az: float | None = None,
        kmirror: bool = True,
        altaz_tracking: bool = False,
        use_pointing_offsets: bool = True,
        force: bool = False,
        retry: bool = True,
    ):
        """Moves the telescope to a given RA/Dec or Alt/Az.

        Parameters
        ----------
        ra
            Right ascension coordinates to move to, in degrees.
        dec
            Declination coordinates to move to, in degrees.
        alt
            Altitude coordinates to move to, in degrees.
        az
            Azimuth coordinates to move to, in degrees.
        kmirror
            Whether to move the k-mirror into position. Only when
            the coordinates provided are RA/Dec.
        altaz_tracking
            If `True`, starts tracking after moving to alt/az coordinates.
            By defaul the PWI won't track with those coordinates.
        use_pointing_offsets
            If defined, uses the RA/Dec calibrations offsets for the telescope.
        force
            Move the telescopes even if mode is local.
        retry
            Retry once if the coordinates are not reached.

        """

        if (await self.gort.enclosure.is_local()) and not force:
            raise GortTelescopeError(
                "Cannot move telescope in local mode.",
                error_code=101,
            )

        kmirror_task: asyncio.Task | None = None
        if kmirror and self.km and ra and dec:
            kmirror_task = asyncio.create_task(self.km.slew(ra, dec))

        # Commanded and reported coordinates. To be used to check if we reached
        # the correct position.
        commanded: tuple[float, float]
        reported: tuple[float, float]

        if ra is not None and dec is not None:
            is_radec = ra is not None and dec is not None and not alt and not az
            assert is_radec, "Invalid input parameters"

            await self.initialise()

            self.write_to_log(f"Moving to ra={ra:.6f} dec={dec:.6f}.", level="info")

            if use_pointing_offsets:
                offsets = self.config.get("pointing_offsets", {}).get(self.name, None)
                if offsets is not None:
                    ra_offset = offsets[0] / 3600.0
                    dec_offset = offsets[1] / 3600.0
                    ra = float(ra + ra_offset / numpy.cos(numpy.radians(dec)))
                    dec = float(dec + dec_offset)

            ra = float(numpy.clip(ra, 0, 360))
            dec = float(numpy.clip(dec, -90, 90))

            await self.pwi.commands.gotoRaDecJ2000(ra / 15.0, dec)

            # Check the position the PWI reports.
            status = await self.status()
            ra_status = status["ra_j2000_hours"] * 15
            dec_status = status["dec_j2000_degs"]

            commanded = (ra, dec)
            reported = (ra_status, dec_status)

        elif alt is not None and az is not None:
            is_altaz = alt is not None and az is not None and not ra and not dec
            assert is_altaz, "Invalid input parameters"

            await self.initialise()

            self.write_to_log(f"Moving to alt={alt:.6f} az={az:.6f}.", level="info")
            await self.pwi.commands.gotoAltAzJ2000(alt, az)

            # Check the position the PWI reports.
            status = await self.status()
            az_status = status["azimuth_degs"]
            alt_status = status["altitude_degs"]

            commanded = (az, alt)
            reported = (az_status, alt_status)

        else:
            raise GortTelescopeError("Invalid coordinates.")

        # Check if we reached the position. If not retry once or fail.
        separation = angular_separation(*commanded, *reported)
        if separation > 0.1:
            if retry:
                self.write_to_log(
                    "Telescope failed to reach the desired position. Retrying",
                    "warning",
                )
                # Need to make sure the k-mirror is not moving before re-trying.
                if kmirror_task is not None and not kmirror_task.done():
                    await kmirror_task

                await asyncio.sleep(3)

                return await self.goto_coordinates(
                    ra=ra,
                    dec=dec,
                    alt=alt,
                    az=az,
                    kmirror=kmirror,
                    altaz_tracking=altaz_tracking,
                    use_pointing_offsets=use_pointing_offsets,
                    force=force,
                    retry=False,
                )
            else:
                await self.actor.commands.setEnabled(False)
                raise GortTelescopeError(
                    "Telescope failed to reach desired position. "
                    "The axes have been disabled for safety. "
                    "Try re-homing the telescope.",
                    error_code=102,
                )

        if alt is not None and az is not None and altaz_tracking:
            await self.pwi.commands.setTracking(enable=True)

        if kmirror_task is not None and not kmirror_task.done():
            await kmirror_task

    async def goto_named_position(
        self,
        name: str,
        altaz_tracking: bool = False,
        force: bool = False,
    ):
        """Sends the telescope to a named position.

        Parameters
        ----------
        name
            The name of the position, e.g., ``'zenith'``.
        altaz_tracking
            Whether to start tracking after reaching the position, if the
            coordinates are alt/az.
        force
            Move the telescope even in local enclosure mode.

        """

        if (await self.gort.enclosure.is_local()) and not force:
            raise GortTelescopeError(
                "Cannot move telescope in local mode.",
                error_code=101,
            )

        if name not in config["telescopes"]["named_positions"]:
            raise GortTelescopeError(
                f"Invalid named position {name!r}.",
                error_code=103,
            )

        position_data = config["telescopes"]["named_positions"][name]

        if self.name in position_data:
            coords = position_data[self.name]
        elif "all" in position_data:
            coords = position_data["all"]
        else:
            raise GortTelescopeError("Cannot find position data.", error_code=103)

        if "alt" in coords and "az" in coords:
            coro = self.goto_coordinates(
                alt=coords["alt"],
                az=coords["az"],
                altaz_tracking=altaz_tracking,
                force=force,
            )
        elif "ra" in coords and "dec" in coords:
            coro = self.goto_coordinates(
                ra=coords["ra"],
                dec=coords["dec"],
                force=force,
            )
        else:
            raise GortTelescopeError(
                "No ra/dec or alt/az coordinates found.",
                error_code=103,
            )

        await coro

    async def offset(
        self,
        ra: float | None = None,
        dec: float | None = None,
        axis0: float | None = None,
        axis1: float | None = None,
    ):
        """Apply an offset to the telescope axes.

        Parameters
        ----------
        ra
            Offset in RA, in arcsec.
        dec
            Offset in Dec, in arcsec.
        axis0
            Offset in axis 0, in arcsec.
        axis1
            Offset in axis 1, in arcsec.

        """

        if any([ra, dec]) and any([axis0, axis1]):
            raise GortTelescopeError(
                "Cannot offset in ra/dec and axis0/axis1 at the same time."
            )

        kwargs = {}
        if any([ra, dec]):
            if ra is not None:
                kwargs["ra_add_arcsec"] = ra
            if dec is not None:
                kwargs["dec_add_arcsec"] = dec
            self.write_to_log(f"Offsetting RA={ra:.3f}, Dec={dec:.3f}.")

        elif any([axis0, axis1]):
            if axis0 is not None:
                kwargs["axis0_add_arcsec"] = axis0
            if axis1 is not None:
                kwargs["axis1_add_arcsec"] = axis1
            self.write_to_log(f"Offsetting axis0={axis0:.3f}, axis1={axis1:.3f}.")

        else:
            # This should not happen.
            raise GortTelescopeError("No offsets provided.")

        await self.actor.commands.offset(**kwargs)

        self.write_to_log("Offset complete.")

        return True


class TelescopeSet(GortDeviceSet[Telescope]):
    """A representation of a set of telescopes."""

    __DEVICE_CLASS__ = Telescope

    def __init__(self, gort: GortClient, data: dict[str, dict]):
        super().__init__(gort, data)

        self.guiders = self.gort.guiders

    async def initialise(self):
        """Initialise all telescopes."""

        await asyncio.gather(*[tel.initialise() for tel in self.values()])

    async def home(self, home_subdevices: bool = False):
        """Initialises and homes all telescopes.

        Parameters
        ---------
        home_subdevices
            Homes telecope k-mirror, focuser, and fibre selector (if
            applicable).

        """

        await self.call_device_method(Telescope.home, home_subdevices=home_subdevices)

    async def park(
        self,
        disable=True,
        use_pw_park=False,
        alt_az: tuple[float, float] | None = None,
        kmirror=True,
        force=False,
    ):
        """Parks the telescopes.

        Parameters
        ----------
        disable
            Disables the axes after reaching the park position.
        use_pw_park
            Uses the internal park position stored in the PlaneWave mounts.
        alt_az
            A tuple with the alt and az position at which to park the telescopes.
            If not provided, defaults to the ``park`` named position.
        kmirror
            Whether to park the mirrors at 90 degrees.
        force
            Moves the telescopes even if the mode is local.

        """

        await self.call_device_method(
            Telescope.park,
            disable=disable,
            use_pw_park=use_pw_park,
            alt_az=alt_az,
            kmirror=kmirror,
            force=force,
        )

    async def goto_coordinates_all(
        self,
        ra: float | None = None,
        dec: float | None = None,
        alt: float | None = None,
        az: float | None = None,
        kmirror: bool = True,
        altaz_tracking: bool = False,
        force: bool = False,
    ):
        """Moves all the telescopes to a given RA/Dec or Alt/Az.

        Parameters
        ----------
        ra
            Right ascension coordinates to move to.
        dec
            Declination coordinates to move to.
        alt
            Altitude coordinates to move to.
        az
            Azimuth coordinates to move to.
        kmirror
            Whether to move the k-mirror into position. Only when
            the coordinates provided are RA/Dec.
        altaz_tracking
            If `True`, starts tracking after moving to alt/az coordinates.
            By defaul the PWI won't track with those coordinates.
        force
            Move the telescopes even if mode is local.

        """

        await self.call_device_method(
            Telescope.goto_coordinates,
            ra=ra,
            dec=dec,
            alt=alt,
            az=az,
            kmirror=kmirror,
            altaz_tracking=altaz_tracking,
            force=force,
        )

    async def goto_named_position(
        self,
        name: str,
        altaz_tracking: bool = False,
        force: bool = False,
    ):
        """Sends the telescopes to a named position.

        Parameters
        ----------
        name
            The name of the position, e.g., ``'zenith'``.
        altaz_tracking
            Whether to start tracking after reaching the position, if the
            coordinates are alt/az.
        force
            Move the telescopes even in local enclosure mode.

        """

        await self._check_local(force)

        if name not in config["telescopes"]["named_positions"]:
            raise GortTelescopeError(
                f"Invalid named position {name!r}.",
                error_code=103,
            )

        await self.call_device_method(
            Telescope.goto_named_position,
            name=name,
            altaz_tracking=altaz_tracking,
            force=force,
        )

    async def stop(self):
        """Stops all the mounts."""

        await self.call_device_method(Telescope.stop)

    async def goto(
        self,
        sci: tuple[float, float] | None = None,
        spec: tuple[float, float] | None = None,
        skye: tuple[float, float] | None = None,
        skyw: tuple[float, float] | None = None,
        force: bool = False,
    ):
        """Sends each telescope to a different position.

        Parameters
        ----------
        sci
            The RA and Dec where to slew the science telescope.
        spec
            The RA and Dec where to slew the spectrophotometric telescope.
        skye
            The RA and Dec where to slew the skyE telescope.
        skyw
            The RA and Dec where to slew the skyW telescope.

        """

        await self._check_local(force)

        jobs = []

        if sci is not None:
            jobs.append(self["sci"].goto_coordinates(ra=sci[0], dec=sci[1]))

        if spec is not None:
            jobs.append(self["spec"].goto_coordinates(ra=spec[0], dec=spec[1]))

        if skye is not None:
            jobs.append(self["skye"].goto_coordinates(ra=skye[0], dec=skye[1]))

        if skyw is not None:
            jobs.append(self["skyw"].goto_coordinates(ra=skyw[0], dec=skyw[1]))

        if len(jobs) == 0:
            return

        await asyncio.gather(*jobs)

    async def _check_local(self, force: bool = False):
        """Checks if the telescope is in local mode and raises an error."""

        is_local = await self.gort.enclosure.is_local()
        if is_local and not force:
            raise GortTelescopeError(
                "Cannot move telescopes in local mode.",
                error_code=101,
            )
