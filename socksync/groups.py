from abc import ABC, abstractmethod
from threading import Lock
from typing import Set, cast, TypeVar, Generic

from django.db.models.signals import post_save

_SockSyncSocket = 'SockSyncSocket'
_SockSyncConsumer = 'SockSyncConsumer'


class SockSyncGroup(ABC):
    _subscriber_sockets: Set[_SockSyncSocket] = set()

    def __init__(self, name: str, type_: str):
        self._name: str = name
        self._type: str = type_

    @property
    def name(self) -> str:
        return self._name

    @property
    def type(self) -> str:
        return self._type

    # TODO: Hide these somehow
    @abstractmethod
    def handle_func(self, func: str, data: dict = None, socket: _SockSyncSocket = None) -> dict:
        pass

    def add_subscriber_socket(self, socket: _SockSyncSocket):
        self._subscriber_sockets.add(socket)

    def remove_subscriber_socket(self, socket: _SockSyncSocket):
        self._subscriber_sockets.remove(socket)

    def send_json_to_all(self, data: dict):
        for socket in self._subscriber_sockets:
            cast(_SockSyncConsumer, socket).send_json(data)

    def send_json_to_others(self, data: dict, self_socket: _SockSyncSocket):
        for socket in self._subscriber_sockets:
            if socket != self_socket:
                cast(_SockSyncConsumer, socket).send_json(data)

    def to_json(self) -> dict:
        return {
            "type": self._type,
            "name": self.name
        }


class SockSyncList(SockSyncGroup):
    value = []

    def get(self, index: int) -> object:
        return self.value[index]

    def set(self, index: int, value: object):
        self.value[index] = value
        # TODO: Update subscribers


class SockSyncModelList(SockSyncList):
    def __init__(self, model):
        self.model = model
        post_save.connect(self._model_post_save, sender=model)

    def get(self, index: int) -> object:
        pass

    def set(self, index: int, value: object):
        pass

    def _model_post_save(self):
        pass


T = TypeVar('T')


class SockSyncVariable(SockSyncGroup, Generic[T]):
    def __init__(self, name: str, value: T = None):
        super().__init__(name, "var")
        self._value: T = value
        self._lock: Lock = Lock()

    @property
    def value(self) -> T:
        with self._lock:
            return self._value

    @value.setter
    def value(self, new_value: T):
        with self._lock:
            self._value = new_value
        self.send_json_to_all(self.handle_func("get"))

    # TODO: Type checking
    def handle_func(self, func: str, data: dict = None, socket: _SockSyncSocket = None) -> dict:
        if func == "get" and (socket is None or socket.subscribed(self)):
            return dict(func="set", value=self.value, **self.to_json())
        elif func == "set" and "value" in data and socket.subscribed(self):
            with self._lock:
                self._value = data["value"]
            self.send_json_to_others(self.handle_func("get"), socket)
