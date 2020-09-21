import io
import abc
import json
import inspect
import asyncio
import secrets
import traceback
from queue import Queue
from collections import defaultdict
from itertools import product,chain
from contextlib import asynccontextmanager, AsyncExitStack, ExitStack
from typing import Dict, List, Any, Union, Optional, AsyncIterator, Tuple

from nats.aio.client import Client as NATS

from ..base import config, BaseConfig, field
from ..util.asynchelper import aenter_stack
from ..util.data import ignore_args
from ..df.types import DataFlow, Operation, Definition, Input, Stage,Parameter
from ..df.memory import (
    MemoryRedundancyChecker,
    MemoryLockNetwork,
    MemoryOperationImplementationNetwork,
    MemoryOperationImplementationNetworkContext,
    MemoryOperationNetworkContext,
    MemoryOperationNetwork,
    MemoryInputNetworkContext,
    MemoryInputNetwork,
    MemoryRedundancyCheckerConfig,
    MemoryRedundancyCheckerContext,
    MemoryRedundancyChecker,
    MemoryOrchestratorContext,
    MemoryOrchestratorContextConfig,
    MemoryParameterSet,
    MemoryParameterSetConfig,
    MemoryInputSet,
    MemoryLockNetworkContext,
    NotificationSet,
    MemoryInputNetworkContextEntry,
    MemoryInputSetConfig
)
from ..df.base import (
    BaseOrchestratorContext,
    BaseOperationNetworkContext,
    BaseOperationNetwork,
    BaseDataFlowObject,
    BaseDataFlowObjectContext,
    BaseOperationImplementationNetworkContext,
    BaseInputSetContext,
    StringInputSetContext,
    BaseInputSet,
    BaseParameterSet,
    BaseDefinitionSetContext,
    BaseInputNetworkContext,
    OperationException,
    BaseContextHandle,
    OperationImplementation,
)


# Subjects
RegisterWorkerNode = "RegisterWorkerNode"
ConnectToOrchestratorNode = "ConnectToOrchestratorNode"
OrchestratorNodeReady = "OrchestratorNodeReady"

####

NBYTES_UID = 6


class CircularQueue(Queue):
    def get(self):
        val = super().get()
        self.put(val)
        return val


class NatsOrchestratorInputNetworkContext(MemoryInputNetworkContext):
    def __init__(self, config: BaseConfig, parent: "NatsOrchestratorNodeContext"):
        super().__init__(config, parent)
        self.input_uids = {} # maps uids to input
        self.waiting_for_outputs = {}

    async def add_input_set(self,msg):
        self.logger.debug(f"Receiving new input set from worker")
        data = json.loads(msg.data.decode())

        def set_parents(data:Any):
            # The exported input set contains ids in parent list.
            # Rebuild parent from ids using self.input_uids
            if isinstance(data,Dict):
                for key,val in data.items():
                    if isinstance(val,Dict):
                        val = set_parents(val)
                    if isinstance(val,List):
                        val = [set_parents(item) for item in val]
                    data[key] = val

                if 'parents' in data:
                    data['parents'] = [ self.input_uids[parent_id] for parent_id in data['parents']]

            return data

        data = set_parents(data)

        self.logger.debug(f"Received {data}")
        input_set = MemoryInputSet._fromdict(**data)
        await self.add(input_set)

    async def add(self,input_set: BaseInputSet):
        self.logger.debug(f"Adding input to network :{input_set}")


        # TODO
        # Check if any output operation is waiting for th
        # Continue from here
        # key = str(input_set.export())
        # print(f"\n\n How to set key  {key} \n\n")

        async for item in input_set.inputs():
            self.input_uids[item.uid] = item

        # Grab the input set context handle
        handle = await input_set.ctx.handle()
        handle_string = handle.as_string()
        # TODO These ctx.add calls should probably happen after inputs are in
        # self.ctxhd

        # remove unvalidated inputs
        unvalidated_input_set = await input_set.remove_unvalidated_inputs()

        # If the context for this input set does not exist create a
        # NotificationSet for it to notify the orchestrator
        if not handle_string in self.input_notification_set:
            self.input_notification_set[handle_string] = NotificationSet()
            async with self.ctx_notification_set() as ctx:
                await ctx.add((None, input_set.ctx))


        # Add the input set to the incoming inputs
        async with self.input_notification_set[handle_string]() as ctx:
            await ctx.add((unvalidated_input_set, input_set))
        # Associate inputs with their context handle grouped by definition
        async with self.ctxhd_lock:
            # Create dict for handle_string if not present
            if not handle_string in self.ctxhd:
                self.ctxhd[handle_string] = MemoryInputNetworkContextEntry(
                    ctx=input_set.ctx, definitions={}, by_origin={}
                )
            # Go through each item in the input set
            async for item in input_set.inputs():
                # Create set for item definition if not present
                if (
                    not item.definition
                    in self.ctxhd[handle_string].definitions
                ):
                    self.ctxhd[handle_string].definitions[item.definition] = []
                # Add input to by defintion set
                self.ctxhd[handle_string].definitions[item.definition].append(
                    item
                )
                # Create set for item origin if not present
                if not item.origin in self.ctxhd[handle_string].by_origin:
                    self.ctxhd[handle_string].by_origin[item.origin] = []
                # Add input to by origin set
                self.ctxhd[handle_string].by_origin[item.origin].append(item)


    async def __aenter__(self):
        await super().__aenter__()
        # subscribe for new input sets
        self.logger.debug(f"Subscribing to new input sets on {self.parent.uid}.NewInputSet")
        await self.parent.nc.subscribe(
            f"{self.parent.uid}.NewInputSet",
            queue = "InputSetQueue",
            cb = self.add_input_set
        )
        return self



