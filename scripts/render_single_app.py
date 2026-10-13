"""One application process, with a private worker listener and public gateway."""
import asyncio
from contextlib import nullcontext
import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(ALBATROS_CLOUD_MODE='true', ALBATROS_GATEWAY_ONLY='false',
                  ALBATROS_INTERNAL_WORKER_API='true', ALBATROS_INTERNAL_BRIDGE='true',
                  RECEPTION_BRIDGE_URL='http://127.0.0.1:8010')

import uvicorn
from cloud_app import backend, CloudGateway


class OwnedServer(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()  # One coordinated signal handler for both listeners.


async def main():
    core = OwnedServer(uvicorn.Config(backend, host='127.0.0.1', port=8010,
                                    lifespan='on', proxy_headers=False))
    gateway = OwnedServer(uvicorn.Config(CloudGateway(backend), host='0.0.0.0',
        port=int(os.getenv('PORT', '10000')), lifespan='off', proxy_headers=True,
        forwarded_allow_ips='*'))
    loop = asyncio.get_running_loop()
    def shutdown():
        core.should_exit = gateway.should_exit = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)
    await asyncio.gather(core.serve(), gateway.serve())


if __name__ == '__main__':
    asyncio.run(main())
