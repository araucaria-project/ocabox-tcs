from typing import Optional

from ob.planrunner import ConfigGeneral
from ocaboxapi import Telescope, Observatory, Dome, Mount, AccessGrantor


class TicConn:
    """
    Class is responsible for tic server connections
    """

    def __init__(self, manager = None):
        self.manager = manager
        self.svc_logger = self.manager.svc_logger
        self.obs: Optional[Observatory] = None
        self.telescope: Optional[Telescope] = None
        self.dome: Optional[Dome] = None
        self.mount: Optional[Mount] = None
        self.access_grantor: Optional[AccessGrantor] = None
        super().__init__()

    async def init_peripherals(self, telescope_id: str) -> None:
        self.obs = Observatory(
            client_name=self.manager.client_name,
            software_id=self.manager.software_id,
            config_stream=self.manager.obs_config_stream
        )
        self.telescope = self.obs.get_telescope(telescope_id=telescope_id)
        self.dome = self.telescope.get_dome()
        self.dome.request_special_permission = True
        self.mount = self.telescope.get_mount()
        # self.access_grantor = self.telescope.get_access_grantor()

    async def get_obs_cfg(self):
        self.svc_logger.info(f'Loading client config...')
        try:
            await self.obs.load_client_cfg(timeout=5.0)
        except TimeoutError:
            self.svc_logger.error(f"Can not load client config from nats - timeout.")
            raise
        self.svc_logger.info(f'Client config loaded.')
        self.obs.connect()
        self.manager.obs_cfg = ConfigGeneral(
            telescope=self.telescope,
            client_config_dict=self.obs.get_client_configuration()
        )