class NatsWorkerInputNetworkContext(MemoryInputNetworkContext):
    def __init__(self, config: BaseConfig, parent: "NatsWorkerNodeContext"):
        super().__init__(config, parent)

    async def add(self,input_set:BaseInputSet):
        # In mem octx `run_dispatch` calls `add` internally to
        # add the output of the operation back to the network,
        # now add will sent back the new inputs to the orchestrator
        # network from which it'll be redirected to the corresponding
        # worker node.
        self.logger.debug(f"Adding new input_set to main network: {input_set.export()}")
        await self.parent.nc.publish(
            f"{self.parent.onid}.NewInputSet",
            json.dumps(input_set.export()).encode()
        )


class NatsOperationImplementationNetworkContext(
    MemoryOperationImplementationNetworkContext
):
    def __init__(
        self, config: BaseConfig, parent: "NatsWorkerNodeContext",
    ):
        BaseOperationImplementationNetworkContext.__init__(
            self, config, parent
        )
        self.operations = {}

    async def instantiate_operation(self, operation, instance_name):
        opimp = self.parent.operation_implementations.get(operation.name, None)
        opconfig = self.parent.operation_configs.get(instance_name, {})
        self.logger.debug(
            f"Instantiating instance {instance_name} of operation {operation} with config {opconfig}"
        )
        operation = operation._replace(instance_name = operation.name)
        await self.instantiate(operation, config=opconfig, opimp=opimp)
        await self.parent.nc.publish(
            f"InstanceAck.{instance_name}.{self.parent.onid}", b""
        )

    async def __aenter__(self):
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        return self

    async def dispatch(self,wctx:"NatsWorkerNodeContext",operation:Operation,parameter_set:BaseParameterSet):
        """
        Schedule the running of an operation
        """
        self.logger.debug("[DISPATCH] %s", operation.name)
        # run_dipatch calls ictx.add to add back results to the orchestrator network

        # TODO
        # this is just a patch
        # find why instance name is None, and why parents are still strings
        operation = operation._replace(instance_name = operation.name)

        # TODO
        # Run dispatch does not support locking
        # aqquire requires parents, currently parents are list of uids in
        # worker context.
        task = asyncio.create_task(
            self.run_dispatch(wctx, operation, parameter_set)
        )

        return task

    async def run_dispatch(
        self,
        octx: BaseOrchestratorContext,
        operation: Operation,
        parameter_set: BaseParameterSet,
        set_valid: bool = True,
    ):
        """
        Run an operation in the background and add its outputs to the input
        network when complete
        """
        # Ensure that we can run the operation
        # Locking not implemented
            # Lock all inputs which cannot be used simultaneously
            # async with octx.lctx.acquire(parameter_set):

        # Run the operation
        outputs = await self.run(
            parameter_set.ctx,
            octx,
            operation,
            await parameter_set._asdict(),
        )
        if outputs is None:
            return
        if not inspect.isasyncgen(outputs):

            async def to_async_gen(x):
                yield x

            outputs = to_async_gen(outputs)
        async for an_output in outputs:
            # Create a list of inputs from the outputs using the definition mapping
            try:
                inputs = []
                if operation.expand:
                    expand = operation.expand
                else:
                    expand = []
                parents = [
                    item.origin async for item in parameter_set.parameters()
                ]
                for key, output in an_output.items():
                    if not key in expand:
                        output = [output]
                    for value in output:
                        new_input = Input(
                            value=value,
                            definition=operation.outputs[key],
                            parents=parents,
                            origin=(operation.instance_name, key),
                        )
                        new_input.validated = set_valid
                        inputs.append(new_input)
            except KeyError as error:
                raise KeyError(
                    "Value %s missing from output:definition mapping %s(%s)"
                    % (
                        str(error),
                        operation.instance_name,
                        ", ".join(operation.outputs.keys()),
                    )
                ) from error
            # Add the input set made from the outputs to the input set network
            await octx.ictx.add(
                MemoryInputSet(
                    MemoryInputSetConfig(ctx=parameter_set.ctx, inputs=inputs)
                )
            )

