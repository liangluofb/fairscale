# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

from threading import Condition
from types import TracebackType
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union, cast

import torch
from torch import Tensor, nn
from torch.autograd.profiler import record_function
from torch.distributed import rpc

from fairscale.nn.pipe import microbatch
from fairscale.nn.pipe.checkpoint import Checkpointing, TensorOrTensors
from fairscale.nn.pipe.dependency import fork, join
from fairscale.nn.pipe.microbatch import Batch
from fairscale.nn.pipe.stream import as_cuda, current_stream, is_cuda, use_device, use_stream
from fairscale.nn.pipe.worker import Task, create_workers

Device = Union[torch.device, int, str]

ExcInfo = Tuple[Type[BaseException], BaseException, TracebackType]


def check_pytorch_version() -> None:
    if torch.__version__.split("+")[0].split(".")[:2] < ["1", "9"]:
        raise Exception("DistributedPipeline requires PyTorch version 1.9 or higher")


def rloss(loss_func: Callable, input_rref: rpc.RRef, target_rref: rpc.RRef) -> rpc.RRef:
    return loss_func(input_rref.to_here(), target_rref.to_here())


def DistributedLoss(loss: nn.Module, *args: Tuple, **kwargs: Dict) -> Callable:
    loss_func = loss(*args, **kwargs)

    def dloss(input_rref: rpc.RRef, target_rref: rpc.RRef) -> rpc.RRef:
        return rpc.remote(input_rref.owner(), rloss, args=(loss_func, input_rref, target_rref))

    return dloss


class PipelineModule(nn.Module):
    """Constructs a module on a remote device, possibly at a later time (in case the device is not
    specified when creating PipelineModule.
    Args:
        module_cls (nn.Module): Class for the module to be created remotely.
        args (Sequence): args to be passed to ``module_cls``.
        kwargs (Dict, optional): kwargs to be passed to ``module_cls``.
        num_input (int): number of inputs to the forward function.
        num_outputs: (int, optional): If the forward function returns a tuple, number of elements
            in the tuple, otherwise it should be None
        remote_device: (str, optional): Device on the destination worker where we‘d like to place
            this module. The format should be "<workername>/<device>", where the device field can be
            parsed as torch.device type. E.g., "trainer0/cpu", "trainer0", "ps0/cuda:0".
            If this parameter can be provided later by calling the method instantiate
    """

    def __init__(
        self,
        module_cls: nn.Module,
        args: Tuple,
        kwargs: Optional[Dict] = None,
        num_inputs: int = 1,
        num_outputs: Optional[int] = None,
        remote_device: str = None,
    ):
        super().__init__()
        self.num_inputs = num_inputs
        self.num_outputs = num_outputs
        self.module_args = (module_cls, args, kwargs or {})
        if remote_device is not None:
            self.instantiate(remote_device)

    @staticmethod
    def _create_module(module_cls: Callable, args: Tuple, kwargs: Dict, device: str) -> nn.Module:
        result: nn.Module = module_cls(*args, **kwargs)
        result.to(device)
        return result

    def instantiate(self, remote_device: str) -> "PipelineModule":
        on, device = remote_device.split("/")
        self.on = on
        self.device = device
        self.module_rref = rpc.remote(on, PipelineModule._create_module, self.module_args + (device,))
        return self

    def get_module_rref(self) -> rpc.RRef:
        return self.module_rref


