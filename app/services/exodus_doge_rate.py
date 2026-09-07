"""Shared native Dogecoin provider pacing across this node's wallet workers."""
import asyncio
import fcntl
import math
import os
from pathlib import Path
import time

from app.services.exodus_send_service import SendRefused

INTERVAL=.4  # BlockCypher's public per-second quota is three requests.
MAX_QUEUE=20


def reserve(now=None):
    now=time.monotonic() if now is None else now
    folder=Path(os.environ.get('EXODUS_TRANSFER_DIR','data/exodus-transfers')).resolve()
    folder.mkdir(parents=True,mode=0o700,exist_ok=True)
    fd=os.open(folder/'dogecoin-rate',os.O_RDWR|os.O_CREAT,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        try:
            previous=float(os.read(fd,100).decode() or '0')
        except (ValueError,UnicodeError):
            previous=0
        # A machine reboot resets monotonic time. Discard a reservation from another boot.
        if not math.isfinite(previous) or previous>now+60:
            previous=0
        slot=max(now,previous)
        if slot-now>MAX_QUEUE:
            raise SendRefused('The Dogecoin provider is busy; try again shortly')
        os.lseek(fd,0,os.SEEK_SET);os.write(fd,str(slot+INTERVAL).encode());os.ftruncate(fd,os.lseek(fd,0,os.SEEK_CUR))
        return slot-now
    finally:
        os.close(fd)


async def pace():
    await asyncio.sleep(await asyncio.to_thread(reserve))
