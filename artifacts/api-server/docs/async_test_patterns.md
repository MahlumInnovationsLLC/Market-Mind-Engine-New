# Async test patterns

The pytest suite runs **serially in a single process** (`pytest tests/`), so a
test that leaves the event loop or a coroutine in a bad state can corrupt every
test that runs after it. Two rules keep async tests hermetic.

## 1. Never drive a coroutine on a torn-down loop

`RuntimeWarning: coroutine '...' was never awaited` and
`RuntimeError: Event loop is closed` are the tell-tale symptoms. They happen
when a test awaits something that reaches a **module-level** resource (most
often `app.db` or a singleton bound to an earlier test's loop). The root
`pyproject.toml` promotes `RuntimeWarning` to an error
(`filterwarnings = ["error::RuntimeWarning"]`) so these leaks fail loudly
instead of silently breaking a later test.

Prefer the standard library runner that creates and tears down a fresh loop per
call:

```python
import asyncio

def _run(coro):
    return asyncio.run(coro)  # fresh loop each call; cleans up on exit
```

If `pytest-asyncio` is available, an `async def` test marked with
`@pytest.mark.asyncio` is equivalent and reads more cleanly. Avoid the
`asyncio.get_event_loop().run_until_complete(...)` + manual
`new_event_loop()/close()` fixture pattern — it relies on global loop state and
is the usual source of cross-file "Event loop is closed" failures.

## 2. Mock every db touchpoint the code path actually reaches

Patching only the `db` symbol you can see from the test is not enough. A wired
code path can fan out into other modules' `db` references (e.g. a live
prediction cycle reaches `app.risk.gates.db` and the registry predictors, not
just `in_game_loop.db`). Either:

- patch `db` at **each** import site the path uses, or
- stub the higher-level entrypoints (e.g. the predictor functions) so the path
  never reaches the real db at all.

When a feature gets wired for a new sport/market, a test that assumed the
"unwired / no-op" path will start exercising real db calls. Re-stub the new
entrypoints rather than relying on the loop happening to be alive.
