"""Next-token sampling for generation, shared by both trainers.

DO NOT CHANGE the defaults without reading sample output: sampling from the full
vocabulary at a high temperature lets one unlikely token (often a rare byte pair)
derail the rest of the reply. Top-k keeps only the most likely tokens, top-p keeps
the smallest set holding most of the probability, and the repetition penalty
discourages the model from looping on the same words.
"""

from __future__ import annotations

import numpy as np


def sample_token(logits, rng: np.random.Generator, recent=(), temperature: float = 0.7,
                 top_k: int = 40, top_p: float = 0.9, repetition_penalty: float = 1.15) -> int:
    z = np.array(logits, dtype=np.float64)
    if repetition_penalty > 1.0 and len(recent):
        ids = np.unique(np.asarray(list(recent), dtype=np.int64))
        ids = ids[(ids >= 0) & (ids < z.shape[0])]
        values = z[ids]
        z[ids] = np.where(values > 0, values / repetition_penalty, values * repetition_penalty)
    z = z / max(float(temperature), 1e-3)
    if 0 < top_k < z.shape[0]:
        cutoff = np.partition(z, -top_k)[-top_k]
        z = np.where(z >= cutoff, z, -np.inf)
    z = z - np.max(z)
    probabilities = np.exp(z)
    probabilities /= probabilities.sum()
    if 0.0 < top_p < 1.0:
        order = np.argsort(-probabilities)
        sorted_probabilities = probabilities[order]
        keep = (np.cumsum(sorted_probabilities) - sorted_probabilities) < top_p
        mask = np.zeros(probabilities.shape[0], dtype=bool)
        mask[order[keep]] = True
        probabilities = np.where(mask, probabilities, 0.0)
        probabilities /= probabilities.sum()
    return int(rng.choice(probabilities.shape[0], p=probabilities))
