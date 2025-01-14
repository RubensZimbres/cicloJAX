from functools import partial
import functools
import importlib.util
import inspect
import threading
from abc import abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Tuple,
    Type,
    TypeVar,
    Union,
)

import jax
from flax import struct
from flax.core import tracers
from flax.training import train_state
from typing_extensions import Protocol, runtime_checkable
from ciclo.api import (
    Broadcasts,
    LoopCallback,
    LoopCallbackBase,
    CallbackOutput,
    Elapsed,
    LogsLike,
    State,
    Statics,
    inject,
    register_adapter,
    Batch,
    Metric,
    LoopState,
    FunctionCallbackOutputs,
    to_standard_outputs,
)

from ciclo.strategies import Strategy, get_strategy


@runtime_checkable
class HasStrategy(Protocol):
    strategy: Strategy


@runtime_checkable
class HasBatchStats(Protocol):
    batch_stats: Any


Loss = jax.Array

S = TypeVar("S", bound="ManagedState")
A = TypeVar("A")
B = TypeVar("B")

ManagedCallbackCallable = Callable[[Batch, S, Broadcasts, Statics], CallbackOutput[S]]


@runtime_checkable
class ManagedCallback(Protocol, Generic[S]):
    def __managed_callback__(
        self, state: S, batch: Batch, broadcast: Broadcasts, statics: Statics
    ) -> CallbackOutput[S]:
        ...

    def get_function_with_input_signature(self) -> Callable:
        ...


@dataclass(frozen=True)
class ManagedFunctionCallback(ManagedCallback[S]):
    f: Callable[..., FunctionCallbackOutputs[S]]

    def __managed_callback__(
        self, state: S, batch: Batch, broadcast: Broadcasts, statics: Statics
    ) -> CallbackOutput[S]:
        outputs = inject(self.f, state, batch, broadcast, statics)
        return to_standard_outputs(outputs, state)

    def get_function_with_input_signature(
        self,
    ) -> Callable[..., FunctionCallbackOutputs[S]]:
        return self.f


class ManagedState(train_state.TrainState):
    """
    A train state that manages the strategy.
    """

    strategy: "Strategy" = struct.field(pytree_node=False)

    @classmethod
    def create(
        cls: Type[S],
        *,
        apply_fn,
        params,
        tx,
        strategy: Union[Strategy, str] = "jit",
        **kwargs,
    ) -> S:
        state = super().create(
            apply_fn=apply_fn,
            params=params,
            tx=tx,
            strategy=get_strategy("eager"),
            **kwargs,
        )
        return state.with_strategy(strategy)

    def with_strategy(self, strategy: Union[Strategy, str]) -> "ManagedState":
        new_strategy = get_strategy(strategy) if isinstance(strategy, str) else strategy
        current_strategy = self.strategy
        if new_strategy == current_strategy:
            return self
        state = current_strategy.to_host(self)
        state = new_strategy.from_host(state)
        return state.replace(strategy=new_strategy)


