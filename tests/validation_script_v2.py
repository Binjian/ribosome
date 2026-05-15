import sys
from unittest.mock import MagicMock

# Mock nbdev before importing ribosome
sys.modules["nbdev"] = MagicMock()
sys.modules["nbdev.showdoc"] = MagicMock()

import asyncio
from ribosome.core.dom.summary import with_async_context

async def test_validation():
    # 1) Verify a decorated async function still returns normally
    # Define a dummy context builder for the decorator
    ctx_builder = lambda *args, **kwargs: "context"
    
    @with_async_context(ctx_builder)
    async def decorated_func(x):
        return x + 1
    
    result = await decorated_func(5)
    print(f"Decorated func result: {result}")
    assert result == 6

    # 2) Verify summary_node_pair_async pattern
    # The decorator is called with a lambda: 
    # @with_async_context(lambda config_node, node: ...)
    
    mock_ctx_builder = lambda config_node, node: f"Ctx for {config_node}"
    
    @with_async_context(mock_ctx_builder)
    async def mock_summary_node_pair_async(config_node, node, action=None):
        if action:
            return action(node)
        return node

    # Invoke with action=lambda node: node
    # This passes config_node, node as positional and action as keyword
    res = await mock_summary_node_pair_async("config", "real_node", action=lambda n: n)
    print(f"Mock summary_node_pair_async result: {res}")
    assert res == "real_node"

if __name__ == "__main__":
    asyncio.run(test_validation())
