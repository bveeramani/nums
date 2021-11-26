# coding=utf-8
# Copyright (C) 2020 NumS Development Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import warnings
import logging
from types import FunctionType
from typing import Any, Union, List, Dict, Optional

import ray

from nums.core.grid.grid import DeviceID
from nums.core.systems.system_interface import SystemInterface
from nums.core.systems.utils import get_private_ip, get_num_cores
from nums.core import settings


# pylint: disable = unused-argument


class SerialSystem(SystemInterface):
    def __init__(self, num_cpus: Optional[int] = None):
        self.num_cpus = int(get_num_cores()) if num_cpus is None else num_cpus
        self._remote_functions: dict = {}
        self._actors: dict = {}

    def init(self):
        pass

    def shutdown(self):
        pass

    def put(self, value: Any, device_id: DeviceID):
        return value

    def get(self, object_ids: Union[Any, List]):
        return object_ids

    def remote(self, function: FunctionType, remote_params: dict):
        return function

    def devices(self):
        return [DeviceID(0, "localhost", "cpu", 0)]

    def register(self, name: str, func: callable, remote_params: dict = None):
        if name in self._remote_functions:
            return
        if remote_params is None:
            remote_params = {}
        self._remote_functions[name] = self.remote(func, remote_params)

    def call(self, name: str, args, kwargs, device_id: DeviceID, options: Dict):
        return self._remote_functions[name](*args, **kwargs)

    def register_actor(self, name: str, cls: type):
        if name in self._actors:
            warnings.warn(
                "Actor %s has already been registered. "
                "Overwriting with %s." % (name, cls.__name__)
            )
            return
        self._actors[name] = cls

    def make_actor(self, name: str, *args, device_id: DeviceID = None, **kwargs):
        return self._actors[name](*args, **kwargs)

    def call_actor_method(self, actor, method: str, *args, **kwargs):
        return getattr(actor, method)(*args, **kwargs)

    def num_cores_total(self) -> int:
        return self.num_cpus


class MPITargetRank(object):
    def __init__(self, rank: int):
        self._target_rank = rank

    def set_target_rank(self, rank: int):
        self._target_rank = rank

    def get_target_rank(self):
        return self._target_rank


