from __future__ import annotations

import asyncio
import gc
import importlib
import logging
import os
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from json import dumps as stringify
from typing import TypedDict

import psutil
from sanic import Sanic
from sanic.log import access_logger, logger
from sanic.request import Request
from sanic.response import json
from sanic_cors import CORS

import api
from api import (
    ExecutionOptions,
    Group,
    JsonExecutionOptions,
    NodeId,
    set_execution_options,
)
from chain.cache import OutputCache
from chain.chain import Chain, FunctionNode
from chain.input import InputMap
from chain.json import JsonNode, parse_json
from chain.optimize import optimize
from custom_types import UpdateProgressFn
from dependencies.store import DependencyInfo, install_dependencies, installed_packages
from events import EventConsumer, EventQueue, ExecutionErrorData
from gpu import get_nvidia_helper
from process import Executor, NodeExecutionError, NodeOutput
from progress_controller import Aborted
from response import (
    already_running_response,
    error_response,
    no_executor_response,
    success_response,
)
from server_config import ServerConfig
from system import is_arm_mac


class AppContext:
    def __init__(self):
        self.config: ServerConfig = None  # type: ignore
        self.executor: Executor | None = None
        self.cache: dict[NodeId, NodeOutput] = {}
        # This will be initialized by after_server_start.
        # This is necessary because we don't know Sanic's event loop yet.
        self.queue: EventQueue = None  # type: ignore
        self.setup_queue: EventQueue = None  # type: ignore
        self.pool = ThreadPoolExecutor(max_workers=4)

    @staticmethod
    def get(app_instance: Sanic) -> AppContext:
        assert isinstance(app_instance.ctx, AppContext)
        return app_instance.ctx


app = Sanic("chaiNNer", ctx=AppContext())
app.config.REQUEST_TIMEOUT = sys.maxsize
app.config.RESPONSE_TIMEOUT = sys.maxsize
CORS(app)


class SSEFilter(logging.Filter):
    def filter(self, record):  # noqa: ANN001
        request = record.request  # type: ignore
        return not (
            (request.endswith(("/sse", "/setup-sse"))) and record.status == 200  # type: ignore
        )


class ZeroCounter:
    def __init__(self) -> None:
        self.count = 0

    async def wait_zero(self) -> None:
        while self.count != 0:
            await asyncio.sleep(0.01)

    def __enter__(self):
        self.count += 1

    def __exit__(self, _exc_type: object, _exc_value: object, _exc_traceback: object):
        self.count -= 1


run_individual_counter = ZeroCounter()

setup_task = None


async def nodes_available():
    if setup_task is not None:
        await setup_task


access_logger.addFilter(SSEFilter())


@app.route("/nodes")
async def nodes(_request: Request):
    """Gets a list of all nodes as well as the node information"""
    await nodes_available()
    logger.debug(api.registry.categories)

    node_list = []
    for node, sub in api.registry.nodes.values():
        node_dict = {
            "schemaId": node.schema_id,
            "name": node.name,
            "category": sub.category.id,
            "nodeGroup": sub.id,
            "inputs": [x.to_dict() for x in node.inputs],
            "outputs": [x.to_dict() for x in node.outputs],
            "groupLayout": [
                g.to_dict() if isinstance(g, Group) else g for g in node.group_layout
            ],
            "description": node.description,
            "seeAlso": node.see_also,
            "icon": node.icon,
            "nodeType": node.type,
            "hasSideEffects": node.side_effects,
            "deprecated": node.deprecated,
            "features": node.features,
        }
        node_list.append(node_dict)

    return json(
        {
            "nodes": node_list,
            "categories": [x.to_dict() for x in api.registry.categories],
            "categoriesMissingNodes": [],
        }
    )


class RunRequest(TypedDict):
    data: list[JsonNode]
    options: JsonExecutionOptions
    sendBroadcastData: bool


