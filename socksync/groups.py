import time
from abc import ABC, abstractmethod
from threading import Lock, Thread
from typing import Set, cast, TypeVar, Generic, Callable, Dict, Optional, List
from uuid import uuid4

from django.core.paginator import Paginator
from django.db.models import Model, QuerySet
from django.db.models.signals import post_save, post_delete

_SockSyncSocket = 'SockSyncSocket'
_SockSyncConsumer = 'SockSyncConsumer'


class SockSyncGroup(ABC):
    def __init__(self, name: str, type_: str):
        self._name: str = name
        self._type: str = type_

        self._subscriber_sockets: Set[_SockSyncSocket] = set()
        self._subscribable = True

    @property
    def name(self) -> str:
        return self._name

    @property
    def type(self) -> str:
        return self._type

    @abstractmethod
    def _handle_func(self, func: str, data: dict = None, socket: _SockSyncSocket = None) -> Optional[dict]:
        pass

    def _socket_subscribed(self, socket: _SockSyncSocket):
        self._subscriber_sockets.add(socket)

    def _socket_unsubscribed(self, socket: _SockSyncSocket):
        self._subscriber_sockets.remove(socket)

    def _send_json(self, data: dict, ignore_socket: _SockSyncSocket = None):
        for socket in self._subscriber_sockets:
            if socket != ignore_socket:
                cast(_SockSyncConsumer, socket).send_json(data)

    def _to_json(self) -> dict:
        return {
            "type": self._type,
            "name": self.name
        }


T = TypeVar('T')


class SockSyncVariable(SockSyncGroup, Generic[T]):
    def __init__(self, name: str, value: T = None):
        super().__init__(name, "var")
        self._value: T = value
        self._lock: Lock = Lock()

    def get(self) -> T:
        with self._lock:
            return self._value

    def set(self, new_value: T, ignore_socket: _SockSyncSocket = None):
        with self._lock:
            self._value = new_value
        self._send_json(self._handle_func("get"), ignore_socket)

    def _handle_func(self, func: str, data: dict = None, socket: _SockSyncSocket = None) -> Optional[dict]:
        # TODO: This is going through if socket is not subscribed
        if func == "get" and (socket is None or socket.subscribed(self)):
            return dict(func="set", value=self.get(), **self._to_json())
        elif func == "set" and "value" in data and socket.subscribed(self):
            self.set(data["value"], socket)


class SockSyncListItem(SockSyncGroup):
    def __init__(self, id_: str, value: any):
        super().__init__(id_, "list_item")
        self.id = id_
        self.value = value

    def _handle_func(self, func: str, data: dict = None, socket: _SockSyncSocket = None) -> Optional[dict]:
        pass

    def _to_json(self) -> dict:
        return dict(id=self.id, value=self.value)


class SockSyncList(SockSyncGroup):
    def __init__(self, name: str, page_size: int = 10):
        super().__init__(name, "list")
        self.page_size = page_size
        self._id_map: Dict[str, int] = {}
        self._values: List[SockSyncListItem] = []
        self._count = 0
        self._current_page = 0
        self._subscriber_pages: Dict[_SockSyncSocket, (int, int)] = {}

    def count(self):
        return self._count

    def get(self, id_: str) -> any:
        return self._values[self._id_map[id_]].value

    def insert(self, index: int, value: any, id_: str = None, ignore_socket: _SockSyncSocket = None):
        id_ = id_ or str(uuid4())
        self._values.insert(index, SockSyncListItem(id_, value))
        for i in range(index, len(self._values)):
            self._id_map[self._values[i].id] = i
        self._send_json_page(id_, dict(func="insert", id=id_, value=value, index=index, **self._to_json()),
                             ignore_socket)
        self._count += 1

    def append(self, value: any, id_: str = None):
        self.insert(len(self._values), value, id_)

    def set(self, id_: str, value: any, ignore_socket: _SockSyncSocket = None):
        self._values[self._id_map[id_]].value = value
        self._send_json_page(id_, self._handle_func("get", dict(id=id_, **self._to_json())), ignore_socket)

    def delete(self, id_: str, ignore_socket: _SockSyncSocket = None):
        index = self._id_map[id_]
        self._values.pop(index)
        self._id_map.pop(id_)
        for i in range(index, len(self._values) - 1):
            self._id_map[self._values[i].id] = i
        self._send_json_page(id_, dict(func="delete", id=id_, **self._to_json()), ignore_socket)
        self._count -= 1

    def __getitem__(self, item: int) -> any:
        return self._values[item].value

    def __setitem__(self, key: int, value: any):
        id_ = self._values[key].id
        self._values[key].value = value
        self._send_json_page(id_, self._handle_func("get", dict(id=id_)))

    def _send_json_page(self, id_: str, json: dict, ignore_socket: _SockSyncSocket = None):
        index = self._id_map[id_]
        for socket in self._subscriber_sockets:
            page, page_size = self._subscriber_pages[socket]
            if socket != ignore_socket and (page * page_size <= index < page * page_size + page_size):
                cast(_SockSyncConsumer, socket).send_json(json)

    def _socket_subscribed(self, socket: _SockSyncSocket):
        super()._socket_subscribed(socket)
        self._subscriber_pages[socket] = (0, self.page_size)

    def _socket_unsubscribed(self, socket: _SockSyncSocket):
        super()._socket_unsubscribed(socket)
        self._subscriber_pages.pop(socket, None)

    def _handle_func(self, func: str, data: dict = None, socket: _SockSyncSocket = None) -> Optional[dict]:
        # TODO: Send error at end of this if invalid (and for vars and functions)
        # TODO: Catch id key errors
        id_ = data.get("id", None)  # TODO: and subscribed
        if func == "get":
            page_size = min(self.page_size, data.get("page_size", self.page_size))
            paginator = Paginator(self._values, page_size)
            page = max(0, min(data.get("page", 0), paginator.num_pages - 1))
            self._subscriber_pages[socket] = (page, page_size)

            if id_ is None:
                return dict(func="set_all", **self._to_json(), page=page, total_item_count=self.count(),
                            items=[v._to_json() for v in paginator.get_page(page + 1)])
            else:
                if id_ in self._id_map:
                    return dict(func="set", id=id_, value=self._values[self._id_map[id_]].value, **self._to_json())
                else:
                    socket.send_name_error(self.type, self.name, id_)
                    return

        elif func == "set_all" and "items" in data:
            self._values = [SockSyncListItem(i["id"], i["value"]) for i in data["items"] if "id" in i and "value" in i]
            self._current_page = data["page"]
            self._count = data["total_item_count"]
            self._send_json(data, socket)
        elif func == "insert" and "index" in data and "value" in "data" and id_ is not None:
            self.insert(data["index"], data["value"], id_, socket)
        elif func == "set" and "value" in data and id_ is not None:
            self.set(id_, data["value"], socket)
        elif func == "delete" and id_ is not None:
            self.delete(id_, socket)


