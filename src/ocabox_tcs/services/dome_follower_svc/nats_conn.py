from typing import List, Dict, Callable, Any, Tuple, Optional

from serverish.base import dt_utcnow_array
from serverish.messenger import Messenger, single_read
from serverish.messenger.msg_rpc_resp import get_rpcresponder, Rpc


class NatsConn:
    """
    Class is responsible for nats server connections
    """

    def __init__(self, manager = None):
        self.manager = manager
        self.svc_logger = self.manager.svc_logger
        self.messenger: Optional[Messenger] = None
        self.messenger_self_managed = False
        self.connected: bool = False
        super().__init__()

    async def connect(self) -> None:
        self.messenger = Messenger()
        if not self.messenger.is_open:
            # self.svc_logger.info(f'Nats not opened, connecting...')
            # host = '192.168.8.140' #TODO take from config
            # port = 4222 #TODO take from config
            # self.svc_logger.info(f'Trying connect to nats: {host}:{port}')
            # await self.messenger.open(host=host, port=port)
            # self.svc_logger.info(f'Nats connected to {host}:{port}')
            # self.connected = True
            # self.messenger_self_managed = True
            self.svc_logger.error('Messenger/NATS not ready!')
            exit(1)
        else:
            self.svc_logger.info(f'Nats already connected')
            self.connected = True

    async def rpc_follow_on(self, rpc: Rpc) -> None:
        self.svc_logger.info(f'Follow on rpc request received')
        self.manager.follow_on = True
        data = {
            'response': 'Dome follower - follow turned on',
            'status': 'ok',
            'ts': dt_utcnow_array(),
            }
        meta = {
            "message_type": "rpc",  # IMPORTANT type message, one of pre declared types
            'sender': self.manager.software_id  # name who send message
        }
        await rpc.response_now(data=data, meta=meta)
        self.svc_logger.info(f'Sent rpc response: follow on')

    async def rpc_follow_off(self, rpc: Rpc) -> None:
        self.svc_logger.info(f'Follow off rpc request received')
        self.manager.follow_on = False
        data = {
            'response': 'Dome follower - follow turned off',
            'status': 'ok',
            'ts': dt_utcnow_array(),
            }
        meta = {
            "message_type": "rpc",  # IMPORTANT type message, one of pre declared types
            'sender': self.manager.software_id  # name who send message
        }
        await rpc.response_now(data=data, meta=meta)
        self.svc_logger.info(f'Sent rpc response: follow off')

    async def rpc_state(self, rpc: Rpc) -> None:
        self.svc_logger.info(f'State rpc request received')
        data = {
            'response': 'State of the dome follower',
            'follow_on': self.manager.follow_on,
            'status': 'ok',
            'ts': dt_utcnow_array(),
            }
        meta = {
            "message_type": "rpc",  # IMPORTANT type message, one of pre declared types
            'sender': self.manager.software_id  # name who send message
        }
        await rpc.response_now(data=data, meta=meta)
        self.svc_logger.info(f'Sent rpc response: status')

    async def start_responders(self) -> None:
        await self.msg_rpc_responder(
            subject=f'tic.rpc.{self.manager.tic_conn.telescope.id}.dome.follower.on',
            callb=self.rpc_follow_on
        ) #TODO take rpc  from obs settings
        await self.msg_rpc_responder(
            subject=f'tic.rpc.{self.manager.tic_conn.telescope.id}.dome.follower.off',
            callb=self.rpc_follow_off
        ) #TODO take rpc  from obs settings
        await self.msg_rpc_responder(
            subject=f'tic.rpc.{self.manager.tic_conn.telescope.id}.dome.follower.state',
            callb=self.rpc_state
        ) #TODO take rpc  from obs settings
        self.svc_logger.info(f"All responders started")

    async def msg_rpc_responder(self, subject: str, callb: Callable):
        rpc = get_rpcresponder(subject=subject)
        await rpc.register_function(callback=callb)
        self.svc_logger.info(f'Rpc responder for {subject} started')

    async def close(self) -> None:
        if self.connected and self.messenger_self_managed:
            await self.messenger.close()
            self.connected = False
            self.svc_logger.info(f'Nats disconnected.')

    async def get_obs_cfg(self) -> Dict:
        _obs_cfg, meta = await single_read('tic.config.observatory', wait=10)
        self.svc_logger.info(f'Obtained Observatory Config. Published: {_obs_cfg["published"]}')
        return _obs_cfg
