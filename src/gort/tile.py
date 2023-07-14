#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# @Author: José Sánchez-Gallego (gallegoj@uw.edu)
# @Date: 2023-07-08
# @Filename: tile.py
# @License: BSD 3-clause (http://www.opensource.org/licenses/BSD-3-Clause)

from __future__ import annotations

import warnings

import pandas
from astropy.coordinates import EarthLocation, SkyCoord
from astropy.time import Time
from httpx import RequestError

from gort.exceptions import GortNotImplemented, GortWarning, TileError
from gort.tools import get_calibrators_sync, get_db_connection, get_next_tile_id_sync
from gort.transforms import fibre_to_master_frame


__all__ = [
    "Coordinates",
    "QuerableCoordinates",
    "ScienceCoordinates",
    "SkyCoordinates",
    "StandardCoordinates",
    "Tile",
]


CoordTuple = tuple[float, float]


class Coordinates:
    """Basic coordinates class.

    Parameters
    ----------
    ra
        The RA coordinate, in degrees. FK5 frame at the epoch of observation.
    dec
        The Dec coordinate, in degrees.

    """

    def __init__(self, ra: float, dec: float):
        self.ra = ra
        self.dec = dec
        self.skycoord = SkyCoord(ra=ra, dec=dec, unit="deg", frame="fk5")

        # The MF pixel on which to guide/centre the target.
        self._mf_pixel: tuple[float, float] | None = None

    def __repr__(self):
        return f"<{self.__class__.__name__} (ra={self.ra:.6f}, dec={self.dec:.6f})>"

    def __str__(self):
        return f"{self.ra:.6f}, {self.dec:.6f}"

    def calculate_altitude(self, time: Time | None = None):
        """Returns the current altitude of the target."""

        time = time or Time.now()
        location = EarthLocation.of_site("Las Campanas Observatory")

        sc = self.skycoord.copy()
        sc.obstime = time
        sc.location = location
        altaz = sc.transform_to("altaz")

        return altaz.alt.deg

    def is_observable(self):
        """Determines whether a target is observable."""

        return self.calculate_altitude() > 30


class QuerableCoordinates(Coordinates):
    """A class of coordinates that can be retrieved from the database."""

    __db_table__: str = ""
    targets: SkyCoord | None = None

    @classmethod
    def from_science_coordinates(
        cls,
        sci_coords: ScienceCoordinates,
        exclude_coordinates: list[CoordTuple] = [],
        exclude_invisible: bool = True,
    ):
        """Retrieves a valid and observable position from the database.

        Parameters
        ----------
        sci_coords
            The science coordinates. The position selected will be the
            closest to these coordinates.
        exclude_coordinates
            A list of RA/Dec coordinates to exclude. No region closer
            than one degree to these coordinates will be selected.
        exclude_invisible
            Exclude targets that are too low.

        """

        connection = get_db_connection()

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            targets = pandas.read_sql(
                f"SELECT ra,dec from {cls.__db_table__};",
                connection,  # type:ignore
            )

        # Cache query.
        if cls.targets is None:
            cls.targets = SkyCoord(ra=targets.ra, dec=targets.dec, unit="deg")

        skycoords = cls.targets.copy()

        # Exclude regions too close to the exlcuded ones.
        for ex_coords in exclude_coordinates:
            ex_skycoords = SkyCoord(ra=ex_coords[0], dec=ex_coords[1], unit="deg")
            skycoords = skycoords[skycoords.separation(ex_skycoords).deg > 1]

        # Exclude targets that are too low.
        if exclude_invisible:
            skycoords.location = EarthLocation.of_site("Las Campanas Observatory")
            skycoords.obstime = Time.now()
            altaz_skycoords = skycoords.transform_to("altaz")
            skycoords = skycoords[altaz_skycoords.alt.deg > 30]

        if len(skycoords) == 0:
            raise TileError("No sky coordinates found.")

        seps = skycoords.separation(sci_coords.skycoord)
        skycoord_min = skycoords[seps.argmin()]

        return cls(skycoord_min.ra.deg, skycoord_min.dec.deg)

    def verify_and_replace(self, exclude_coordinates: list[CoordTuple] = []):
        """Verifies that the coordinates are visible and if not, replaces them.

        Parameters
        ----------
        exclude_coordinates
            A list of RA/Dec coordinates to exclude. No region closer
            than one degree to these coordinates will be selected.

        """

        if not self.is_observable():
            # Use current coordinates as proxy for the science telescope.
            sci_coords = ScienceCoordinates(self.ra, self.dec)
            valid_skycoords = self.from_science_coordinates(
                sci_coords,
                exclude_coordinates=exclude_coordinates,
            )
            super().__init__(valid_skycoords.ra, valid_skycoords.dec)


