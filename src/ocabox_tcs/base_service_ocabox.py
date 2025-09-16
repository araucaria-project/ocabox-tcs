from abc import ABC

from ocaboxapi import ClientAPI

from ocabox_tcs.base_service import BaseService, BaseServiceConfig


class BaseOCABoxService(BaseService, ABC):
    """Base class for OCABox-specific services

    This class includes OCABox-client initialization for communication with OCABox-server (TIC).
    """

    def __init__(self, config: BaseServiceConfig):
        super().__init__(config)
        self.tic_client: ClientAPI | None = None

    async def start(self):

        # Initialize TIC client
        self.tic_client = ClientAPI(name=self.config.id)
        # await self.tic_client.get_client().connect()

        return await super().start()



