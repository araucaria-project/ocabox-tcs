import asyncio
import time
from typing import Optional, Dict

from ob.planrunner import ConfigGeneral
from obcom.comunication.comunication_error import CommunicationTimeoutError
from ocaboxapi.exceptions import OcaboxServerError, OcaboxAccessDenied

from ocabox_tcs.monitoring import Status
from ocabox_tcs.services.dome_follower_svc.nats_conn import NatsConn
from ocabox_tcs.services.dome_follower_svc.tic_conn import TicConn


class Manager:

    def __init__(
            self, service = None, config = None, client_name: str = 'CliClient',
            software_id: str = 'dome_follower', obs_config_stream: str ="tic.config.observatory") -> None:
        self.service: 'DomeFollowerService' = service
        self.config = config
        self.svc_logger = self.service.svc_logger
        self.nats_conn: Optional[NatsConn] = None
        self.tic_conn: Optional[TicConn] = None
        self.follow_on: bool = False
        self.obs_cfg: Optional[ConfigGeneral] = None
        self.client_name = client_name
        self.software_id = software_id
        self.obs_config_stream = obs_config_stream
        self.mount_type: Optional[str] = None
        # self.slew_timeout: Optional[float] = None
        self.follow_tolerance: Optional[float] = None
        # self.slew_tolerance: Optional[float] = None
        self.settle_time: Optional[float] = None
        self.dome_speed_deg: Optional[float] = None
        self.dome_az_last: Optional[float] = None
        self.dome_current_speed: float = 0
        self.turn_time: float = 0
        super().__init__()

    async def start_comm(self):
        self.svc_logger.info(f'Starting communication.')
        self.nats_conn = NatsConn(manager=self)
        self.tic_conn = TicConn(manager=self)
        await self.tic_conn.init_peripherals(telescope_id=self.config.variant)
        await self.nats_conn.connect()
        await self.tic_conn.get_obs_cfg()
        await self.nats_conn.start_responders()

    async def stop_comm(self):
        self.svc_logger.info(f'Stopping communication.')
        await self.nats_conn.close()

    async def set_follow_parameters(self):
        self.follow_tolerance = self.config.follow_tolerance
        self.settle_time = self.config.settle_time
        self.dome_speed_deg =  self.config.dome_speed
        self.svc_logger.info(
            f"Follow parameters: "
            f"follow_tolerance: {self.follow_tolerance}deg "
            f"settle_time: {self.settle_time}s "
            f"dome_speed_deg: {self.dome_speed_deg}deg/s"
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
                ts_0 = time.time()
                try:
                    dome_slewing = await self.tic_conn.dome.aget_slewing()
                    dome_az = await self.tic_conn.dome.aget_az()
                    mount_az = await self.tic_conn.mount.aget_az()
                    mount_slewing = await self.tic_conn.mount.aget_slewing()
                    if dome_az is not None and self.dome_az_last is not None:
                        diff = abs(dome_az - self.dome_az_last) % 360
                        if self.turn_time != 0:
                            self.dome_current_speed = min(diff, 360 - diff) / self.turn_time
                            if self.dome_current_speed != 0.0:
                                self.svc_logger.info(
                                    f"Dome speed: {self.dome_current_speed:.1f} deg/s"
                                )
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

            if dome_slewing is False and mount_slewing is False:
                diff = abs(dome_az - mount_az) % 360
                min_diff = min(diff, 360 - diff)
                if min_diff > self.follow_tolerance and self.dome_current_speed == 0.0:
                    self.svc_logger.info(f"Dome is following: {dome_az:.3f} -> {mount_az:.3f}")
                    async with self.service.monitor.track_task('slewing'):
                        try:
                            await self.tic_conn.dome.aput_slewtoazimuth(mount_az)
                        except OcaboxServerError as e:
                            self.svc_logger.error(f'Tic OcaboxServerError, {e}')
                            self.service.monitor.set_status(Status.ERROR, f"Tic slewtoazimuth Server Error {e}")
                            return
                        except CommunicationTimeoutError:
                            self.svc_logger.error(f'Tic CommunicationTimeoutError')
                            self.service.monitor.set_status(Status.DEGRADED, f"Tic slewtoazimuth Time out")
                            return
                        except OcaboxAccessDenied:
                            self.svc_logger.error(f'Tic OcaboxAccessDenied')
                            self.service.monitor.set_status(Status.ERROR, f"Tic slewtoazimuth Access Denied")
                            return
                        self.service.monitor.cancel_error_status()
                        await self.dome_slew_settle(min_diff)
            self.turn_time = time.time() - ts_0