@app.route("/run", methods=["POST"])
async def run(request: Request):
    """Runs the provided nodes"""
    await nodes_available()
    ctx = AppContext.get(request.app)

    if ctx.executor:
        message = "Cannot run another executor while the first one is still running."
        logger.warning(message)
        return json(already_running_response(message), status=500)

    try:
        # wait until all previews are done
        await run_individual_counter.wait_zero()

        full_data: RunRequest = dict(request.json)  # type: ignore
        logger.debug(full_data)
        chain, inputs = parse_json(full_data["data"])
        optimize(chain)

        logger.info("Running new executor...")
        exec_opts = ExecutionOptions.parse(full_data["options"])
        set_execution_options(exec_opts)
        executor = Executor(
            chain=chain,
            inputs=inputs,
            send_broadcast_data=full_data["sendBroadcastData"],
            loop=app.loop,
            queue=ctx.queue,
            pool=ctx.pool,
            parent_cache=OutputCache(static_data=ctx.cache.copy()),
        )
        try:
            ctx.executor = executor
            await executor.run()
        except Aborted:
            pass
        finally:
            ctx.executor = None
            gc.collect()

        await ctx.queue.put(
            {"event": "finish", "data": {"message": "Successfully ran nodes!"}}
        )
        return json(success_response("Successfully ran nodes!"), status=200)
    except Exception as exception:
        logger.error(exception, exc_info=True)
        logger.error(traceback.format_exc())

        error: ExecutionErrorData = {
            "message": "Error running nodes!",
            "source": None,
            "exception": str(exception),
        }
        if isinstance(exception, NodeExecutionError):
            error["source"] = {
                "nodeId": exception.node_id,
                "schemaId": exception.node_data.schema_id,
                "inputs": exception.inputs,
            }

        await ctx.queue.put({"event": "execution-error", "data": error})
        return json(error_response("Error running nodes!", exception), status=500)


class RunIndividualRequest(TypedDict):
    id: NodeId
    inputs: list[object]
    schemaId: str
    options: JsonExecutionOptions


@app.route("/run/individual", methods=["POST"])
async def run_individual(request: Request):
    """Runs a single node"""
    await nodes_available()
    ctx = AppContext.get(request.app)
    try:
        full_data: RunIndividualRequest = dict(request.json)  # type: ignore
        node_id = full_data["id"]
        if ctx.cache.get(node_id, None) is not None:
            del ctx.cache[node_id]
        logger.debug(full_data)
        exec_opts = ExecutionOptions.parse(full_data["options"])
        set_execution_options(exec_opts)

        chain = Chain()
        chain.add_node(FunctionNode(node_id, full_data["schemaId"]))

        input_map = InputMap()
        input_map.set_values(node_id, full_data["inputs"])

        # only yield certain types of events
        queue = EventConsumer.filter(ctx.queue, {"node-finish", "execution-error"})

        executor = Executor(
            chain=chain,
            inputs=input_map,
            send_broadcast_data=True,
            loop=app.loop,
            queue=queue,
            pool=ctx.pool,
        )

        with run_individual_counter:
            try:
                output = await executor.process_regular_node(node_id)
                ctx.cache[node_id] = output
            except Aborted:
                pass
            finally:
                gc.collect()

        return json({"success": True, "data": None})
    except Exception as exception:
        logger.error(exception, exc_info=True)
        return json({"success": False, "error": str(exception)})


@app.route("/clear-cache/individual", methods=["POST"])
async def clear_cache_individual(request: Request):
    await nodes_available()
    ctx = AppContext.get(request.app)
    try:
        full_data = dict(request.json)  # type: ignore
        if ctx.cache.get(full_data["id"], None) is not None:
            del ctx.cache[full_data["id"]]
        return json({"success": True, "data": None})
    except Exception as exception:
        logger.error(exception, exc_info=True)
        return json({"success": False, "error": str(exception)})


