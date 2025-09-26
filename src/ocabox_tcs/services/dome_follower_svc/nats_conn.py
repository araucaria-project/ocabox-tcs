from typing import List, Dict, Callable, Any, Tuple, Optional

from serverish.messenger import Messenger, single_publish, single_read, get_callbacksubscriber, get_journalpublisher
from serverish.messenger.msg_journal_pub import MsgJournalPublisher
from serverish.messenger.msg_rpc_resp import get_rpcresponder



class NatsConn:
    """
    Class is responsible for nats server connections
    """

    def __init__(self, manager = None):
        self.manager = manager
        self.messenger: Optional[Messenger] = None
        self.connected: bool = False
        super().__init__()

    async def connect(self) -> None:
        self.messenger = Messenger()
        host = '192.168.8.140'
        port = 4222
        self.manager.logger.info(f'Trying connect to nats: {host}:{port}')
        await self.messenger.open(host=host, port=port)
        self.manager.logger.info(f'Nats connected to {host}:{port}')
        self.connected = True

    async def msg_rpc_responder(self, subject: str, callb: Callable):
        rpc = get_rpcresponder(subject=subject)
        await rpc.register_function(callback=callb)
        self.manager.logger.info(f'Rpc responder for {subject} started')

    async def close(self) -> None:
        if self.connected:
            await self.messenger.close()
            self.connected = False
            self.manager.logger.info(f'Nats disconnected.')