class NatsNodeContext(BaseDataFlowObjectContext):
    async def __aenter__(self):
        self.uid = secrets.token_hex(nbytes=NBYTES_UID)
        self.nc = self.parent.nc
        self._stack = AsyncExitStack()
        enter = {}
        self._stack = await aenter_stack(self, enter)
        await self.init_context()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self._stack.aclose()

    @abc.abstractmethod
    async def init_context(self):
        pass


@config
class NatsNodeConfig:
    server: str


class NatsNode(BaseDataFlowObject):
    async def error_handler(self, msg):
        tb = traceback.format_exc()
        print()
        print(tb)
        print(f"ERROR in node {self.uid}")
        print()

    async def __aenter__(self):
        self.uid = secrets.token_hex(nbytes=NBYTES_UID)
        self.logger.debug(f"New node: {self.uid}",)
        self.nc = NATS()
        self.logger.debug(f"Connecting to nats server {self.config.server}")
        await self.nc.connect(
            self.config.server,
            name=self.uid,
            connect_timeout=10,
            error_cb=self.error_handler,
        )
        self.logger.debug(f"Connected to nats server {self.config.server}")
        await self.init_node()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.nc.close()

    @abc.abstractmethod
    async def init_node(self):
        pass


@config
class NatsOrchestratorNodeConfig(NatsNodeConfig):
    dataflow: DataFlow = None
    implementations: Dict[str, OperationImplementation] = field(
        "Operation implementations to load on initialization",
        default_factory=lambda: {},
    )
    configs: Dict[str, OperationImplementation] = field(
        "Configs for operations",
        default_factory=lambda: {},
    )