@app.route("/pause", methods=["POST"])
async def pause(request: Request):
    """Pauses the current execution"""
    await nodes_available()
    ctx = AppContext.get(request.app)

    if not ctx.executor:
        message = "No executor to pause"
        logger.warning(message)
        return json(no_executor_response(message), status=400)

    try:
        logger.info("Executor found. Attempting to pause...")
        ctx.executor.pause()
        return json(success_response("Successfully paused execution!"), status=200)
    except Exception as exception:
        logger.log(2, exception, exc_info=True)
        return json(error_response("Error pausing execution!", exception), status=500)


@app.route("/resume", methods=["POST"])
async def resume(request: Request):
    """Pauses the current execution"""
    await nodes_available()
    ctx = AppContext.get(request.app)

    if not ctx.executor:
        message = "No executor to resume"
        logger.warning(message)
        return json(no_executor_response(message), status=400)

    try:
        logger.info("Executor found. Attempting to resume...")
        ctx.executor.resume()
        return json(success_response("Successfully resumed execution!"), status=200)
    except Exception as exception:
        logger.log(2, exception, exc_info=True)
        return json(error_response("Error resuming execution!", exception), status=500)


@app.route("/kill", methods=["POST"])
async def kill(request: Request):
    """Kills the current execution"""
    await nodes_available()
    ctx = AppContext.get(request.app)

    if not ctx.executor:
        message = "No executor to kill"
        logger.warning("No executor to kill")
        return json(no_executor_response(message), status=400)

    try:
        logger.info("Executor found. Attempting to kill...")
        ctx.executor.kill()
        while ctx.executor:
            await asyncio.sleep(0.0001)
        return json(success_response("Successfully killed execution!"), status=200)
    except Exception as exception:
        logger.log(2, exception, exc_info=True)
        return json(error_response("Error killing execution!", exception), status=500)


@app.route("/list-gpus/ncnn", methods=["GET"])
async def list_ncnn_gpus(_request: Request):
    """Lists the available GPUs for NCNN"""
    await nodes_available()
    try:
        # pylint: disable=import-outside-toplevel
        from ncnn_vulkan import ncnn

        result = []
        for i in range(ncnn.get_gpu_count()):
            result.append(ncnn.get_gpu_info(i).device_name())
        return json(result)
    except Exception as exception:
        try:
            from ncnn import ncnn

            result = ["cpu"]
            return json(result)
        except Exception as exception2:
            logger.error(exception, exc_info=True)
            logger.error(exception2, exc_info=True)
            return json([])


@app.route("/list-gpus/nvidia", methods=["GET"])
async def list_nvidia_gpus(_request: Request):
    """Lists the available GPUs for NCNN"""
    await nodes_available()
    try:
        nv = get_nvidia_helper()

        if nv is None:
            return json([])

        result = nv.list_gpus()
        return json(result)
    except Exception as exception:
        logger.error(exception, exc_info=True)
        return json([])


@app.route("/python-info", methods=["GET"])
async def python_info(_request: Request):
    version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    return json({"python": sys.executable, "version": version})


@dataclass
class SystemStat:
    label: str
    percent: float


@app.route("/system-usage", methods=["GET"])
async def system_usage(_request: Request):
    stats_list = []
    cpu_usage = psutil.cpu_percent()
    mem_usage = psutil.virtual_memory().percent
    stats_list.append(SystemStat("CPU", cpu_usage))
    stats_list.append(SystemStat("RAM", mem_usage))
    nv = get_nvidia_helper()
    if nv is not None:
        for i in range(nv.num_gpus):
            total, used, _ = nv.get_current_vram_usage(i)
            stats_list.append(
                SystemStat(
                    f"VRAM {i}" if nv.num_gpus > 1 else "VRAM",
                    used / total * 100,
                )
            )
    return json([asdict(x) for x in stats_list])