class SockSyncModelList(SockSyncList):
    def __init__(self, name: str, model: Model, query: QuerySet = None):
        super().__init__(name)
        self.model: Model = model
        self.query = query
        if query is None:
            self.query = model.objects.all()

        post_save.connect(self._model_post_save, sender=model)
        post_delete.connect(self._model_post_delete, sender=model)

    def _model_post_save(self, sender, **kwargs):
        print(kwargs)
        pass

    def _model_post_delete(self, sender, **kwargs):
        pass


class SockSyncFunction(SockSyncGroup, ABC):
    def __init__(self, name: str):
        super().__init__(name, "function")


class SockSyncFunctionRemote(SockSyncFunction):
    def __init__(self, name: str):
        super().__init__(name)
        self._subscribable = False
        self._returns: Dict[str, dict] = {}
        self._ignore_returns: Set[str] = set()

    def call_remote_blocking(self, socket: _SockSyncSocket, **kwargs):
        if not socket.subscribed(self):
            return None

        id_ = str(uuid4())
        json = dict(
            **self._to_json(),
            func="call",
            id=id_,
            args=kwargs)

        socket.send_json(json)
        while id_ not in self._returns:
            time.sleep(.25)

        return_data = self._returns[id_]
        self._returns.pop(id_)

        return return_data

    def call_remote_all(self, **kwargs):
        id_ = str(uuid4())
        json = dict(
            **self._to_json(),
            func="call",
            id=id_,
            args=kwargs)

        for socket in self._subscriber_sockets:
            self._ignore_returns.add(id_)
            socket.send_json(json)
            id_ = str(uuid4())
            json["id"] = id_

    def _handle_func(self, func: str, data: dict = None, socket: _SockSyncSocket = None) -> Optional[dict]:
        if "id" not in data and socket is not None:
            socket.send_general_error("id is required.")
            return None

        id_ = data["id"]

        if func == "call":
            return dict(**self._to_json(), func="return", id=id_, value="Function is not callable!")
        elif func == "return":
            if id_ in self._ignore_returns:
                self._ignore_returns.remove(id_)
            else:
                self._returns[id_] = data.get("value", None)


class SockSyncFunctionLocal(SockSyncFunction):
    def __init__(self, name: str, function: Callable = None):
        super().__init__(name)
        self.function = function

    def _handle_func(self, func: str, data: dict = None, socket: _SockSyncSocket = None) -> Optional[dict]:
        if "id" not in data and socket is not None:
            socket.send_general_error("id is required.")
            return None

        id_ = data["id"]

        if func == "call" and socket.subscribed(self):
            Thread(target=self._function_call_wrapper, args=(id_, data, socket)).start()

    def _function_call_wrapper(self, id_: str, data: dict, socket: _SockSyncSocket):
        socket.send_json(dict(**self._to_json(), func="return", id=id_, value=self.function(**data.get("args", {}))))
