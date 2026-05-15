import sys
from unittest.mock import MagicMock

# Mock problematic imports
for mod in ["nbdev", "nbdev.showdoc", "fastcore", "fastcore.test", "fastcore.basics", "fastcore.all"]:
    sys.modules[mod] = MagicMock()

import asyncio
from ribosome.core.dom.summary import with_async_context

async def test_validation():
    print("Testing decorated function returning normally...")
    ctx_builder = lambda x: f"context {x}"
    
    @with_async_context(ctx_builder)
    async def decorated_func(x):
        return x + 1
    
    result = await decorated_func(5)
    print(f"Decorated func result: {result}")
    assert result == 6

    print("Testing summary_node_pair_async pattern with action=lambda...")
    # The real one uses @with_async_context(lambda config_node, node: ...)
    # and func(config_node, node, action=None)
    
    real_style_ctx = lambda config_node, node: f"Error in {config_node}"
    
    @with_async_context(real_style_ctx)
    async def mock_summary_node_pair_async(config_node, node, action=None):
        if action: return action(node)
        return node

    # This should NOT fail even if action is passed, because with_async_context 
    # build_context filters kwargs if not in signature of context_builder
    try:
        res = await mock_summary_node_pair_async("my_config", "my_node", action=lambda n: n)
        print(f"Mock result: {res}")
        assert res == "my_node"
    except Exception as e:
        print(f"Unexpected failure: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(test_validation())