class PipelineModulesGraph(nn.Module):
    """A collection of remote modules (of type PipelineModule) with connections showing how inputs
    to the model or outputs of individual modules are use as inputs of subsequent modules.
    The graph has a number of helper functions that add new modules to the graph and define inputs
    to these module.
    """

    def __init__(self) -> None:
        super().__init__()
        self.modules_list: List = []
        self.inputs: List[Optional[List[Tuple[int, int]]]] = []

    def _add_new_module(self, num: int = 1) -> None:
        for i in range(num):
            self.inputs.append(None)

    def _find_or_add(self, module: PipelineModule) -> int:
        try:
            return self.modules_list.index(module)
        except ValueError:
            self._add_new_module()
            self.modules_list.append(module)
            return len(self.modules_list) - 1

    def add_sequence(self, modules: List[PipelineModule], first_input: Optional[PipelineModule] = None) -> None:
        """Adds a list of modules to the graph, to be run sequencially.
        The connection between these modules is as follows: the first output of each of these modules
        (except the last one) is used as the first input of its next module in this sequence.
        The user also may optionally specify the input to the first module in this sequence with
        argument 'first_input'. In this case the module 'first_input' must be added to the graph previously.
        """
        old_m = len(self.modules_list)
        m = len(modules)
        self._add_new_module(m)
        self.modules_list.extend(modules)
        for i in range(old_m + 1, old_m + m):
            self.inputs[i] = [(i - 1, 0)]
        if first_input is not None:
            self.inputs[old_m] = [(self.modules_list.index(first_input), 0)]

    def feed_model_input(self, module: PipelineModule, ind: int = 0) -> None:
        """Declares the input to a module as the input to the model. In case the model has multiple
        inputs, the argument 'ind' indicates the index of the model input that is fed to the module.
        """
        self.inputs[self._find_or_add(module)] = [(-1, ind)]

    def add_multi_input_layer(self, module: PipelineModule, inputs: List[PipelineModule]) -> None:
        """Adds a module with multiple inputs to the graph. The modules that provide inpurs to this module
        must be added previously to the graph and are listed with argument inputs.
        """
        self.inputs[self._find_or_add(module)] = [(self.modules_list.index(m), 0) for m in inputs]

    def fan_out(self, module: PipelineModule, outputs: List[PipelineModule]) -> None:
        """Feeds outputs of a previously added module to modules specified by argument 'outputs' (so
        'module' should have at least 'len(outputs)' outputs.
        Modules in the list 'outputs' are added to the graph if they have not been added previously.
        """
        mi = self.modules_list.index(module)
        for i, m in enumerate(outputs):
            self.inputs[self._find_or_add(m)] = [(mi, i)]

    def replicate_output(self, module: PipelineModule, outputs: List[PipelineModule]) -> None:
        """Feeds the first output of a previously added module to multiple modules specified by
        argument 'outputs'. Modules in the list 'outputs' are added to the graph if they have not
        been added previously.
        """
        mi = self.modules_list.index(module)
        for m in outputs:
            self.inputs[self._find_or_add(m)] = [(mi, 0)]

    def validate_graph(self) -> None:
        """Makes sure graph satisfies necessary requirements"""
        # TODO: implement following checks:
        #   * the graph has a least one module, and is connected.
        #   * num_inputs and num_outputs for modules matche list of connections defined in the graph.
        #   * all inputs to a module should come from model input, or modules with smaller index in
        #     the graph. This condition is used in implementaion of DistributedPipeline.forward. Even
        #     if we relax this condition, still need to make sure the graph is acyclic.
        pass

    def compute_output_users(self) -> None:
        """Precomputs self.model_input_users and self.output_users for internal use by the pipleine
        class. These two lists show consumers of inputs to the model, and outputs of each module of
        the graph. Each consumer is a pair (i, j) which stands for the j'th input to the i'th module
        in the graph.
        """
        m = len(self.modules_list)
        self.output_users: List[List[Tuple[int, int, int]]] = [[] for _ in range(m)]
        self.model_input_users = []
        for i, input in enumerate(self.inputs):
            assert input is not None
            for j, input_item in enumerate(input):
                if input_item[0] >= 0:
                    self.output_users[input_item[0]].append((i, j, input_item[1]))
                else:
                    self.model_input_users.append((i, j, input_item[1]))


