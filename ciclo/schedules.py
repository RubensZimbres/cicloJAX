from typing import Union
from datetime import datetime, timedelta

from ciclo.api import Elapsed, Period


class every:
    def __init__(
        self,
        steps: Union[int, None] = None,
        *,
        samples: Union[int, None] = None,
        time: Union[timedelta, float, int, None] = None,
        steps_offset: int = 0,
    ) -> None:
        self.period = Period(steps=steps, samples=samples, time=time)
        self.last_samples: int = 0
        self.last_time: float = datetime.now().timestamp()
        self.steps_offset: int = steps_offset

    def __call__(self, elapsed: Elapsed) -> bool:

        if self.period.steps is not None:
            steps = elapsed.steps - self.steps_offset
            return steps >= 0 and steps % self.period.steps == 0

        if self.period.samples is not None:
            if elapsed.samples - self.last_samples >= self.period.samples:
                self.last_samples = elapsed.samples
                return True

        if self.period.time is not None:
            if elapsed.date - self.last_time >= self.period.time:
                self.last_time = elapsed.date
                return True

        return False
