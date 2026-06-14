from abc import ABC, abstractmethod


class Queue(ABC):
    @abstractmethod
    def init(self): ...

    @abstractmethod
    def push(self): ...

    @abstractmethod
    def pop(self): ...

    def __iter__(self):
        return self

    def __next__(self):
        item = self.pop()
        if not item:
            raise StopIteration
        return item


def init_queues():
    from .front_queue import FrontQueue
    from .back_queue import BackQueue
    from .scheduler_queue import SchedulerQueue

    front_queue = FrontQueue()
    front_queue.init()

    back_queue = BackQueue()
    back_queue.init(list(front_queue))

    scheduler_queue = SchedulerQueue()
    scheduler_queue.init(back_queue.get_hostnames())

    return front_queue, back_queue, scheduler_queue
