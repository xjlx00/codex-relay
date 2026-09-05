"""Bounded fair queue: one running request per user."""
import asyncio
from collections import OrderedDict,deque
from contextlib import asynccontextmanager

class QueueFull(Exception):pass

class Scheduler:
    def __init__(self,capacity=2,limit=16):
        self.capacity=capacity; self.limit=limit; self.waiting=OrderedDict(); self.active=set(); self.last=None
    def pump(self):
        while len(self.active)<self.capacity:
            choices=[u for u,q in self.waiting.items() if q and u not in self.active]
            if not choices:return
            uid=next((u for u in choices if u!=self.last),choices[0]); fut=self.waiting[uid].popleft()
            if not self.waiting[uid]:del self.waiting[uid]
            else:self.waiting.move_to_end(uid)
            if fut.cancelled():continue
            self.active.add(uid); self.last=uid; fut.set_result(True)
    @asynccontextmanager
    async def slot(self,uid,timeout):
        if sum(map(len,self.waiting.values()))>=self.limit:raise QueueFull()
        fut=asyncio.get_running_loop().create_future(); self.waiting.setdefault(uid,deque()).append(fut); self.pump()
        granted=False
        try:
            await asyncio.wait_for(asyncio.shield(fut),timeout); granted=True; yield
        finally:
            if granted or (fut.done() and not fut.cancelled()):self.active.discard(uid)
            else:
                fut.cancel(); q=self.waiting.get(uid)
                if q is not None:
                    try:q.remove(fut)
                    except ValueError:pass
                    if not q:del self.waiting[uid]
            self.pump()