class MPISystem(SystemInterface):
    """
    Implements SystemInterface for MPI.
    """

    def __init__(self):
        # pylint: disable=import-outside-toplevel c-extension-no-member
        from mpi4py import MPI
        import collections

        self.comm = MPI.COMM_WORLD
        self.size = self.comm.Get_size()
        self.rank = self.comm.Get_rank()
        self.proc_name: str = get_private_ip()

        self._devices: List[DeviceID] = []

        self._remote_functions: dict = {}
        self._actors: dict = {}

        # This is same as number of mpi processes.
        self.num_cpus = self.size
        self._num_nodes: int = None

        self._device_to_node: Dict[DeviceID, int] = {}
        self.node_ranks_dict: Dict[str, list] = collections.defaultdict(list)
        self._actor_node_index = 0

    def init(self):
        self.init_devices()

    def init_devices(self):
        # Do an all-gather and collect processor names and rank info.
        node_names = []
        self.comm.Allgather({self.proc_name: self.rank}, node_names)
        # Create a dict with node as key and local ranks as value.
        for name in node_names:
            for k, v in name.items():
                self.node_ranks_dict[k].append(v)
        self._num_nodes = len(self.node_ranks_dict)
        self._devices = []
        for node in range(self._num_nodes):
            did = DeviceID(node, self.proc_name, "cpu", self.rank)
            self._devices.append(did)
            self._device_to_node[did] = node

    def shutdown(self):
        pass

    def put(self, value: Any, device_id: DeviceID):
        target_rank = self._device_to_node[device_id]
        if self.rank == target_rank:
            return value
        else:
            return MPITargetRank(target_rank)

    def get(self, object_ids: Union[Any, List]):
        resolved_object_ids = []
        for obj in object_ids:
            if isinstance(obj, MPITargetRank):
                target_rank = obj.get_target_rank()
            # This should be true for just one rank which has the data.
            else:
                target_rank = self.rank
            # TODO: see if all-2-all might be more efficient.
            obj = self.comm.bcast(obj, root=target_rank)
            resolved_object_ids.append(obj)
        return resolved_object_ids

    def remote(self, function: FunctionType, remote_params: dict):
        return function

    def devices(self) -> List[DeviceID]:
        return self._devices

    def register(self, name: str, func: callable, remote_params: dict = None):
        if name in self._remote_functions:
            return
        if remote_params is None:
            remote_params = {}
        self._remote_functions[name] = self.remote(func, remote_params)

    def call(self, name: str, args, kwargs, device_id: DeviceID, options: Dict):
        target_rank = self._device_to_node[device_id]
        resolved_args = self._resolve_args(args)
        if target_rank == self.rank:
            return self._remote_functions[name](*resolved_args, **kwargs)

    def _resolve_args(self, args):
        # Resolve dependencies: iterate over args and figure out which ones need fetching.
        resolved_args = []
        for arg in args:
            # Check if arg is remote.
            if isinstance(arg, MPITargetRank):
                target_rank = arg.get_target_rank()
                if target_rank == self.rank:
                    # TODO: Try Isend and Irecv and have a switch for sync and async.
                    arg = self.comm.recv(target_rank)
            # This should be true for just one rank.
            elif target_rank != self.rank:
                self.comm.send(arg, target_rank)
            resolved_args.append(arg)
        self.comm.barrier()
        return resolved_args

    def register_actor(self, name: str, cls: type):
        if name in self._actors:
            warnings.warn(
                "Actor %s has already been registered. "
                "Overwriting with %s." % (name, cls.__name__)
            )
            return
        self._actors[name] = cls

    def make_actor(self, name: str, *args, device_id: DeviceID = None, **kwargs):
        # Resolve args.
        resolved_args = self._resolve_args(args)
        # Distribute actors round-robin over devices.
        if device_id is None:
            device_id = self._devices[self._actor_node_index]
            self._actor_node_index = (self._actor_node_index + 1) % len(self._devices)
        actor = self._actors[name]
        target_rank = self._device_to_node[device_id]
        if target_rank == self.rank:
            return actor(*resolved_args, **kwargs)
        else:
            return MPITargetRank(target_rank)

    def call_actor_method(self, actor, method: str, *args, **kwargs):
        # Resolve args.
        resolved_args = self._resolve_args(args)
        # Make sure it gets called on the correct rank.
        if not isinstance(actor, MPITargetRank):
            return getattr(actor, method)(*resolved_args, **kwargs)

    def num_cores_total(self) -> int:
        return self.num_cpus