class NatsOrchestratorContext(MemoryOrchestratorContext):
    def __init__(self,config,parent:"NatsOrchestratorNodeContext"):
        super().__init__(config,parent)
        self._stack = AsyncExitStack()

    async def __aenter__(self):
        await self._stack.__aenter__()
        self.ictx = await self._stack.enter_async_context(
                NatsOrchestratorInputNetworkContext(BaseConfig(),self.parent)
            )
        self.rchecker = await self._stack.enter_async_context(MemoryRedundancyChecker())
        self.rctx = await self._stack.enter_async_context(self.rchecker())
        return self


    async def run(
        self,
        *input_sets: Union[List[Input], BaseInputSet],
        strict: bool = True,
        ctx: Optional[BaseInputSetContext] = None,
    ):
        ctxs: List[BaseInputSetContext] = []
        self.logger.debug("Running %s: %s", self.config.dataflow, input_sets)

        if not input_sets:
            # If there are no input sets, add only seed inputs
            ctxs.append(await self.seed_inputs(ctx=ctx))
        if len(input_sets) == 1 and isinstance(input_sets[0], dict):
            # Helper to quickly add inputs under string context
            for ctx_string, input_set in input_sets[0].items():
                ctxs.append(
                    await self.seed_inputs(
                        ctx=StringInputSetContext(ctx_string),
                        input_set=input_set,
                    )
                )
        else:

            # For inputs sets that are of type BaseInputSetContext or list
            for input_set in input_sets:
                ctxs.append(
                    await self.seed_inputs(ctx=ctx, input_set=input_set)
                )
        tasks = set()
        # Create tasks to wait on the results of each of the contexts submitted
        for ctx in ctxs:
            self.logger.debug(
                "kickstarting context: %s", (await ctx.handle()).as_string()
            )
            tasks.add(
                asyncio.create_task(
                    self.run_operations_for_ctx(ctx, strict=strict)
                )
            )

        try:
            # Return when outstanding operations reaches zero
            while tasks:
                # Wait for incoming events
                done, _pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )

                for task in done:
                    # Remove the task from the set of tasks we are waiting for
                    tasks.remove(task)
                    # Get the tasks exception if any
                    exception = task.exception()
                    if strict and exception is not None:
                        raise exception
                    elif exception is not None:
                        # If there was an exception log it
                        output = io.StringIO()
                        task.print_stack(file=output)
                        self.logger.error("%s", output.getvalue().rstrip())
                        output.close()
                    else:
                        # All operations for a context completed
                        # Yield the context that completed and the results of its
                        # output operations
                        ctx, results = task.result()
                        yield ctx, results
                self.logger.debug("ctx.outstanding: %d", len(tasks) - 1)
        finally:
            # Cancel tasks which we don't need anymore now that we know we are done
            for task in tasks:
                if not task.done():
                    task.cancel()
                else:
                    task.exception()

    async def dispatch(self,operation, parameter_set):
        instance_name = operation.instance_name
        self.logger.debug(f"Dispatching {parameter_set} to instance {instance_name}")
        worker_id = self.parent.nodes_for_operation[operation.name].get()
        data = parameter_set.export()
        data = json.dumps(data).encode()
        task = asyncio.create_task(
            self.parent.nc.publish(
                f"{worker_id}.Parameters.{operation.name}",
                data
            )
        )
        return task

    async def operations(
        self,
        dataflow: DataFlow,
        *,
        input_set: Optional[BaseInputSet] = None,
        stage: Stage = Stage.PROCESSING,
    ) -> AsyncIterator[Operation]:
        operations: Dict[str, Operation] = {}
        if stage not in dataflow.by_origin:
            return
        if input_set is None:
            for operation in chain(*dataflow.by_origin[stage].values()):
                operations[operation.instance_name] = operation
        else:
            async for item in input_set.inputs():
                origin = item.origin
                if isinstance(origin, Operation):
                    origin = origin.instance_name
                if origin not in dataflow.by_origin[stage]:
                    continue
                for operation in dataflow.by_origin[stage][origin]:
                    operations[operation.instance_name] = operation
        for operation in operations.values():
            yield operation


    async def operations_parameter_set_pairs(
        self,
        ctx: BaseInputSetContext,
        dataflow: DataFlow,
        *,
        new_input_set: Optional[BaseInputSet] = None,
        stage: Stage = Stage.PROCESSING,
    ) -> AsyncIterator[Tuple[Operation, BaseInputSet]]:
        """
        Use new_input_set to determine which operations in the network might be
        up for running. Cross check using existing inputs to generate per
        input set context novel input pairings. Yield novel input pairings
        along with their operations as they are generated.
        """
        # Get operations which may possibly run as a result of these new inputs
        async for operation in self.operations(
            dataflow, input_set=new_input_set, stage=stage
        ):
            # Generate all pairs of un-run input combinations
            async for parameter_set in self.ictx.gather_inputs(
                self.rctx, operation, dataflow, ctx=ctx
            ):
                print(f"\n\n new parameter set from opparamset parent {parameter_set.config.parameters[0].origin.parents} \n\n")
                yield operation, parameter_set

    async def run_operations_for_ctx(self, ctx: BaseContextHandle, *, strict: bool = True):
        # Track if there are more inputs
        more = True
        # Set of tasks we are waiting on
        tasks = set()
        # String representing the context we are executing operations for
        ctx_str = (await ctx.handle()).as_string()
        input_set_enters_network = asyncio.create_task(self.ictx.added(ctx))
        tasks.add(input_set_enters_network)
        try:
            # Return when outstanding operations reaches zero
            while tasks:
                self.logger.debug("Waiting for more tasks")
                if (
                    not more
                    and len(tasks) == 1
                    and input_set_enters_network in tasks

                ):
                    async with self.ictx.input_notification_set[ctx_str]() as nt_ctx:
                        if nt_ctx.parent.event_added_lock.locked():
                            async with nt_ctx.parent.event_added_lock:
                                pass
                        else:
                            self.logger.debug("No more tasks")
                            break


                # Wait for incoming events
                done, _pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    # Remove the task from the set of tasks we are waiting for
                    tasks.remove(task)
                    # Get the tasks exception if any
                    exception = task.exception()
                    if strict and exception is not None:
                        if task is input_set_enters_network:
                            raise exception
                        raise OperationException(
                            "{}({}): {}".format(
                                task.operation.instance_name,
                                task.operation.inputs,
                                {
                                    parameter.key: parameter.value
                                    async for parameter in task.parameter_set.parameters()
                                },
                            )
                        ) from exception
                    elif exception is not None:
                        # If there was an exception log it
                        output = io.StringIO()
                        task.print_stack(file=output)
                        self.logger.error("%s", output.getvalue().rstrip())
                        output.close()

                    elif task is input_set_enters_network:
                        (
                            more,
                            new_input_sets,
                        ) = input_set_enters_network.result()
                        for (
                            unvalidated_input_set,
                            new_input_set,
                        ) in new_input_sets:
                            # Identify which operations have completed contextually
                            # appropriate input sets which haven't been run yet
                            async for operation, parameter_set in self.operations_parameter_set_pairs(
                                ctx,
                                self.config.dataflow,
                                new_input_set=new_input_set,
                            ):
                                # Validation operations shouldn't be run here
                                if operation.validator:
                                    continue
                                # Dispatch the operation and input set for running
                                dispatch_operation = await self.dispatch(operation, parameter_set)
                                dispatch_operation.operation = operation
                                dispatch_operation.parameter_set = (
                                    parameter_set
                                )
                                tasks.add(dispatch_operation)
                                self.logger.debug(
                                    "[%s]: dispatch operation: %s",
                                     ctx_str,
                                    operation.instance_name,
                                )
                        # Create a another task to waits for new input sets
                        input_set_enters_network = asyncio.create_task(
                            self.ictx.added(ctx)
                        )
                        tasks.add(input_set_enters_network)
        finally:
            # Cancel tasks which we don't need anymore now that we know we are done
            for task in tasks:
                if not task.done():
                    task.cancel()
                else:
                    task.exception()
            # Run cleanup
            async for _operation, _results in self.run_stage(
                ctx, Stage.CLEANUP
            ):
                # TODO : make an issue to track this
                raise NotImplementedError("Operations in cleanup stage not supported yet!")

        handle_string = (await ctx.handle()).as_string()
        if handle_string in self.parent.opimpctx.waiters_for_output:
            self.parent.opimpctx.waiters_for_output[handle_string].set()
        else:
            self.parent.opimpctx.waiters_for_output[handle_string] = None

        # Set output to empty dict in case output operations fail
        output = {}
        # Run output operations and create a dict mapping the operation name to
        # the output of that operation
        try:
            output = {
                operation.instance_name: results
                async for operation, results in self.run_stage(
                    ctx, Stage.OUTPUT
                )
            }
        except:
            if strict:
                raise
            else:
                self.logger.error("%s", traceback.format_exc().rstrip())
        # If there is only one output operation, return only it's result instead
        # of a dict with it as the only key value pair
        if len(output) == 1:
            output = list(output.values())[0]
        # Return the context along with it's output
        return ctx, output

    async def run_stage(self, ctx: BaseInputSetContext, stage: Stage):
        # Identify which operations have complete contextually appropriate
        # input sets which haven't been run yet and are stage operations
        async for operation, parameter_set in self.operations_parameter_set_pairs(
            ctx, self.config.dataflow, stage=stage
        ):
            # Output operations/implementations are
            # required to be in the orchestrator node,
            # and is handled by omimp network of onode
            if stage == Stage.OUTPUT:
                # Run the operation, input set pair
                yield operation, await self.parent.opimpctx.run(
                    ctx, self, operation, await parameter_set._asdict()
                )
            else:
                raise(NotImplementedError("Cleanup stage not implemented"))
                # possible solution ??
                self.logger.debug(f"Requesting running of  {operation.name} with parameter {parameter_set}")
                worker_id = self.parent.nodes_for_operation[operation.name].get()
                data = dict(
                    parameter_set = parameter_set.export(),
                    operation = operation.export()
                )
                response = await self.parent.nc.request(
                    f"{worker_id}.RunOperationParameterSet",
                    json.dumps(data).encode()
                )
                result = json.loads(response.data.decode())
                yield operation,result

