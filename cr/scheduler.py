"""Bounded fair queue with global and adjustable per-user concurrency."""
import asyncio
from collections import OrderedDict,deque
from contextlib import asynccontextmanager

class QueueFull(Exception):pass

class Scheduler:
    def __init__(self,capacity=6,limit=16):
        self.capacity=capacity; self.limit=limit
        self.waiting=OrderedDict(); self.running={}; self.user_limits={}

    @property
    def active(self):return set(self.running)

    def set_limit(self,uid,limit):
        if type(limit) is not int or not 1<=limit<=6:
            raise ValueError('Concurrent limit must be an integer from 1 to 6')
        self.user_limits[uid]=limit; self.pump()

    def stats(self,uid=None):
        if uid is None:
            return {'running':sum(self.running.values()),'limit':self.capacity,
                    'waiting':sum(map(len,self.waiting.values()))}
        return {'running':self.running.get(uid,0),'limit':self.user_limits.get(uid,1),
                'waiting':len(self.waiting.get(uid,()))}

    def available(self,uid):
        return (sum(self.running.values())<self.capacity and
                self.running.get(uid,0)<self.user_limits.get(uid,1))

    def grant(self,uid,fut):
        self.running[uid]=self.running.get(uid,0)+1; fut.set_result(True)

    def pump(self):
        while sum(self.running.values())<self.capacity:
            uid=next((u for u,q in self.waiting.items() if q and self.available(u)),None)
            if uid is None:return
            fut=self.waiting[uid].popleft()
            if not self.waiting[uid]:del self.waiting[uid]
            else:self.waiting.move_to_end(uid)
            if fut.cancelled():continue
            self.grant(uid,fut)

    @asynccontextmanager
    async def slot(self,uid,timeout):
        self.pump()
        fut=asyncio.get_running_loop().create_future()
        if self.available(uid):self.grant(uid,fut)
        else:
            if sum(map(len,self.waiting.values()))>=self.limit:raise QueueFull()
            self.waiting.setdefault(uid,deque()).append(fut)
        try:
            # wait() leaves the grant future intact and does not swallow a
            # cancellation racing with a newly granted slot.
            done,_=await asyncio.wait((fut,),timeout=timeout)
            if not done:raise asyncio.TimeoutError()
            yield
        finally:
            if fut.done() and not fut.cancelled():
                self.running[uid]-=1
                if not self.running[uid]:del self.running[uid]
            else:
                fut.cancel(); q=self.waiting.get(uid)
                if q is not None:
                    try:q.remove(fut)
                    except ValueError:pass
                    if not q:del self.waiting[uid]
            self.pump()