class DistributedPipelineRecord:
    """ A class for storing a single mini-batch (consisting of multiple micro-batches) as input to
    a single partition.
    Args:
        device: the local device that runs the partition.
        rank: the rank of the partition in the pipeline.
        chunks: number of micro-batches in a mini-batch
        num_inputs: number of inputs to the partition.
        users: list of consumers of outputs of the partition. Each consumer in the list is a tuple
            (remote_partition_rref, input_idx, output_idx) where remote_partition_rref points to a
            remote DistributedPipelineRecord for consumer partiton for this mini-batch. The output number
            output_idx of this partition will be used as the input number input_idx of that partition.
    """

    def __init__(
        self, device: torch.device, rank: int, chunks: int, num_input: int, users: List[Tuple[rpc.RRef, int, int]]
    ) -> None:
        self.ready_cv = Condition()
        # Each chunk consists of num_input tensors. self.tensors stores these individual tensors.
        self.tensors: List[List[Optional[Tensor]]] = [[None] * num_input for _ in range(chunks)]
        # For each tensor in self.tensors, we record a cuda event in corrsponding tensorpipe stream in self.recv_events,
        # and later the stream that processes that tensor will wait on that event.
        self.recv_events = [[None] * num_input for _ in range(chunks)]
        # Once all num_input tensors of a given chunk are recieved, they are assembled as a batch and stored in
        # self.batches
        self.batches: List[Optional[Batch]] = [None] * chunks
        # For each tensor of each chunk, we fork a phony tensor, which will be used for injecting dependency between
        # different chunks in backward path.
        self.forwarded_phony: List[List[List[rpc.RRef]]] = [[[] for j in range(num_input)] for i in range(chunks)]
        self.users = users
        self.rank = rank
        self.device = device

    def feed(self, chunk: int, input_idx: int, input: Tensor) -> Tensor:
        """ This function is called remotely to provide individual tensors of a given chunk."""
        if input.device.type == "cpu":
            input = input.to(self.device)
        cuda_stream = torch.cuda.current_stream(input.device) if input.device.type == "cuda" else None

        with self.ready_cv:
            assert self.tensors[chunk][input_idx] is None
            input, phony = fork(input)
            self.recv_events[chunk][input_idx] = (
                cuda_stream.record_event() if cuda_stream is not None else None  # type: ignore
            )
            self.tensors[chunk][input_idx] = input
            self.ready_cv.notify_all()
        return phony

    def wait_for(self, chunk: int) -> None:
        """Waits until all elements of given chunk is populated in self.tensors.
        Then it constructs self.batches[chunk] if it is not constructed yet.
        """
        with self.ready_cv:
            while self.batches[chunk] is None and any(b is None for b in self.tensors[chunk]):
                self.ready_cv.wait()
            if self.batches[chunk] is None:
                tensors = cast(List[Tensor], self.tensors[chunk])
                self.batches[chunk] = Batch(tuple(tensors), chunk)

    def get_batch(self, chunk: int) -> Batch:
        batch = self.batches[chunk]
        assert batch is not None
        return batch


