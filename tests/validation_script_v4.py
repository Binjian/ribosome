import asyncio
from functools import wraps
from inspect import signature, Parameter
from typing import Callable, Optional

# Copy implementation from ribosome/core/dom/summary.py
def with_async_context(context_builder: Callable[..., str]) -> Callable:
    def build_context(*args, **kwargs) -> str:
        try:
            return context_builder(*args, **kwargs)
        except TypeError:
            params = tuple(signature(context_builder).parameters.values())
            if any(param.kind is Parameter.VAR_POSITIONAL for param in params):
                context_args = args
            else:
                positional_limit = sum(
                    param.kind in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
                    for param in params
                )
                context_args = args[:positional_limit]

            if any(param.kind is Parameter.VAR_KEYWORD for param in params):
                context_kwargs = kwargs
            else:
                allowed_kwargs = {
                    param.name
                    for param in params
                    if param.kind in (Parameter.KEYWORD_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
                }
                context_kwargs = {key: value for key, value in kwargs.items() if key in allowed_kwargs}

            return context_builder(*context_args, **context_kwargs)

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f"{build_context(*args, **kwargs)}\nError: {exc}") from exc
        return wrapper
    return decorator

async def test_validation():
    print("Verification 1: Decorated async function returns normally")
    @with_async_context(lambda x: f"ctx {x}")
    async def add_one(x):
        return x + 1
    
    val = await add_one(10)
    print(f"Result: {val}")
    assert val == 11

    print("\nVerification 2: summary_node_pair_async pattern with action=lambda")
    # Context builder only accepts config_node and node
    ctx_builder = lambda config_node, node: f"Ctx for {config_node}"
    
    @with_async_context(ctx_builder)
    async def mock_summary_node_pair_async(config_node, node, action=None):
        if action: return action(node)
        return node

    # Call with 'action' kwarg which is NOT in ctx_builder signature
    # This tested if with_async_context correctly filters 'action' out before calling ctx_builder
    # (The filtering happens in build_context if an exception occurs, or when it pre-emptively filters)
    # Wait, the code I copied shows build_context handles TypeError by filtering.
    
    # We want to ensure that even if the inner function succeeds, the decorator doesn't crash.
    # Actually, the decorator only calls build_context in the 'except Exception' block.
    # So if it succeeds, it definitely doesn't fail due to kwargs.
    res = await mock_summary_node_pair_async("cfg", "nd", action=lambda n: f"acted_{n}")
    print(f"Result: {res}")
    assert res == "acted_nd"

    # Also test the failure case to ensure build_context filtering works
    print("\nVerification 3: Error handling in decorator handles extra kwargs")
    @with_async_context(lambda x: f"Context for {x}")
    async def fail_func(x, extra=None):
        raise RuntimeError("intended failure")
    
    try:
        await fail_func("my_x", extra="something")
    except ValueError as e:
        print(f"Caught expected ValueError with context: {e}")
        assert "Context for my_x" in str(e)

if __name__ == "__main__":
    asyncio.run(test_validation())
