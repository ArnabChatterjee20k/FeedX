from typing import Protocol
from .front_queue import FrontQueue


class Queue(Protocol):
    def init(self) -> None: ...

    def push(self) -> None: ...

    def pop(self) -> None: ...


def init_queues():
    front_queue = FrontQueue()
    front_queue.init()
