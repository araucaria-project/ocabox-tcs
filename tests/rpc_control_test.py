import asyncio
from serverish.messenger import Messenger, request
from serverish.base.exceptions import MessengerRequestNoResponders, MessengerRequestNoResponse, MessengerRequestTimeout


async def req():
    host = '192.168.8.140'  # TODO take from config
    port = 4222  # TODO take from config
    async with Messenger().context(host=host, port=port):
        try:
            # dat, met = await request(subject=f'tic.rpc.dev.dome.follower.state')
            # dat, met = await request(subject=f'tic.rpc.dev.dome.follower.off')
            dat, met = await request(subject=f'tic.rpc.dev.dome.follower.on')
            if dat:
                try:
                    if dat['status'] == 'ok':
                        print(dat)
                except KeyError:
                    print(False)
        except (MessengerRequestNoResponders, MessengerRequestNoResponse, MessengerRequestTimeout):
            print(False)

async def run():
    ts1 = asyncio.create_task(req())
    await asyncio.gather(ts1)


asyncio.run(run())