class ScienceCoordinates(Coordinates):
    """A science position.

    Parameters
    ----------
    ra
        The RA coordinate, in degrees. FK5 frame at the epoch of observation.
    dec
        The Dec coordinate, in degrees.
    centre_on_fibre
        The name of the fibre on which to centre the target, with the format
        ``<ifulabel>-<finufu>``. By default, acquires the target on the central
        fibre of the science IFU.

    """

    def __init__(self, ra: float, dec: float, centre_on_fibre: str | None = None):
        super().__init__(ra, dec)

        self.centre_on_fibre = centre_on_fibre

        # The MF pixel on which to guide/centre the target.
        self._mf_pixel = self.set_mf_pixel(centre_on_fibre)

    def set_mf_pixel(self, fibre_name: str | None = None, xz: CoordTuple | None = None):
        """Calculates and sets the master frame pixel on which to centre the target.

        If neither ``fibre_name`` or ``xz`` are passed, resets to centring
        the target on the central fibre of the IFU.

        Parameters
        ----------
        fibre_name
            The fibre to which to centre the target, with the format
            ``<ifulabel>-<finifu>``.
        xz
            The coordinates, in master frame pixels, on which to centre
            the target.

        Returns
        -------
        pixel
            A tuple with the x and z coordinates of the pixel in the master frame,
            or `None` if resetting to the central fibre.

        """

        if fibre_name is not None:
            xmf, zmf = fibre_to_master_frame(fibre_name)
        elif xz is not None:
            xmf, zmf = xz
        else:
            self._mf_pixel = None
            return None

        self._mf_pixel = (xmf, zmf)

        return (xmf, zmf)


class SkyCoordinates(QuerableCoordinates):
    """A sky position."""

    __db_table__ = "lvmopsdb.sky"


class StandardCoordinates(QuerableCoordinates):
    """A standard position."""

    __db_table__ = "lvmopsdb.standard"


