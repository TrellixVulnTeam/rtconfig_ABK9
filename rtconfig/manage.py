import os
import json
import attr
import logging
from rtconfig.utils import to_hash, strftime, convert_dt, OSUtils
from rtconfig.config import STORE_DIRECTORY
from rtconfig.backend import BaseBackend, JsonFileBackend
from rtconfig.exceptions import ProjectNoFoundException

MT_NO_CHANGE = 'nochange'
MT_CHANGED = 'changed'
connected = {}
connected_message = {}
config_store_state = {}
extension_backend = {
    '.json': JsonFileBackend
}
logger = logging.getLogger(__name__)


@attr.s
class Message:
    request = attr.ib()
    message_type = attr.ib(validator=attr.validators.instance_of(str))
    config_name = attr.ib(validator=attr.validators.instance_of(str))
    hash_code = attr.ib(validator=attr.validators.instance_of(str))
    data = attr.ib(default=dict(), validator=attr.validators.instance_of(dict))
    context = attr.ib(default=dict(), validator=attr.validators.instance_of(dict))
    lut = attr.ib(default=None, converter=convert_dt)

    def to_dict(self, indent=0):
        context = self.context
        context.update(
            headers=dict(self.request.headers)
        )
        return dict(
            message_type=self.message_type,
            config_name=self.config_name,
            hash_code=self.hash_code,
            data=self.data,
            context=json.dumps(context, indent=indent) if indent else context,
            lut=strftime(self.lut)
        )

    def to_string(self):
        return json.dumps(dict(
            message_type=self.message_type,
            config_name=self.config_name,
            hash_code=self.hash_code,
            data=self.data,
        ))


class ConfigManager:
    _backend_class = JsonFileBackend
    _notify_handler = set()

    def __init__(self, config_name, backend_class=None, notify_handler=None,
                 store_directory=None):
        if backend_class is not None:
            self._backend_class = backend_class
        assert issubclass(self._backend_class, BaseBackend)
        self.config_name = config_name
        self.store_directory = store_directory or STORE_DIRECTORY
        self.store_backend = self._backend_class(
            self.store_directory, self.config_name)
        self._source_data = {}
        self.notify_handler = notify_handler
        self.refresh()

    def refresh(self):
        self._source_data = self.store_backend.read()

    @property
    def source_data(self):
        return self._source_data

    @source_data.setter
    def source_data(self, value):
        self._source_data = value

    @property
    def hash_code(self):
        return to_hash(self._source_data)

    async def update_config(self):
        self.store_backend.write(self.source_data)
        self.refresh()
        for handler in self._notify_handler:
            await handler(self)

    @classmethod
    def notify(cls, func):
        cls._notify_handler.add(func)
        return func

    def config_message(self, request):
        return Message(
            request,
            MT_CHANGED,
            self.config_name,
            self.hash_code,
            self.source_data
        ).to_string()

    def display_info(self):
        return dict(
            config_name=self.config_name,
            connect_num=len(connected.get(self.config_name) or []),
            source_data=json.dumps(self.source_data, indent=4)
        )


def get_config_store(config_name):
    try:
        return config_store_state[config_name]
    except KeyError:
        raise ProjectNoFoundException(config_name=config_name)


def init_config_store():
    global config_store_state
    config_store_state = {}
    os_utils = OSUtils()
    store_path = os_utils.abspath(STORE_DIRECTORY)
    if not os_utils.directory_exists(store_path):
        os_utils.makedirs(store_path)
    for _, _, file_names in OSUtils().walk(store_path):
        for file_name in file_names:
            config_name, extension = os.path.splitext(file_name)
            if extension not in extension_backend:
                continue
            config_store_state[config_name] = ConfigManager(config_name)
            connected.setdefault(config_name, set())
            logger.info('Load config store object: %s', config_name)


def register_connected_ws(message, ws):
    try:
        if ws not in connected[message.config_name]:
            connected[message.config_name].add(ws)
        connected_message[ws] = message
    except KeyError:
        raise ProjectNoFoundException(config_name=message.config_name)


def remove_connected_ws(config_name, ws):
    try:
        connected[config_name].remove(ws)
    except KeyError:
        pass
    try:
        del connected_message[ws]
    except KeyError:
        pass


@ConfigManager.notify
async def notify_changed(config_store):
    config_store_state[config_store.config_name] = config_store
    clients = connected.get(config_store.config_name) or []
    for client in clients:
        message = connected_message.get(client)
        if not (message and message.config_name == config_store.config_name):
            continue
        if config_store.hash_code != message.hash_code:
            continue
        await client.send(config_store.config_message(message.request))


init_config_store()