class NatsOrchestratorNodeContext(NatsNodeContext):
    async def register_worker_node(self, msg):
        self.logger.debug(f"Registering new worker node context")
        worker_id = msg.subject.split('.')[-1]
        reply = msg.reply
        # Data of message contains list of names of
        # operation supported by the worker node
        data = msg.data.decode()
        data = json.loads(data)
        self.logger.debug(f"Received data {data}")

        reply_data = []
        for operation_name in data:
            # Only register operations that are needed by the dataflow
            if operation_name in self.required_operations:
                reply_data.append(operation_name)
                # If some other node also supports running this
                # operation,assign this node a token number in the
                # queue for this operation.
                if operation_name in self.nodes_for_operation:
                    self.nodes_for_operation[operation_name].put(worker_id)
                else:
                    opq = CircularQueue()
                    opq.put(worker_id)
                    self.nodes_for_operation[operation_name] = opq

        reply_data = json.dumps(reply_data).encode()

        if set(self.required_operations) == set(
            self.nodes_for_operation.keys()
        ):
            self.all_ops_available.set()

        await self.nc.publish(reply, reply_data)

    async def acknowledge_instance(self, msg):
        _, *instance_name, snctx_id = msg.subject.split(".")
        instance_name = ".".join(instance_name)

        self.logger.debug(f"Ack: Instance {instance_name} acknowleged")
        self.logger.debug(
            f"self.instances_to_be_acklowledged: {self.instances_to_be_acklowledged}"
        )

        self.instances_to_be_acklowledged.remove(instance_name)
        if len(self.instances_to_be_acklowledged) == 0:
            self.all_op_instantiated.set_result(None)

    async def init_context(self):
        self.dataflow = self.parent.config.dataflow
        # Maps operations to circular queue which contains
        # tokens of worker nodes allocated with different
        # instances of an operation
        self.nodes_for_operation = {}

        self.octx = await self._stack.enter_async_context(
            NatsOrchestratorContext(
                MemoryOrchestratorContextConfig(
                    uid=self.uid,
                    dataflow=self.parent.config.dataflow,
                    reuse={},
                ),
                self,
            )
        )

        # Orchestrator node manages all output operations
        self.opimpctx = await self._stack.enter_async_context(
            NatsOrchestratorOperationImplementationNetworkContext(
                NatsOrchestratorOperationImplementationNetworkContextConfig(
                    self.parent.config.implementations
                ),
                self
            )
        )


        self.output_operations = set()
        for instance_name,operation in self.dataflow.operations.items():
            if operation.stage == Stage.OUTPUT:
                self.output_operations.add(operation.name)
                opimp = self.parent.config.implementations.get(operation.name, None)
                opconfig = self.parent.config.configs.get(instance_name, {})
                self.logger.debug(
                    f"Instantiating instance {instance_name} of operation {operation} with config {opconfig}"
                )
                await self.opimpctx.instantiate(
                        operation._replace(instance_name = operation.name),
                        config=opconfig,
                        opimp=opimp)

                # Also add output operation to `node_for_operation`, so that the check for
                # availability of all operations passes
                self.nodes_for_operation[operation.name] = None

        self.logger.debug("Initializing new orchestrator node context")
        # Orchestrator node announces that it has started
        # All worker node contexts that are not already connected
        # sends list of operations supported by them.
        # Onode responds with allocated token per operation
        self.all_ops_available = asyncio.Event()
        await self.nc.publish(ConnectToOrchestratorNode, self.uid.encode())
        self.required_operations = [
            operation.name
            for operation in self.dataflow.operations.values()
        ]



        await self.nc.subscribe(
            f"{RegisterWorkerNode}.{self.uid}.*", # * contains uid of worker node
            queue="WorkerNodeRegistrationQueue",
            cb=self.register_worker_node,
        )
        self.logger.debug("Waiting for all operations to be found")
        await self.all_ops_available.wait()
        self.logger.debug("All required operations found")

        # Onode goes through operation instances
        # in dataflow and assign an instance to a worker node
        # making sure that each worker node gets at least
        # one instance
        self.node_token_managing_instance = {
            instance_name: (
                operation.name,
                self.nodes_for_operation[operation.name].get(),
            )
            for instance_name, operation in self.dataflow.operations.items()
            if operation.name not in self.output_operations
        }
        self.logger.debug(
            f"Token allocated for instances {self.node_token_managing_instance}"
        )
        # Publish a message with subject as operation name,
        # with data containing node token number and operation
        # instance name. Sub node context will be subscribed
        # to operations they are running for the current dataflow.

        # Whenever pnctx publishes instance details to snctx
        # it keeps log of it and waits for acknwoledgment from
        # the same snctx after instantiating the operation.
        # Execution is blocked till all operations are instantiated.

        self.all_op_instantiated = asyncio.Future()
        self.instances_to_be_acklowledged = set()
        for (
            instance_name,
            (operation_name, token),
        ) in self.node_token_managing_instance.items():
            payload = {
                "instance_name": instance_name,
                "token": token,
                "config": (
                    self.dataflow.configs[instance_name]._asdict()
                    if instance_name in self.dataflow.configs
                    else {}
                ),
            }
            self.instances_to_be_acklowledged.add(instance_name)
            await self.nc.subscribe(
                f"InstanceAck.{instance_name}.{self.uid}",
                queue="InstanceAcknoledgments",
                cb=self.acknowledge_instance,
            )
            self.logger.debug(f"Publishing instance details: {payload}")
            await self.nc.publish(
                f"InstanceDetails{operation_name}.{self.uid}",
                json.dumps(payload).encode(),
            )

        await self.nc.publish(f"{OrchestratorNodeReady}.{self.uid}", b"")
        self.logger.debug("Waiting for all ops to be instantiated")
        await asyncio.wait_for(self.all_op_instantiated, timeout=None)
        self.logger.debug("All ops instantiated")

    async def run(
        self,
        *input_sets: Union[List[Input], BaseInputSet],
        strict: bool = True,
        ctx: Optional[BaseInputSetContext] = None,):
        await asyncio.wait_for(self.all_op_instantiated, timeout=None)
        async for ctx, results in self.octx.run(*input_sets,strict = strict,ctx = ctx):
            yield ctx,results


