import asyncio
from ribosome.core.dom.summary import with_async_context

async def test_validation():
    # 1) Verify a decorated async function still returns normally
    @with_async_context
    async def decorated_func(x):
        return x + 1
    
    result = await decorated_func(5)
    print(f"Decorated func result: {result}")
    assert result == 6

    # 2) Verify summary_node_pair_async with action=lambda node: node
    # Since we can't easily mock the whole infrastructure, we test if with_async_context
    # handles unexpected kwargs properly by looking at how it's implemented or 
    # by simulating the call that might pass extra kwargs.
    
    @with_async_context
    async def mock_summary_node_pair_async(action, **kwargs):
        return action("test_node")

    try:
        # Simulate invocation with action=lambda node: node
        # We also pass an extra kwarg that might be injected by the decorator or infrastructure
        res = await mock_summary_node_pair_async(action=lambda node: node, extra_arg="dummy")
        print(f"Mock summary_node_pair_async result: {res}")
        assert res == "test_node"
    except Exception as e:
        print(f"Failed with exception: {e}")
        raise

if __name__ == "__main__":
    asyncio.run(test_validation())
