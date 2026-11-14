"""Learning-rate governor: lowers the rate while validation loss is not improving.

Rule: validation is checked during training. If it fails to beat the best value so far
(by at least min_delta, relative) for `patience` checks in a row, the learning rate is
multiplied by a factor. Reductions repeat while validation keeps stalling and stop as
soon as it improves, so the rate settles at the level that makes progress. The rate
never goes below min_fraction of the starting rate.

How big each cut is tracks how *unsteady* the training loss has been. A jumpy, zig-zagging
loss means the steps are overshooting, so a stall there is cut hard (down to `factor_jumpy`);
a smooth loss that has merely levelled off is near a good spot, so its cut is gentle (up to
`factor_steady`). Unsteadiness is measured from the recent training losses fed in through
`note_train_loss`; with none recorded the plain `factor` is used, so behaviour is unchanged
for callers that only report validation.

The defaults are deliberately patient. Late in training validation improves by tiny
amounts per check; an earlier version (0.2% better within 2 checks, floor 2%) read that
as stalling, cut the rate a dozen times in a row and left training flat at almost zero.

DO NOT CHANGE the LEARNING_RATE line: the app reads it to move the learning-rate slider.
"""

from __future__ import annotations

from collections import deque
from typing import Callable, Deque


def loss_unsteadiness(losses) -> float:
    """How jagged a run of losses is, from 0 (a smooth monotone line) to 1 (pure zig-zag, no net progress).

    The efficiency ratio: net change divided by the total up-and-down travelled. A loss that only ever
    falls travels exactly its net drop, so efficiency 1 and unsteadiness 0; one that wanders up and down
    as much as it falls travels far more than its net, so unsteadiness nears 1. It needs no units, so it
    is comparable across models and loss scales.
    """
    xs = [float(x) for x in losses]
    if len(xs) < 2:
        return 0.0
    total_travel = sum(abs(b - a) for a, b in zip(xs, xs[1:]))
    if total_travel <= 1e-12:
        return 0.0
    net = abs(xs[-1] - xs[0])
    return max(0.0, min(1.0, 1.0 - net / total_travel))


class LearningRateGovernor:
    def __init__(self, initial_rate: float, factor: float = 0.7, patience: int = 4,
                 min_delta: float = 0.0, min_fraction: float = 0.1,
                 factor_steady: float = 0.85, factor_jumpy: float = 0.5, noise_window: int = 40):
        if not 0.0 < factor < 1.0:
            raise ValueError("The reduction factor must be between 0 and 1.")
        if not 0.0 < factor_jumpy <= factor_steady < 1.0:
            raise ValueError("Need 0 < factor_jumpy <= factor_steady < 1.")
        self.factor = factor
        self.factor_steady = factor_steady
        self.factor_jumpy = factor_jumpy
        self.patience = patience
        self.min_delta = min_delta
        self.min_rate = initial_rate * min_fraction
        self.best: float | None = None
        self.stalled = 0
        self.reductions = 0
        self.at_floor = False
        self._recent: Deque[float] = deque(maxlen=max(4, noise_window))

    def note_train_loss(self, loss: float) -> None:
        """Record one training-loss value, so the size of the next cut can follow how jumpy the loss is."""
        if loss is not None and loss == loss:   # skip NaN
            self._recent.append(float(loss))

    def _cut_factor(self) -> tuple[float, float | None]:
        """The multiplier for the next cut, and the unsteadiness it came from (None when too little data)."""
        if len(self._recent) < 8:
            return self.factor, None
        u = loss_unsteadiness(self._recent)
        factor = self.factor_steady - (self.factor_steady - self.factor_jumpy) * u
        return factor, u

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
        factor, unsteadiness = self._cut_factor()
        new_rate = max(self.min_rate, rate * factor)
        self.reductions += 1
        noisy = "" if unsteadiness is None else (
            f" The loss has been {'jumpy' if unsteadiness > 0.5 else 'fairly steady'} "
            f"(unsteadiness {unsteadiness:.2f}), so the cut is {'firm' if factor < 0.7 else 'gentle'} (×{factor:.2f}).")
        emit(f"Validation has not improved for {self.patience} checks (best {self.best:.4f}, latest {loss:.4f}). "
             f"Lowering the learning rate from {rate:.3g} to {new_rate:.3g}.{noisy}")
        emit(f"LEARNING_RATE | {new_rate:.8g}")
        return new_rate