@dataclass
class ManagedStep(LoopCallbackBase[S]):
    strategy_callbacks: Dict[Strategy, ManagedCallbackCallable[S]]
    default_strategy: Strategy
    managed_step_fn: ManagedCallback[S]

    def __call__(self, state: S, *args: Any) -> CallbackOutput[S]:

        if len(args) > 3:
            raise ValueError(f"Expected a maximum of 4 arguments, got {len(args) + 1}")

        batch, broadcasts, statics = args + (None,) * (3 - len(args))

        if isinstance(state, HasStrategy):
            strategy = state.strategy
            assert isinstance(strategy, Strategy)
        else:
            strategy = self.default_strategy

        if strategy not in self.strategy_callbacks:
            self.strategy_callbacks[strategy] = strategy(
                self.get_final_callback(strategy)
            )

        callback = self.strategy_callbacks[strategy]

        batch = strategy.lift_batch(batch)
        logs, state = callback(state, batch, broadcasts, statics)

        for collection in logs.keys():
            if collection == "stateful_metrics":
                logs[collection] = strategy.lower_replicated(logs[collection])
            elif collection in ("losses", "metrics"):
                logs[collection] = strategy.lower_averageable(logs[collection])
            elif collection == "per_sample_outputs":
                logs[collection] = strategy.lower_tileable(logs[collection])
            else:
                logs[collection] = strategy.lower_sharded(logs[collection])

        return logs, state

    def get_final_callback(self, strategy: Strategy) -> ManagedCallbackCallable[S]:
        # @functools.wraps(self.managed_step_fn.get_function_with_input_signature())
        def lifted_postprocess(
            state: S, batch: Batch, broadcasts: Broadcasts, statics: Statics
        ) -> CallbackOutput[S]:
            step_fn = self.get_step_callback(strategy)
            logs, state = step_fn(state, batch, broadcasts, statics)

            if "stateful_metrics" in logs:
                stateful_metrics = logs["stateful_metrics"]
                assert isinstance(stateful_metrics, MutableMapping)
                for key, value in stateful_metrics.items():
                    if isinstance(value, Metric):
                        metric: Metric = getattr(state, key)
                        value = strategy.handle_metric(value)
                        metric = metric.merge(value)
                        state = state.replace(**{key: metric})
                        metric_value = metric.compute()
                        if isinstance(metric_value, Mapping):
                            stateful_metrics.update(metric_value)
                        else:
                            stateful_metrics[key] = metric_value
            return logs, state

        return lifted_postprocess

    def get_step_callback(self, strategy: Strategy) -> ManagedCallbackCallable[S]:
        return self.managed_step_fn.__managed_callback__

    def __loop_callback__(self, loop_state: LoopState[S]) -> CallbackOutput[S]:
        logs, state = self(loop_state.state, loop_state.batch, loop_state.elapsed, None)
        return logs, state


@dataclass
class ManagedTrainStep(ManagedStep[S]):
    def get_step_callback(self, strategy: Strategy) -> ManagedCallbackCallable[S]:
        def train_step_callback(
            state: S, batch: Batch, broadcasts: Broadcasts, statics: Statics
        ) -> CallbackOutput[S]:
            def loss_fn(params):
                _state = state.replace(params=params)
                logs, _state = self.managed_step_fn.__managed_callback__(
                    _state, batch, broadcasts, statics
                )
                if "losses" not in logs:
                    raise ValueError(
                        f"callback must return dictorionary with a 'losses' key, but got {logs.keys()}"
                    )
                if len(logs["losses"]) == 0:
                    raise ValueError(
                        "'losses' collection is empty, you must provide at least one entry "
                        "in the 'losses' collection"
                    )

                loss = 0.0
                for k, v in logs["losses"].items():
                    if v.shape != ():
                        raise ValueError(
                            f"Loss {k} should be a scalar, but has shape {v.shape}"
                        )
                    loss += v

                return loss, (logs, _state)

            logs: LogsLike
            (_, (logs, state)), grads = jax.value_and_grad(loss_fn, has_aux=True)(
                state.params
            )
            grads = strategy.handle_grads(grads)
            state = state.apply_gradients(grads=grads)

            if isinstance(state, HasBatchStats):
                batch_stats = strategy.handle_batch_stats(state.batch_stats)
                state = state.replace(batch_stats=batch_stats)

            return logs, state

        return train_step_callback


def train_step(
    step_fn: Callable[..., FunctionCallbackOutputs[S]],
    strategy: Union[Strategy, str] = "jit",
) -> ManagedTrainStep[S]:
    strategy = get_strategy(strategy) if isinstance(strategy, str) else strategy
    return ManagedTrainStep(
        strategy_callbacks={},
        default_strategy=strategy,
        managed_step_fn=ManagedFunctionCallback(step_fn),
    )


def step(
    step_fn: Callable[..., FunctionCallbackOutputs[S]],
    strategy: Union[Strategy, str] = "jit",
) -> ManagedStep[S]:
    strategy = get_strategy(strategy) if isinstance(strategy, str) else strategy
    return ManagedStep(
        strategy_callbacks={},
        default_strategy=strategy,
        managed_step_fn=ManagedFunctionCallback(step_fn),
    )