class NatsOrchestratorNode(NatsNode):
    CONTEXT = NatsOrchestratorNodeContext
    CONFIG = NatsOrchestratorNodeConfig

    def __call__(self):
        return self.CONTEXT(BaseConfig(), self)


@config
class NatsOrchestratorOperationImplementationNetworkContextConfig:
    operations: Dict[str, OperationImplementation] = field(
        "Operation implementations to load on initialization",
        default_factory=lambda: {},
    )


class NatsOrchestratorOperationImplementationNetworkContext(MemoryOperationImplementationNetworkContext):
    def __init__(self,config,parent:"NatsOrchestratorNodeContext"):
        BaseOperationImplementationNetworkContext.__init__(self,config,parent)
        self.opimps = self.config.operations
        self.operations = {}
        self.completed_event = asyncio.Event()
        self.waiters_for_output = {}

    async def run(
        self,
        ctx: BaseInputSetContext,
        octx: BaseOrchestratorContext,
        operation: Operation,
        inputs: Dict[str, Any]):
        # TODO : Ensure that only operations with stage == Stage.OUTPUT is run
        print(f"\n\n OPIMPCTX OUTPUT \n\n\n")
        handle_string = (await ctx.handle()).as_string()
        self.logger.debug(f"output operations waiting: context = {handle_string}")
        print(f"\n\n output operations waiting: context = {handle_string} \n\n")
        print(f"\n\n output operations waiting blah :{self} \n\n")

        if handle_string not in self.waiters_for_output:
            self.waiters_for_output[handle_string] = asyncio.Event()
            await self.waiters_for_output[handle_string].wait()
        return await super().run(ctx,octx,operation,inputs)