@app.route("/packages", methods=["GET"])
async def get_packages(_request: Request):
    await nodes_available()

    packages = []
    for package in api.registry.packages.values():
        if package.name == "chaiNNer_standard":
            continue

        pkg_dependencies = []
        for pkg_dep in package.dependencies:
            installed_version = installed_packages.get(pkg_dep.pypi_name, None)
            pkg_dep_item = {
                **pkg_dep.to_dict(),
            }
            if installed_version is None:
                pkg_dep_item["installed"] = None
            else:
                pkg_dep_item["installed"] = installed_version
            pkg_dependencies.append(pkg_dep_item)

        packages.append(
            {
                "id": package.id,
                "name": package.name,
                "description": package.description,
                "icon": package.icon,
                "color": package.color,
                "dependencies": [d.to_dict() for d in package.dependencies],
                "features": [f.to_dict() for f in package.features],
                "settings": [asdict(x) for x in package.settings],
            }
        )

    return json(packages)


@app.route("/installed-dependencies", methods=["GET"])
async def get_installed_dependencies(_request: Request):
    await nodes_available()

    installed_deps: dict[str, str] = {}
    for package in api.registry.packages.values():
        for pkg_dep in package.dependencies:
            installed_version = installed_packages.get(pkg_dep.pypi_name, None)
            if installed_version is not None:
                installed_deps[pkg_dep.pypi_name] = installed_version

    return json(installed_deps)


@app.route("/features")
async def get_features(_request: Request):
    await nodes_available()

    features: list[tuple[api.Feature, api.Package]] = []
    for package in api.registry.packages.values():
        for feature in package.features:
            features.append((feature, package))

    # check all features in parallel
    async def check(feature: api.Feature) -> api.FeatureState | None:
        if feature.behavior is None:
            # no behavior assigned
            return None

        try:
            return await feature.behavior.check()
        except Exception as e:
            return api.FeatureState.disabled(str(e))

    # because good API design just isn't pythonic, asyncio.gather will return List[Any].
    results: list[api.FeatureState | None] = await asyncio.gather(
        *[check(f) for f, _ in features]
    )

    features_json = []
    for (feature, package), state in zip(features, results):
        if state is None:
            continue

        features_json.append(
            {
                "packageId": package.id,
                "featureId": feature.id,
                "enabled": state.is_enabled,
                "details": state.details,
            }
        )

    return json(features_json)


@app.get("/sse")
async def sse(request: Request):
    ctx = AppContext.get(request.app)
    headers = {"Cache-Control": "no-cache"}
    response = await request.respond(headers=headers, content_type="text/event-stream")
    while True:
        message = await ctx.queue.get()
        if response is not None:
            await response.send(f"event: {message['event']}\n")
            await response.send(f"data: {stringify(message['data'])}\n\n")


@app.get("/setup-sse")
async def setup_sse(request: Request):
    ctx = AppContext.get(request.app)
    headers = {"Cache-Control": "no-cache"}
    response = await request.respond(headers=headers, content_type="text/event-stream")
    while True:
        message = await ctx.setup_queue.get()
        if response is not None:
            await response.send(f"event: {message['event']}\n")
            await response.send(f"data: {stringify(message['data'])}\n\n")


