from abc import ABC, abstractmethod
from scout.logger import get_logger


class Worker(ABC):
    def __init__(self, id):
        self._id = id
        self._logger = get_logger(f"{type(self).__name__}_{id}")
        self._running = False

    @abstractmethod
    async def start(self): ...

    @abstractmethod
    async def stop(self): ...

    @abstractmethod
    async def cancel(self): ...

    @abstractmethod
    async def complete(self, *args, **kwargs): ...

    @abstractmethod
    async def error(self, *args, **kwargs): ...
