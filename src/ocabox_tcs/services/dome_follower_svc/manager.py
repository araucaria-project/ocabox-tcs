import asyncio
from typing import Optional, Dict

from ob.planrunner import ConfigGeneral
from obcom.comunication.comunication_error import CommunicationTimeoutError
from ocaboxapi.exceptions import OcaboxServerError, OcaboxAccessDenied

from ocabox_tcs.services.dome_follower_svc.nats_conn import NatsConn
from ocabox_tcs.services.dome_follower_svc.tic_conn import TicConn


class Manager:

    def __init__(
            self, service = None, config = None, client_name: str = 'CliClient',
            software_id: str = 'dome_follower', obs_config_stream: str ="tic.config.observatory") -> None:
        self.service = service
        self.config = config
        self.logger = self.service.logger
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
        super().__init__()

    async def start_comm(self):
        self.logger.info(f'Starting communication.')
        self.nats_conn = NatsConn(manager=self)
        self.tic_conn = TicConn(manager=self)
        await self.tic_conn.init_peripherals(telescope_id=self.config.instance_context)
        await self.nats_conn.connect()
        await self.tic_conn.get_obs_cfg()
        await self.nats_conn.start_responders()

    async def stop_comm(self):
        self.logger.info(f'Stopping communication.')
        await self.nats_conn.close()

    async def set_follow_parameters(self):
        self.follow_tolerance = 3.0 #TODO take from obs settings
        # self.slew_tolerance = 3.0  #TODO take from obs settings
        self.settle_time = 5.0  #TODO take from obs settings

    async def dome_follow(self) -> None:
        if self.follow_on:
            try:
                dome_slew = await self.tic_conn.dome.aget_slewing()
                dome_az = await self.tic_conn.dome.aget_az()
                mount_az = await self.tic_conn.mount.aget_az()
            except OcaboxServerError:
                self.logger.error(f'Tic OcaboxServerError.')
                return
            except CommunicationTimeoutError:
                self.logger.error(f'Tic CommunicationTimeoutError')
                return
            except OcaboxAccessDenied:
                self.logger.error(f'Tic OcaboxAccessDenied')
                return

            if not dome_slew:
                diff = abs(dome_az - mount_az) % 360
                if min(diff, 360 - diff) > self.follow_tolerance:
                    self.logger.info(f"Dome slewing: {dome_az:.3f} -> {mount_az:.3f}")
                    try:
                        await self.tic_conn.dome.aput_slewtoazimuth(mount_az)
                    except OcaboxServerError:
                        self.logger.error(f'Tic OcaboxServerError')
                        return
                    except CommunicationTimeoutError:
                        self.logger.error(f'Tic CommunicationTimeoutError')
                        return
                    except OcaboxAccessDenied:
                        self.logger.error(f'Tic OcaboxAccessDenied')
                        return
                    await asyncio.sleep(self.settle_time)
