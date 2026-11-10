"""Learning-rate governor: lowers the rate while validation loss is not improving.

Rule: validation is checked during training. If it fails to beat the best value so far
(by at least min_delta, relative) for `patience` checks in a row, the learning rate is
multiplied by `factor`. Reductions repeat while validation keeps stalling and stop as
soon as it improves, so the rate settles at the level that makes progress. The rate
never goes below min_fraction of the starting rate.

The defaults are deliberately patient. Late in training validation improves by tiny
amounts per check; an earlier version (0.2% better within 2 checks, floor 2%) read that
as stalling, cut the rate a dozen times in a row and left training flat at almost zero.

DO NOT CHANGE the LEARNING_RATE line: the app reads it to move the learning-rate slider.
"""

from __future__ import annotations

from typing import Callable


class LearningRateGovernor:
    def __init__(self, initial_rate: float, factor: float = 0.7, patience: int = 4,
                 min_delta: float = 0.0, min_fraction: float = 0.1):
        if not 0.0 < factor < 1.0:
            raise ValueError("The reduction factor must be between 0 and 1.")
        self.factor = factor
        self.patience = patience
        self.min_delta = min_delta
        self.min_rate = initial_rate * min_fraction
        self.best: float | None = None
        self.stalled = 0
        self.reductions = 0
        self.at_floor = False

    def observe(self, loss: float, rate: float, step: int, emit: Callable[[str], None]) -> float | None:
        """Record one validation loss. Returns a lower learning rate when one is due, else None."""
        improved = self.best is None or loss < self.best * (1.0 - self.min_delta)
        if self.best is None or loss < self.best:
            self.best = loss
        if improved:
            if self.stalled or self.at_floor:
                emit(f"Validation improved again at step {step} ({loss:.4f}); the learning rate stays at {rate:.3g}.")
            self.stalled = 0
            self.at_floor = False
            return None
        self.stalled += 1
        if self.stalled < self.patience:
            return None
        self.stalled = 0
        if rate <= self.min_rate * 1.000001:
            if not self.at_floor:
                emit(f"Validation is still not improving at the lowest learning rate ({rate:.3g}). "
                     "More text or a larger model is needed to go lower.")
            self.at_floor = True
            return None
        new_rate = max(self.min_rate, rate * self.factor)
        self.reductions += 1
        emit(f"Validation has not improved for {self.patience} checks (best {self.best:.4f}, latest {loss:.4f}). "
             f"Lowering the learning rate from {rate:.3g} to {new_rate:.3g}.")
        emit(f"LEARNING_RATE | {new_rate:.8g}")
        return new_rate