class PartitionHandler:
    """This class processes a single partition of the pipeline.
    Args:
        module_rref: RRef to the nn.Module for this partition. It should be on the local rpc worker.
        device: The device that hols the module.
        num_input: Numer of inputs to the module
        num_output: Number of outputs of the module. If the module output is not a tuple (and it is a
            single tensor), num_output should be None.
        rank: The rank of the partition
        chunks: Number of micor-batches in a mini-batch
        checkpoint_stop::
    """

    def __init__(
        self,
        module_rref: rpc.RRef,
        device: str,
        num_input: int,
        num_output: Optional[int],
        rank: int,
        chunks: int,
        checkpoint_stop: int,
    ) -> None:
        self.module = module_rref.local_value()
        self.chunks = chunks
        self.device = torch.device(device)
        self.checkpoint_stop = checkpoint_stop
        self.rank = rank
        self.num_input = num_input
        self.num_output = num_output
        (self.in_queue,), (self.out_queue,) = create_workers([self.device])

    def local_parameter_rrefs(self) -> List[rpc.RRef]:
        r"""
        Create one RRef for each parameter in the given local module, and return a
        list of RRefs.
        """
        return [rpc.RRef(p) for p in self.module.parameters()]

    def make_pipeline_record(self, users: List[Tuple[rpc.RRef, int, int]]) -> DistributedPipelineRecord:
        return DistributedPipelineRecord(self.device, self.rank, self.chunks, self.num_input, users)

    def run(self, pipeline_record: DistributedPipelineRecord) -> None:
        """Runs pipeline parallelism.

        It modifies the given batches in place.

        """

        m = len(pipeline_record.batches)

        self.stream = current_stream(self.device)

        for i in range(m):
            with record_function("feed"):
                pipeline_record.wait_for(i)
            self.fence(pipeline_record, i)
            self.compute(pipeline_record, i)
            self.forward_results(i, pipeline_record)

    def fence(self, pipeline_record: DistributedPipelineRecord, chunk: int) -> None:
        """Prepares micro-batches for computation."""
        # Ensure that batches[chunk-1] is executed after batches[chunk] in
        # backpropagation by an explicit dependency.
        # TODO: This dependency injection causes deadlock if this partition
        # gets its input from model input. 1) Figure out why 2) If we need to live
        # with this constraint, replace the condition 'pipeline_record.rank > 0' below with
        # a more accurate one.
        if chunk != 0 and pipeline_record.users and pipeline_record.rank > 0:
            t = []
            batch = pipeline_record.batches[chunk]
            assert batch is not None
            for b, remote_ph_list in zip(batch.tensors, pipeline_record.forwarded_phony[chunk - 1]):
                r = b
                for remote_ph in remote_ph_list:
                    ph = remote_ph.to_here()
                    r = join(r, ph)
                t.append(r)
            pipeline_record.batches[chunk] = Batch(tuple(t), chunk)

    def compute(self, pipeline_record: DistributedPipelineRecord, chunk: int) -> None:
        """Runs tasks with synchronization to tensor-pipe streams."""
        checkpoint_stop = self.checkpoint_stop

        # Disable checkpointing if in eval mode.
        if not self.module.training:
            checkpoint_stop = 0

        exc_info: Optional[ExcInfo] = None

        batch = pipeline_record.get_batch(chunk)

        if pipeline_record is not None and pipeline_record.rank >= 0:
            for e in pipeline_record.recv_events[chunk]:
                if e is not None and is_cuda(self.stream):
                    self.stream.wait_event(e)

        # Determine whether checkpointing or not.
        checkpoint = chunk < checkpoint_stop
        if checkpoint:

            def function(input: TensorOrTensors, chunk_id: int = chunk) -> TensorOrTensors:
                with record_function("chunk%d-rank%d" % (chunk_id, pipeline_record.rank)):
                    result = self.module(*input)
                    if self.num_output is None:
                        result = (result,)
                    return tuple(result)

            chk = Checkpointing(function, batch)
            task = Task(self.stream, compute=chk.checkpoint, finalize=chk.recompute)
            del function, chk

        else:

            def compute(
                batch: Batch = batch,
                chunk_id: int = chunk,
                rank: int = pipeline_record.rank if pipeline_record is not None else -1,
            ) -> Batch:
                with record_function("chunk%d-rank%d" % (chunk_id, pipeline_record.rank)):
                    result = self.module(*batch.tensors)
                    if self.num_output is None:
                        result = (result,)
                return Batch(result, chunk_id)

            task = Task(self.stream, compute=compute, finalize=None)
            del compute

        self.in_queue.put(task)

        ok, payload = self.out_queue.get()

        # Hold the first exception.
        if exc_info is not None:
            pass
        elif not ok:
            exc_info = cast(ExcInfo, payload)
        else:
            task, batch = cast(Tuple[Task, Batch], payload)

            with use_device(self.device):
                task.finalize(batch)

            pipeline_record.batches[chunk] = batch

        if exc_info is not None:
            raise exc_info[0].with_traceback(exc_info[1], exc_info[2])

    def forward_results(self, chunk: int, pipeline_record: DistributedPipelineRecord) -> None:
        """Forwards outputs of processing a chunk in this parition for processing by next partition."""
        with use_stream(self.stream):
            for user, input_idx, output_idx in pipeline_record.users:
                v = pipeline_record.get_batch(chunk).value[output_idx]
                pipeline_record.forwarded_phony[chunk][output_idx].append(user.remote().feed(chunk, input_idx, v))

    def run_pipeline(self, pipeline_record_rref: rpc.RRef) -> Optional[Tensor]:
        """Processes a min-batch on this partition.
           If this is the last partition (pipeline_record has no user), concatenates results of processing
           all chunks and returns the result as the output of the model on the whole mini-batch.
        """
        pipeline_record = pipeline_record_rref.local_value()
        self.run(pipeline_record)

        if not pipeline_record.users:
            result = microbatch.gather(pipeline_record.batches)
            assert len(result) == 1
            result = result[0]
            s0 = current_stream(result.device)
            if is_cuda(s0):
                # TODO. Investigate why this is needed and remove it if possible.
                as_cuda(s0).synchronize()
            return result

        return None


class MultiInputSequential(nn.Module):
    """A variation of nn.Sequential, that allows the first module in the sequence accepts
        multiple inputs. To be used internally by _split_module
    """

    def __init__(self, *modules: nn.Module) -> None:
        super().__init__()
        self.modules_list = modules

    def forward(self, *inputs: Tuple[Tensor]) -> Tensor:  # type: ignore
        input = self.modules_list[0](*inputs)
        for module in self.modules_list[1:]:
            input = module(input)
        return input


def RemoteSequential(rref_list: List[rpc.RRef]) -> MultiInputSequential:
    return MultiInputSequential(*(r.local_value() for r in rref_list))


