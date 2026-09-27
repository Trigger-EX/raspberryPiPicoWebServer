# Runs PicoMail with a test config under CPython or the MicroPython unix port.
# usage: run_server.py <config-dir> <src-dir>
import sys

sys.path.insert(0, sys.argv[2])
sys.path.insert(0, sys.argv[1])

import asyncio

import config
from picomail import app


async def main():
    await app.start(config)
    try:
        import gc
        gc.collect()
        print("MEM after start: alloc=%d free=%d" % (gc.mem_alloc(), gc.mem_free()))
    except AttributeError:
        pass
    print("READY")
    while True:
        await asyncio.sleep(1)


asyncio.run(main())
