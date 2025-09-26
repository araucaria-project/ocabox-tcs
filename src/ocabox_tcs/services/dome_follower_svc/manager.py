from typing import Optional

from ocabox_tcs.services.dome_follower_svc.nats_conn import NatsConn


class Manager:

    def __init__(self, service = None, config = None):
        self.service = service
        self.config = config
        self.logger = self.service.logger
        self.nats_conn: Optional[NatsConn] = None
        super().__init__()

    async def start_comm(self):
        self.logger.info(f'Starting communication.')
        self.nats_conn = NatsConn(manager=self)
        await self.nats_conn.connect()

    async def stop_comm(self):
        self.logger.info(f'Stopping communication.')
        await self.nats_conn.close()