def _split_module(graph: PipelineModulesGraph) -> List[Tuple[List[int], rpc.RRef]]:
    """Splits the graph into pipeline partitions and for each parition returns a tuple (indices, module_rref),
    where indices is indices of modules of the partition in the graph, and module_rref is an RRef to an nn.Module:
    If there is only one module in the partition, module_rref is reference to that module; otherwise those modules
    are wrapped by a MultiInputSequential and module_rref referes to that.
    """
    graph.compute_output_users()
    module_used = [False] * len(graph.modules_list)
    partitions = []
    for module_idx, module in enumerate(graph.modules_list):
        if module_used[module_idx]:
            continue
        partition = []
        current_module_idx = module_idx
        current_module = module
        while True:
            assert not module_used[current_module_idx]
            module_used[current_module_idx] = True
            partition.append(current_module_idx)
            # If we reached a module with multiple outputs or with multiple users for its output,
            # stop adding more modules to the partition.
            if len(graph.output_users[current_module_idx]) != 1:
                break
            if graph.modules_list[current_module_idx].num_outputs is not None:
                break
            # Next module to add is the only consumer of the ouput of the current module
            next_module_idx = graph.output_users[current_module_idx][0][0]
            next_module = graph.modules_list[next_module_idx]
            # If the next module has multiple inputs, do not add it to the current partition and stop.
            if graph.inputs[next_module_idx] != [(current_module_idx, 0)]:
                break
            # If the next module is on a different deivce or worker, stop
            if next_module.on != current_module.on:
                break
            if next_module.device != current_module.device:
                break
            current_module = next_module
            current_module_idx = next_module_idx
        if len(partition) == 1:
            remote_module = graph.modules_list[partition[0]].get_module_rref()
        else:
            remote_module = rpc.remote(
                graph.modules_list[partition[0]].on,
                RemoteSequential,
                args=([graph.modules_list[p].get_module_rref() for p in partition],),
            )
        partitions.append((partition, remote_module))

    return partitions


MOVING_DENIED = TypeError(
    "denied to move parameters and buffers, " "because DistributedPipeline should manage device placement"
)