async def import_packages(
    config: ServerConfig,
    update_progress_cb: UpdateProgressFn,
):
    async def install_deps(dependencies: list[api.Dependency]):
        dep_info: list[DependencyInfo] = [
            {
                "package_name": dep.pypi_name,
                "display_name": dep.display_name,
                "version": dep.version,
                "from_file": None,
            }
            for dep in dependencies
        ]
        await install_dependencies(dep_info, update_progress_cb, logger)

    # Manually import built-in packages to get ordering correct
    # Using importlib here so we don't have to ignore that it isn't used
    importlib.import_module("packages.chaiNNer_standard")
    importlib.import_module("packages.chaiNNer_pytorch")
    importlib.import_module("packages.chaiNNer_ncnn")
    importlib.import_module("packages.chaiNNer_onnx")
    importlib.import_module("packages.chaiNNer_external")

    logger.info("Checking dependencies...")

    to_install: list[api.Dependency] = []
    for package in api.registry.packages.values():
        logger.info(f"Checking dependencies for {package.name}...")

        if config.install_builtin_packages:
            to_install.extend(package.dependencies)
            continue

        if package.name == "chaiNNer_standard":
            to_install.extend(package.dependencies)

        # check auto updates
        for dep in package.dependencies:
            is_installed = installed_packages.get(dep.pypi_name, None) is not None
            if dep.auto_update and is_installed:
                to_install.append(dep)

    if len(to_install) > 0:
        try:
            await install_deps(to_install)
        except Exception as ex:
            logger.error(f"Error installing dependencies: {ex}")
            if config.close_after_start:
                raise ValueError("Error installing dependencies") from ex

    logger.info("Done checking dependencies...")

    # TODO: in the future, for external packages dir, scan & import
    # for package in os.listdir(packages_dir):
    #     importlib.import_module(package)

    await update_progress_cb("Loading Nodes...", 1.0, None)

    load_errors = api.registry.load_nodes(__file__)
    if len(load_errors) > 0:
        import_errors: list[api.LoadErrorInfo] = []
        for e in load_errors:
            if not isinstance(e.error, ModuleNotFoundError):
                logger.warning(f"Failed to load {e.module} ({e.file}):")
                logger.warning(e.error)
            else:
                import_errors.append(e)

        if len(import_errors) > 0:
            logger.warning(f"Failed to import {len(import_errors)} modules:")
            for e in import_errors:
                logger.warning(f"{e.error}  ->  {e.module}")

        if config.error_on_failed_node:
            raise ValueError("Error importing nodes")


async def apple_silicon_setup():
    # enable mps fallback on apple silicon
    os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"


async def setup(sanic_app: Sanic):
    setup_queue = AppContext.get(sanic_app).setup_queue

    async def update_progress(
        message: str, progress: float, status_progress: float | None = None
    ):
        await setup_queue.put_and_wait(
            {
                "event": "backend-status",
                "data": {
                    "message": message,
                    "progress": progress,
                    "statusProgress": status_progress,
                },
            },
            timeout=1,
        )

    logger.info("Starting setup...")
    await setup_queue.put_and_wait(
        {
            "event": "backend-started",
            "data": None,
        },
        timeout=1,
    )

    if is_arm_mac:
        await apple_silicon_setup()

    await update_progress("Importing nodes...", 0.0, None)

    logger.info("Importing nodes...")

    # Now we can load all the nodes
    await import_packages(AppContext.get(sanic_app).config, update_progress)

    logger.info("Sending backend ready...")

    await update_progress("Loading Nodes...", 1.0, None)

    await setup_queue.put_and_wait(
        {
            "event": "backend-ready",
            "data": None,
        },
        timeout=1,
    )

    logger.info("Done.")


exit_code = 0


async def close_server(sanic_app: Sanic):
    # pylint: disable=global-statement
    global exit_code

    try:
        await nodes_available()
    except Exception as ex:
        logger.error(f"Error waiting for server to start: {ex}")
        exit_code = 1

    # now we can close the server
    logger.info("Closing server...")
    sanic_app.stop()


@app.after_server_start
async def after_server_start(sanic_app: Sanic, loop: asyncio.AbstractEventLoop):
    # pylint: disable=global-statement
    global setup_task

    # initialize the queues
    ctx = AppContext.get(sanic_app)
    ctx.queue = EventQueue()
    ctx.setup_queue = EventQueue()

    # start the setup task
    setup_task = loop.create_task(setup(sanic_app))

    # start task to close the server
    if ctx.config.close_after_start:
        loop.create_task(close_server(sanic_app))


def main():
    config = ServerConfig.parse_argv()
    AppContext.get(app).config = config
    app.run(port=config.port, single_process=True)
    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
