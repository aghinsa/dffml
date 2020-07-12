import asyncio

from dffml.distribuited.orchestrator import (
    NatsPrimaryNode,
    NatsPrimaryNodeConfig,
)
from dffml import DataFlow
from dffml_feature_git.feature.operations import (
    check_if_valid_git_repository_URL,
)
from dffml.operation.output import GetSingle


async def main():
    server = "0.0.0.0:4222"
    primary_node = NatsPrimaryNode(
        NatsPrimaryNodeConfig(
            server=server,
            dataflow=DataFlow.auto(
                GetSingle, check_if_valid_git_repository_URL
            ),
        )
    )

    async with primary_node as pn:
        async with pn() as pnctx:
            pass


asyncio.run(main())