class RaySystem(SystemInterface):
    # pylint: disable=abstract-method
    """
    Implements SystemInterface for Ray.
    """

    def __init__(
        self,
        use_head: bool = False,
        num_nodes: Optional[int] = None,
        num_cpus: Optional[int] = None,
    ):
        self._use_head = use_head
        self._num_nodes = num_nodes
        self.num_cpus = int(get_num_cores()) if num_cpus is None else num_cpus
        self._manage_ray = True
        self._remote_functions = {}
        self._actors: dict = {}
        self._actor_node_index = 0
        self._available_nodes = []
        self._head_node = None
        self._worker_nodes = []
        self._devices: List[DeviceID] = []
        self._device_to_node: Dict[DeviceID, Dict] = {}

    def init(self):
        if ray.is_initialized():
            self._manage_ray = False
        if self._manage_ray:
            ray.init(num_cpus=self.num_cpus)
        # Compute available nodes, based on CPU resource.
        if settings.head_ip is None:
            # TODO (hme): Have this be a class argument vs. using what's set in settings directly.
            logging.getLogger(__name__).info("Using driver node ip as head node.")
            head_ip = get_private_ip()
        else:
            head_ip = settings.head_ip
        total_cpus = 0
        nodes = ray.nodes()
        for node in nodes:
            node_ip = self._node_ip(node)
            if head_ip == node_ip:
                logging.getLogger(__name__).info("head node %s", node_ip)
                self._head_node = node
            elif self._has_cpu_resources(node):
                logging.getLogger(__name__).info("worker node %s", node_ip)
                total_cpus += node["Resources"]["CPU"]
                self._worker_nodes.append(node)
                self._available_nodes.append(node)
        if self._head_node is None:
            if self._use_head:
                logging.getLogger(__name__).warning(
                    "Failed to determine which node is the head."
                    " The head node will be used even though"
                    " nums.core.settings.use_head = False."
                )
        elif self._use_head and self._has_cpu_resources(self._head_node):
            total_cpus += self._head_node["Resources"]["CPU"]
            self._available_nodes.append(self._head_node)
        logging.getLogger(__name__).info("total cpus %s", total_cpus)

        if self._num_nodes is None:
            self._num_nodes = len(self._available_nodes)
        assert self._num_nodes <= len(self._available_nodes)

        self.init_devices()

    def init_devices(self):
        self._devices = []
        for node_id in range(self._num_nodes):
            node = self._available_nodes[node_id]
            did = DeviceID(node_id, self._node_key(node), "cpu", 1)
            self._devices.append(did)
            self._device_to_node[did] = node

    def _has_cpu_resources(self, node: dict) -> bool:
        return self._node_cpu_resources(node) > 0.0

    def _node_cpu_resources(self, node: dict) -> float:
        return node["Resources"]["CPU"] if "CPU" in node["Resources"] else 0.0

    def _node_key(self, node: dict) -> str:
        node_key = list(filter(lambda key: "node" in key, node["Resources"].keys()))
        assert len(node_key) == 1
        return node_key[0]

    def _node_ip(self, node: dict) -> str:
        return self._node_key(node).split(":")[1]

    def shutdown(self):
        if self._manage_ray:
            ray.shutdown()

    def warmup(self, n: int):
        # Quick warm-up. Useful for quick and more accurate testing.
        if n > 0:
            assert n < 10 ** 6

            def warmup_func(n):
                # pylint: disable=import-outside-toplevel
                import random

                r = ray.remote(num_cpus=1)(lambda x, y: x + y).remote

                num_devices = len(self._devices)
                for i in range(n):
                    _a = random.randint(0, 1000)
                    _b = random.randint(0, 1000)
                    d0 = i % num_devices
                    d1 = (i + 1) % num_devices
                    _v = self.get(
                        r(
                            self.put(_a, self._devices[d0]),
                            self.put(_b, self._devices[d1]),
                        )
                    )

            warmup_func(n)

    def put(self, value: Any, device_id: DeviceID):
        return self.call("identity", [value], {}, device_id, {})

    def get(self, object_ids):
        return ray.get(object_ids)

    def remote(self, function: FunctionType, remote_params: dict):
        r = ray.remote(num_cpus=1, **remote_params)
        return r(function)

    def register(self, name: str, func: callable, remote_params: dict = None):
        if name in self._remote_functions:
            return
        self._remote_functions[name] = self.remote(func, remote_params)

    def call(self, name: str, args, kwargs, device_id: DeviceID, options: Dict):
        if device_id is not None:
            node = self._device_to_node[device_id]
            node_key = self._node_key(node)
            if "resources" in options:
                assert node_key not in options  # should it be options["resources"]?
            options["resources"] = {node_key: 1.0 / 10 ** 4}
        return self._remote_functions[name].options(**options).remote(*args, **kwargs)

    def devices(self) -> List[DeviceID]:
        return self._devices

    def num_cores_total(self) -> int:
        num_cores = sum(
            map(lambda n: n["Resources"]["CPU"], self._device_to_node.values())
        )
        return int(num_cores)

    def register_actor(self, name: str, cls: type):
        if name in self._actors:
            warnings.warn(
                "Actor %s has already been registered. "
                "Overwriting with %s." % (name, cls.__name__)
            )
            return
        self._actors[name] = ray.remote(cls)

    def make_actor(self, name: str, *args, device_id: DeviceID = None, **kwargs):
        # Distribute actors round-robin over devices.
        if device_id is None:
            device_id = self._devices[self._actor_node_index]
            self._actor_node_index = (self._actor_node_index + 1) % len(self._devices)
        actor = self._actors[name]
        node = self._device_to_node[device_id]
        node_key = self._node_key(node)
        options = {"resources": {node_key: 1.0 / 10 ** 4}}
        return actor.options(**options).remote(*args, **kwargs)

    def call_actor_method(self, actor, method: str, *args, **kwargs):
        return getattr(actor, method).remote(*args, **kwargs)


class RaySystemStockScheduler(RaySystem):
    """
    An implementation of the Ray system which ignores scheduling commands given
    by the caller. For testing only.
    """

    def call(self, name: str, args, kwargs, device_id: DeviceID, options: Dict):
        if device_id is not None:
            node = self._device_to_node[device_id]
            node_key = self._node_key(node)
            if "resources" in options:
                assert node_key not in options
        return self._remote_functions[name].options(**options).remote(*args, **kwargs)

    def make_actor(self, name: str, *args, device_id: DeviceID = None, **kwargs):
        actor = self._actors[name]
        return actor.remote(*args, **kwargs)
