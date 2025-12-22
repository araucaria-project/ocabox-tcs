import asyncio
import time
from typing import Optional, Dict

from ob.planrunner import ConfigGeneral
from obcom.comunication.comunication_error import CommunicationTimeoutError
from ocaboxapi.exceptions import OcaboxServerError, OcaboxAccessDenied
from pyaraucaria.dome_eq import dome_eq_azimuth

from ocabox_tcs.monitoring import Status
from ocabox_tcs.services.dome_follower_svc.nats_conn import NatsConn
from ocabox_tcs.services.dome_follower_svc.tic_conn import TicConn


class Manager:

    def __init__(
            self, service = None, config = None, client_name: str = 'CliClient',
            software_id: str = 'dome_follower',
            obs_config_stream: str ="tic.config.observatory") -> None:

        self.service: 'DomeFollowerService' = service
        self.svc_config = config
        self.svc_logger = self.service.svc_logger
        self.nats_conn: Optional[NatsConn] = None
        self.tic_conn: Optional[TicConn] = None
        self.follow_on: bool = False
        self.obs_cfg: Optional[ConfigGeneral] = None
        self.client_name = client_name
        self.software_id = software_id
        self.obs_config_stream = obs_config_stream
        self.mount_type: Optional[str] = None
        self.dome_radius: Optional[float] = None
        self.spx: Optional[float] = None
        self.spy: Optional[float] = None
        self.gem: Optional[float] = None
        self.lon: Optional[float] = None
        self.lat: Optional[float] = None
        self.elev: Optional[float] = None
        self.follow_tolerance: Optional[float] = None
        self.settle_time: Optional[float] = None
        self.dome_speed_deg: Optional[float] = None
        self.dome_az_last: Optional[float] = None
        self.dome_current_speed: float = 0
        self.turn_time: float = 0
        # self.slew_tolerance: Optional[float] = None
        # self.slew_timeout: Optional[float] = None
        super().__init__()

    async def start_comm(self):
        self.svc_logger.info(f'Starting communication.')
        self.nats_conn = NatsConn(manager=self)
        self.tic_conn = TicConn(manager=self)
        await self.tic_conn.init_peripherals(telescope_id=self.svc_config.variant)
        await self.nats_conn.connect()
        await self.tic_conn.get_obs_cfg()
        await self.nats_conn.start_responders()

    async def stop_comm(self):
        self.svc_logger.info(f'Stopping communication.')
        await self.nats_conn.close()

    async def set_follow_params(self):
        self.follow_tolerance = self.svc_config.follow_tolerance
        self.settle_time = self.svc_config.settle_time
        self.dome_speed_deg =  self.svc_config.dome_speed
        self.svc_logger.info(
            f"Follow parameters: "
            f"follow_tolerance: {self.follow_tolerance} deg, "
            f"settle_time: {self.settle_time} s, "
            f"dome_speed_deg: {self.dome_speed_deg} deg/s"
        )

    async def set_mount_type_params(self):
        self.mount_type = self.obs_cfg.get_value(seq=[
             'telescopes', self.tic_conn.telescope.id, 'observatory', 'components', 'mount', 'type'
        ])
        self.svc_logger.info(
            f"Mount type: {self.mount_type}"
        )
        if self.mount_type is None:
            self.svc_logger.error(
                f'Can not get obs_config: mount_type for {self.tic_conn.telescope.id}'
            )
            raise RuntimeError
        if self.mount_type == 'eq':
            self.dome_radius = self.obs_cfg.get_value(seq=[
                'telescopes', self.tic_conn.telescope.id, 'observatory', 'components', 'dome',
                'pointing_params', 'dome_radius'
            ])
            self.spx = self.obs_cfg.get_value(seq=[
                'telescopes', self.tic_conn.telescope.id, 'observatory', 'components', 'dome',
                'pointing_params', 'spx'
            ])
            self.spy = self.obs_cfg.get_value(seq=[
                'telescopes', self.tic_conn.telescope.id, 'observatory', 'components', 'dome',
                'pointing_params', 'spy'
            ])
            self.gem = self.obs_cfg.get_value(seq=[
                'telescopes', self.tic_conn.telescope.id, 'observatory', 'components', 'dome',
                'pointing_params', 'gem'
            ])
            self.lon: Optional[float] = self.obs_cfg.get_value(seq=[
                'site', 'global', 'geo_location', 'lon',
            ])
            self.lat: Optional[float] = self.obs_cfg.get_value(seq=[
                'site', 'global', 'geo_location', 'lat',
            ])
            self.elev: Optional[float] = self.obs_cfg.get_value(seq=[
                'site', 'global', 'geo_location', 'elev',
            ])
            self.svc_logger.info(
                f"Dome radius: {self.dome_radius} mm, "
                f"spx: {self.spx} mm, "
                f"spy: {self.spy} mm, "
                f"gem: {self.gem} mm, "
                f"lon: {self.lon} deg, "
                f"lat: {self.lat} deg, "
                f"elev: {self.elev} m"
            )
            if self.dome_radius is None or self.spx is None or self.spy is None or \
                self.gem is None or self.lon is None or self.lat is None or self.elev is None:
                self.svc_logger.error(
                    f'Can not get obs_config: dome_radius or '
                    f'spx or spy or gem or lon or lat or elev for {self.tic_conn.telescope.id}'
                )
                raise RuntimeError

    async def dome_target_az(self, mount_az: float) -> Optional[float]:
        if self.mount_type == 'eq':
            try:
                ra = await self.tic_conn.mount.aget_ra()
                dec = await self.tic_conn.mount.aget_dec()
                side_of_pier = await self.tic_conn.mount.aget_sideofpier()
            except OcaboxServerError as e:
                self.svc_logger.error(f'Tic OcaboxServerError, {e}')
                self.service.monitor.set_status(
                    Status.ERROR, f"Tic dome get Server Error {e}"
                )
                return None
            except CommunicationTimeoutError:
                self.svc_logger.error(f'Tic CommunicationTimeoutError')
                self.service.monitor.set_status(Status.DEGRADED, f"Tic dome get Time out")
                return None
            except OcaboxAccessDenied:
                self.svc_logger.error(f'Tic OcaboxAccessDenied')
                self.service.monitor.set_status(
                    Status.ERROR, f"Tic dome get Access Denied"
                )
                return None
            if ra is None or dec is None or side_of_pier is None:
                return None
            eq_mount_az, info_dict = dome_eq_azimuth(
                ra=ra, dec=dec, r_dome=self.dome_radius, spx=self.spx, spy=self.spy,
                gem=self.gem, side_of_pier=side_of_pier, latitude=self.lat,
                longitude=self.lon, elevation=self.elev
            )
            return eq_mount_az
        else:
            return mount_az

    async def calc_dome_speed(self, dome_az: float):
        if dome_az is not None and self.dome_az_last is not None:
            diff_az = abs(dome_az - self.dome_az_last) % 360
            if self.turn_time != 0:
                self.dome_current_speed = min(diff_az, 360 - diff_az) / self.turn_time
                if round(self.dome_current_speed, 1) != 0.0:
                    self.svc_logger.info(
                        f"Dome speed: {self.dome_current_speed:.1f} deg/s"
                    )

    async def dome_slew_settle(self, angle_to_go: Optional[float]) -> None:
        if self.dome_speed_deg and angle_to_go and self.dome_speed_deg:
            _settle_time = angle_to_go / self.dome_speed_deg
            await asyncio.sleep(_settle_time)
        else:
            _settle_time = self.settle_time
            await asyncio.sleep(_settle_time)
        self.svc_logger.info(f"Dome follow settle done: {_settle_time:.1f}s")

    async def dome_follow(self) -> None:
        if self.follow_on:
            async with self.service.monitor.track_task('checking'):
                try:
                    dome_slewing = await self.tic_conn.dome.aget_slewing()
                    dome_az = await self.tic_conn.dome.aget_az()
                    mount_az = await self.tic_conn.mount.aget_az()
                    mount_slewing = await self.tic_conn.mount.aget_slewing()
                    mount_tracking = await self.tic_conn.mount.aget_tracking()
                    await self.calc_dome_speed(dome_az=dome_az)
                    self.dome_az_last = dome_az
                except OcaboxServerError as e:
                    self.svc_logger.error(f'Tic OcaboxServerError, {e}')
                    self.service.monitor.set_status(
                        Status.ERROR, f"Tic dome get Server Error {e}"
                    )
                    return
                except CommunicationTimeoutError:
                    self.svc_logger.error(f'Tic CommunicationTimeoutError')
                    self.service.monitor.set_status(Status.DEGRADED, f"Tic dome get Time out")
                    return
                except OcaboxAccessDenied:
                    self.svc_logger.error(f'Tic OcaboxAccessDenied')
                    self.service.monitor.set_status(
                        Status.ERROR, f"Tic dome get Access Denied"
                    )
                    return

            if dome_slewing is False and mount_slewing is False and mount_tracking is True:
                dome_target_az = await self.dome_target_az(mount_az=mount_az)
                if dome_target_az is None:
                    self.svc_logger.error(f'Can not calculate dome target az')
                    return
                diff = abs(dome_az - dome_target_az) % 360
                min_diff = min(diff, 360 - diff)
                if min_diff > self.follow_tolerance and self.dome_current_speed == 0.0:
                    self.svc_logger.info(
                        f"Dome is following: {dome_az:.3f} -> {dome_target_az:.3f}"
                    )
                    async with self.service.monitor.track_task('slewing'):
                        try:
                            await self.tic_conn.dome.aput_slewtoazimuth(dome_target_az)
                        except OcaboxServerError as e:
                            self.svc_logger.error(f'Tic OcaboxServerError, {e}')
                            self.service.monitor.set_status(
                                Status.ERROR, f"Tic slewtoazimuth Server Error {e}"
                            )
                            return
                        except CommunicationTimeoutError:
                            self.svc_logger.error(f'Tic CommunicationTimeoutError')
                            self.service.monitor.set_status(
                                Status.DEGRADED, f"Tic slewtoazimuth Time out"
                            )
                            return
                        except OcaboxAccessDenied:
                            self.svc_logger.error(f'Tic OcaboxAccessDenied')
                            self.service.monitor.set_status(
                                Status.ERROR, f"Tic slewtoazimuth Access Denied"
                            )
                            return
                        self.service.monitor.cancel_error_status()
                        await self.dome_slew_settle(min_diff)