class Tile(dict[str, Coordinates | list[Coordinates] | None]):
    """A representation of a science pointing with associated calibrators.

    This class is most usually initialised from a classmethod like
    `.from_scheduler`.

    Parameters
    ----------
    sci_coords
        The science telescope pointing.
    sky_coords
        A dictionary of ``skye`` and ``skyw`` coordinates.
    spec_coords
        A list of coordinates to observe with the spectrophotometric telescope.
    dither_position
        The dither position to obseve (not yet functional).
    allow_replacement
        If `True`, allows the replacement of empty, invalid or low altitude sky
        and standard targets.

    """

    def __init__(
        self,
        sci_coords: ScienceCoordinates,
        sky_coords: dict[str, SkyCoordinates | CoordTuple] | None = None,
        spec_coords: list[StandardCoordinates | CoordTuple] | None = None,
        dither_position: str = "C",
        allow_replacement: bool = True,
    ):
        self.allow_replacement = allow_replacement

        self.tile_id: int | None = None
        self.dither_position = dither_position

        self.sci_coords = self.set_sci_coords(sci_coords)
        self.sky_coords = self.set_sky_coords(
            sky_coords,
            allow_replacement=allow_replacement,
        )
        self.spec_coords = self.set_spec_coords(
            spec_coords,
            reject_invisible=allow_replacement,
        )

        dict.__init__(
            self,
            {
                "sci": self.sci_coords,
                "skye": self.sky_coords.get("skye", None),
                "skyw": self.sky_coords.get("skyw", None),
                "spec": self.spec_coords,
            },
        )

    def __repr__(self):
        return (
            "<Tile "
            f"(tile_id={self.tile_id}, "
            f"science ra={self.sci_coords.ra:.6f}, dec={self.sci_coords.dec:.6f}; "
            f"n_skies={len(self.sky_coords)}; n_standards={len(self.spec_coords)})>"
        )

    @classmethod
    def from_coordinates(
        cls,
        ra: float,
        dec: float,
        sky_coords: dict[str, SkyCoordinates | CoordTuple] | None = None,
        spec_coords: list[StandardCoordinates | CoordTuple] | None = None,
    ):
        """Creates an instance from coordinates, allowing autocompletion.

        Parameters
        ----------
        sci_coords
            The science telescope pointing.
        sky_coords
            A dictionary of ``skye`` and ``skyw`` coordinates. If `None`,
            autocompleted from the closest available regions.
        spec_coords
            A list of coordinates to observe with the spectrophotometric telescope.
            If `None`, selects the 12 closest standard stars.

        """

        sci_coords = ScienceCoordinates(ra, dec)

        if sky_coords is None:
            exclude_coordinates: list[CoordTuple] = []
            sky_coords = {}
            for telescope in ["skye", "skyw"]:
                coords = SkyCoordinates.from_science_coordinates(
                    sci_coords,
                    exclude_coordinates=exclude_coordinates,
                )
                sky_coords[telescope] = coords
                exclude_coordinates.append((coords.ra, coords.dec))

        if spec_coords is None:
            exclude_coordinates: list[CoordTuple] = []
            spec_coords = []
            for _ in range(12):
                coords = StandardCoordinates.from_science_coordinates(
                    sci_coords,
                    exclude_coordinates=exclude_coordinates,
                )
                spec_coords.append(coords)
                exclude_coordinates.append((coords.ra, coords.dec))

        return cls(
            sci_coords,
            spec_coords=spec_coords,
            sky_coords=sky_coords,
            allow_replacement=False,
        )

    @classmethod
    def from_scheduler(
        cls,
        tile_id: int | None = None,
        ra: float | None = None,
        dec: float | None = None,
    ):
        """Creates a new instance of `.Tile` with data from the scheduler.

        Parameters
        ----------
        tile_id
            The ``tile_id`` for which to create a new `.Tile`. If `None`, and
            ``ra`` and ``dec`` are also `None`, the best ``tile_id`` selected
            by the scheduler will be used.
        ra
            Right ascension coordinates of the science telescope pointing.
            Calibrators will be selected from the scheduler.
        dec
            Declination coordinates of the science telescope pointing.

        """

        if tile_id is None and ra is None and dec is None:
            try:
                tile_id_data = get_next_tile_id_sync()
            except RequestError:
                raise TileError("Cannot retrieve tile_id from scheduler.")

            tile_id = tile_id_data["tile_id"]
            sci_pos = tile_id_data["tile_pos"][:2]
            dither_pos = "C"

        elif tile_id is not None:
            raise GortNotImplemented("Initialising from a tile_id is not supported.")

        elif tile_id is None and (ra is not None and dec is not None):
            tile_id = None
            sci_pos = (ra, dec)
            dither_pos = "C"

        else:
            raise TileError("Invalid inputs.")

        sci_coords = ScienceCoordinates(*sci_pos, centre_on_fibre=None)

        calibrator_data = get_calibrators_sync(
            tile_id=None,
            ra=sci_pos[0],
            dec=sci_pos[1],
        )
        sky_coords = {
            "skye": calibrator_data["sky_pos"][0],
            "skyw": calibrator_data["sky_pos"][1],
        }
        spec_coords = list(calibrator_data["standard_pos"])

        new_obj = cls(
            sci_coords,
            sky_coords=sky_coords,
            spec_coords=spec_coords,
            dither_position=dither_pos,
        )
        new_obj.tile_id = tile_id

        return new_obj

    def set_sci_coords(
        self,
        sci_coords: ScienceCoordinates | CoordTuple,
    ) -> ScienceCoordinates:
        """Sets the science telescope coordinates.

        Parameters
        ----------
        sci_coords
            A `.ScienceCoordinates` object or a tuple with RA/Dec coordinates
            for the science telescope.

        """

        if isinstance(sci_coords, ScienceCoordinates):
            self.sci_coords = sci_coords
        else:
            self.sci_coords = ScienceCoordinates(*sci_coords)

        return self.sci_coords

    def set_sky_coords(
        self,
        sky_coords: dict[str, SkyCoordinates | CoordTuple] | None = None,
        allow_replacement: bool = True,
    ) -> dict[str, SkyCoordinates]:
        """Sets the sky telescopes coordinates.

        Parameters
        ----------
        sky_coords
            A dictionary of ``skye`` and ``skyw`` coordinates. Each value must
            be a `.SkyCoordinates` object or a tuple of RA/Dec coordinates.
        allow_replacement
            If `True`, allows the replacement of empty, invalid or low altitude
            targets.

        """

        if sky_coords is None:
            sky_coords = {}

        valid_sky_coords: dict[str, SkyCoordinates] = {}
        assigned_coordinates: list[CoordTuple] = []

        for telescope in ["skye", "skyw"]:
            tel_coords = sky_coords.get(telescope, None)

            replace: bool = False
            if tel_coords is None:
                replace = True
            elif isinstance(tel_coords, SkyCoordinates):
                tel_coords = tel_coords
            else:
                tel_coords = SkyCoordinates(*tel_coords)

            if allow_replacement is False:
                if tel_coords is not None:
                    valid_sky_coords[telescope] = tel_coords
                continue

            # If both coordinates are assigned, check that they are not identical.
            if (
                tel_coords is not None
                and telescope == "skyw"
                and "skye" in valid_sky_coords
            ):
                if (
                    tel_coords.ra == valid_sky_coords["skye"].ra
                    and tel_coords.dec == valid_sky_coords["skye"].dec
                ):
                    tel_coords = None
                    replace = True

            if replace:
                try:
                    tel_coords = SkyCoordinates.from_science_coordinates(
                        self.sci_coords,
                        exclude_coordinates=assigned_coordinates,
                    )
                except Exception as err:
                    warnings.warn(
                        f"Failed getting sky coordinates for {telescope}: {err}",
                        GortWarning,
                    )
                    continue

            try:
                assert tel_coords is not None
                tel_coords.verify_and_replace(
                    exclude_coordinates=assigned_coordinates,
                )
            except Exception as err:
                warnings.warn(
                    f"Failed verifying sky coordinates for {telescope}: {err}",
                    GortWarning,
                )
                continue

            assigned_coordinates.append((tel_coords.ra, tel_coords.dec))
            valid_sky_coords[telescope] = tel_coords

        self.sky_coords = valid_sky_coords

        return self.sky_coords

    def set_spec_coords(
        self,
        spec_coords: list[StandardCoordinates | CoordTuple] | None = None,
        reject_invisible: bool = True,
    ) -> list[StandardCoordinates]:
        """Sets the spec telescope coordinates.

        Parameters
        ----------
        spec_coords
            A list of coordinates to observe with the spectrophotometric telescope.
        reject_invisible
            Skip targets that are not visible now.

        """

        valid_spec_coords = []

        if spec_coords is None:
            pass
        else:
            for coords in spec_coords:
                if not isinstance(coords, Coordinates):
                    coords = StandardCoordinates(*coords)

                if reject_invisible and not coords.is_observable():
                    continue

                valid_spec_coords.append(coords)

        self.spec_coords = valid_spec_coords

        return self.spec_coords