class DistributedPipeline(nn.Module):
    """Wraps a :class:`PipelineModulesGraph` model to train on using synchronous pipeline
    parallelism. If the model requires lots of memory and doesn't fit on a single GPU,
    pipeline parallelism is a useful technique to employ for training.

    The implementation is based on the torchgpipe_ paper.

    .. _torchgpipe: https://arxiv.org/abs/2004.09910

    PipelineModulesGraph combines pipeline parallelism with checkpointing to reduce peak
    memory required to train while minimizing device under-utilization.

    You should place all the modules on the appropriate rpc workers and devices and wrap
    them into an :class:`PipelineModulesGraph` module defining the connection between them.

    Args:
        module (:class:`PipelineModulesGraph`):
        model to be parallelized using pipelining. Each module
            in the graph has to have all of its parameters on a single
            device.
        chunks (int):
            number of micro-batches (default: ``1``)
        checkpoint (str):
            when to enable checkpointing, one of ``'always'``,
            ``'except_last'``, or ``'never'`` (default: ``'except_last'``).
            ``'never'`` disables checkpointing completely, ``'except_last'``
            enables checkpointing for all micro-batches except the last one
            and ``'always'`` enables checkpointing for all micro-batches.
    """

    def __init__(self, graph: PipelineModulesGraph, chunks: int = 1, checkpoint: str = "except_last",) -> None:
        super().__init__()

        check_pytorch_version()
        graph.validate_graph()

        chunks = int(chunks)
        checkpoint = str(checkpoint)

        if chunks <= 0:
            raise ValueError("number of chunks must be positive integer")
        if checkpoint not in ["always", "except_last", "never"]:
            raise ValueError("checkpoint is not one of 'always', 'except_last', or 'never'")

        self.chunks = chunks

        self.partitions = _split_module(graph)
        self.input_feeds = [
            next((i, fj, feed_idx) for i, (p, m) in enumerate(self.partitions) if p[0] == fi)
            for fi, fj, feed_idx in graph.model_input_users
        ]

        # The micro-batch index where the checkpointing stops.
        checkpoint_stop = {"always": self.chunks, "except_last": self.chunks - 1, "never": 0}[checkpoint]

        self.partition_handlers = [
            rpc.remote(
                m.owner(),
                PartitionHandler,
                args=(
                    m,
                    graph.modules_list[p[0]].device,
                    graph.modules_list[p[0]].num_inputs,
                    graph.modules_list[p[-1]].num_outputs,
                    i,
                    self.chunks,
                    checkpoint_stop,
                ),
            )
            for i, (p, m) in enumerate(self.partitions)
        ]
        self.graph = graph

    # DistributedPipeline should manage the device of each partition.
    # Deny cuda(), cpu(), and to() with device, by TypeError.
    def cuda(self, device: Optional[Device] = None) -> "DistributedPipeline":
        raise MOVING_DENIED

    def cpu(self) -> "DistributedPipeline":
        raise MOVING_DENIED

    def to(self, *args: Any, **kwargs: Any) -> "DistributedPipeline":
        # Deny these usages:
        #
        # - to(device[, dtype, non_blocking])
        # - to(tensor[, non_blocking])
        #
        # But allow this:
        #
        # - to(dtype[, non_blocking])
        #
        if "device" in kwargs or "tensor" in kwargs:
            raise MOVING_DENIED

        if args:
            if isinstance(args[0], (torch.device, int, str)):
                raise MOVING_DENIED
            if torch.is_tensor(args[0]):
                raise MOVING_DENIED

        return super().to(*args, **kwargs)

    def parameter_rrefs(self) -> List[rpc.RRef]:
        remote_params = []
        for p in self.partition_handlers:
            remote_params.extend(p.rpc_sync().local_parameter_rrefs())
        return remote_params

    def forward(self, *inputs: Tensor) -> rpc.RRef:  # type: ignore
        for i, input in enumerate(inputs):
            microbatch.check(input)

        # Divide a mini-batch into micro-batches.
        batches_list = [microbatch.scatter(input, self.chunks) for input in inputs]
        num_partitions = len(self.partition_handlers)

        # Create a DistributedPipelineRecord, one per partition, and make connections between them (i.e.
        # set list of users).
        pipeline_records: List[Optional[rpc.RRef]] = [None] * (num_partitions + 1)
        for part_idx in reversed(range(num_partitions)):
            r_handler = self.partition_handlers[part_idx].remote()
            users = []
            # Identify users of the outputs of the partition
            for user, input_idx, output_idx in self.graph.output_users[self.partitions[part_idx][0][-1]]:
                user_partition = next(i for i, (p, num_partitions) in enumerate(self.partitions) if p[0] == user)
                # Index of a user partition should be greater than index of the partition.
                assert user_partition > part_idx
                users.append((pipeline_records[user_partition], input_idx, output_idx))
            pipeline_records[part_idx] = r_handler.make_pipeline_record(users)
            # Let the pipeline-handler for the partition starts processing the pipeline-record for that partition.
            this_result = r_handler.run_pipeline(pipeline_records[part_idx])
            # If this is the last partition, we expect the result of the model be the output of this partition.
            if part_idx == num_partitions - 1:
                result = this_result

        # Start feeding model input to the partitions that need them.
        for i, b in enumerate(zip(*batches_list)):
            for fi, fj, feed_idx in self.input_feeds:
                pipeline_record = pipeline_records[fi]
                assert pipeline_record is not None
                # TODO: Debug why we need this special handling
                if pipeline_record.owner().name == rpc.get_worker_info().name:  # type: ignore
                    pipeline_record.local_value().feed(i, fj, b[feed_idx].value)
                else:
                    pipeline_record.rpc_async().feed(i, fj, b[feed_idx].value)  # type: ignore

        return result


def create_sequence_pipeline(
    layers: List[PipelineModule], balance: List[int], devices: List[str], **kwargs: Any
) -> DistributedPipeline:
    """A simple helper function to create a pipeline from list of pipeline-modules that run sequentially.
       Args:
           layers: list of modules. They should not be already assigned a remote-device.
           balance: a list of integers how layers should be paritioned. Sum of numbers in 'balance'
               should be equal to the number of layers.
           devices: specification of remote device for each partition. Should be of the same length
               as 'balance'.
    """
    graph = PipelineModulesGraph()

    index = 0
    for num_layers, remote_device in zip(balance, devices):
        next_index = index + num_layers
        for li in range(index, next_index):
            layers[li].instantiate(remote_device)
        index = next_index

    graph.add_sequence(layers)
    graph.feed_model_input(layers[0])

    return DistributedPipeline(graph, **kwargs)