@config
class NatsWorkerNodeContextConfig:
    orchestrator_node_id: str


class NatsWorkerNodeContext(NatsNodeContext):
    CONFIG = NatsWorkerNodeContextConfig

    async def __aenter__(self,):
        self.uid = secrets.token_hex(nbytes=NBYTES_UID)
        self.nc = self.parent.nc
        self.context_done = asyncio.Future()
        self._stack = AsyncExitStack()
        await self.init_context()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.logger.debug("Exiting context")
        await self._stack.aclose()

    async def set_instance_details(self, msg):
        # - Sets instance details of the operation
        # - Instantiate operation in opimp network
        # - Starts listenting to parameter sets required for the operation

        # Setting instance details
        subject = msg.subject
        # Operation name might contain '.'
        *subject, onid = subject.split(".")
        subject = ".".join(subject)
        operation_name = subject.replace("InstanceDetails", "")

        # data is expected to contain 'instance_name':('operation_name','token')
        data = msg.data.decode()

        data = json.loads(data)
        self.logger.debug(f"Received instance details: {data}")
        token = data["token"]
        instance_name = data["instance_name"]
        config = data["config"]

        operation = self.operation_names[operation_name]
        op_for_instance = operation._replace(
            instance_name=operation.name
        )
        self.operation_instances[instance_name] = op_for_instance
        if config:
            self.operation_configs[instance_name] = config

        # Instantiate operation
        await self.opimpctx.instantiate_operation(
            op_for_instance, instance_name
        )

        # start listening to parameter set for the operation
        await self.nc.subscribe(
                f"{self.uid}.Parameters.{operation_name}",
                queue=f"{operation_name}ParametersQueue",
                cb=self.dispatch_to_opimp,
            )

        # listen to operation run command
        await self.nc.subscribe(
            f"{self.uid}.RunOperationParameterSet",
            queue=f"RunOperationParameterSetQueue",
            cb=self.run_operation_parameter_set
        )

    async def dispatch_to_opimp(self,msg):
        # Process operation,parameter_set pair received from onode ictx
        self.logger.debug("Dispatching parameter to opimp network")
        subject = msg.subject
        operation_name = subject.split('Parameters.')[-1]
        data = json.loads(msg.data.decode())
        parameter_set = MemoryParameterSet._fromdict(**data)
        await self.opimpctx.dispatch(self,self.operation_names[operation_name],parameter_set)

    async def run_operation_parameter_set(self,msg):
        reply = msg.reply
        data = msg.data.decode()
        data = json.loads(data)
        parameter_set = MemoryParameterSet._fromdict(**data["parameter_set"])
        operation = Operation._fromdict(**data["operation"])
        # Worker contexts doesn't have a concept of instances.
        # Instance names are maintained by onode.
        # Replace whatever instance_name from the incoming
        # operation with operation name, so that the corresponding
        # operation implementation is detected by opimpctx
        operation = operation._replace(instance_name = operation.name)
        self.logger.debug(f"Running {operation} with {parameter_set}")
        outputs = await self.opimpctx.run(
            parameter_set.ctx,
            self,
            operation,
            await parameter_set._asdict(),
        )
        reply_data = json.dumps(outputs)
        self.logger.debug(f"Sending outputs: {outputs} back to orcehstrator")
        await self.nc.publish(reply,reply_data.encode())

    async def init_context(self):
        self.onid = self.config.orchestrator_node_id
        self.logger.debug(
            f"New worker node context {self.uid} connected to orchestrator node {self.onid}"
        )

        self.operation_implementations = {}
        self.operation_configs = {}
        self.operation_instances: Dict[str, Operation] = {}

        self.operation_names = {
            operation.name: operation
            for operation in self.parent.config.operations
        }

        # Look for implementations provided in sub node
        # if any. If no implementations are provided,
        # the operation is assumed to be entrypoint loadable.
        # opimpNetworkctx raises an exception if implementation
        # is not found in both ways.
        for operation_name in self.operation_names:
            if operation_name in self.parent.config.implementations:
                self.operation_implementations[
                    operation_name
                ] = self.parent.config.implementations[operation_name]

        self.opimpctx = await self._stack.enter_async_context(
            NatsOperationImplementationNetworkContext(BaseConfig(), self)
        )
        self.logger.debug(f"Operation implementation network instantiated")
        self.ictx = await self._stack.enter_async_context(
            NatsWorkerInputNetworkContext(BaseConfig(),self)
        )
        self.logger.debug(f"New worker input context")
        self.lctx = await self._stack.enter_async_context(
            MemoryLockNetworkContext(BaseConfig(),self)
        )


        # Subscribe to instance details.
        # We cannot request for instance details because,
        # all worker nodes need to complete registration before
        # orchestrator node can allocate instances uniformly
        for operation in self.parent.config.operations:
            self.logger.debug(
                "Listening for instance details on"
                + f"InstanceDetails{operation.name}.{self.onid}"
            )

            await self.nc.subscribe(
                f"InstanceDetails{operation.name}.{self.onid}",
                queue="SetInstanceDetails",
                cb=self.set_instance_details,
            )

        # Send list of operation supported by the node
        msg_data = json.dumps(list(self.operation_names.keys())).encode()
        # Request node registration
        self.logger.debug(
            "Requesting context registration and token allocation"
        )
        response = await self.nc.request(
            f"{RegisterWorkerNode}.{self.onid}.{self.uid}", msg_data
        )
        # Response contains list operation names that are allowed to
        # be run in this context
        response = json.loads(response.data.decode())
        self.logger.debug(f"operations running in this context: {response}")


@config
class NatsWorkerNodeConfig(NatsNodeConfig):
    operations: List[Operation] = field(
        "Operations available in the worker",
                default_factory=lambda: []
        )
    implementations: Dict[str, "OperationImplementation"] = field(
        "Operation implementations to load on initialization",
        default_factory=lambda: {}
    )


class NatsWorkerNode(NatsNode):
    CONTEXT = NatsWorkerNodeContext
    CONFIG = NatsWorkerNodeConfig

    async def connection_ack_handler(self, msg):
        orchestrator_node_id = msg.data.decode()
        self.logger.debug(
            f"Got connection request from orchestrator node {orchestrator_node_id}"
        )
        self.running_contexts.append(
            await self.CONTEXT(
                NatsWorkerNodeContextConfig(orchestrator_node_id), self
            ).__aenter__()
        )

    async def init_node(self):
        self.logger.debug(
            f"New worker node with operations {self.config.operations}"
        )
        self.running_contexts = []
        # Worker node waits for a connection to a orchestrator node
        await self.nc.subscribe(
            ConnectToOrchestratorNode, cb=self.connection_ack_handler,
        )

    def __call__(self):
        return self