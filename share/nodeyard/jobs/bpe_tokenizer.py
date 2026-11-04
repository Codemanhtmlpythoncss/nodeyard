"""Byte-pair encoding (BPE) tokenizer, trained from scratch on the chosen text.

Method: start from the 256 single bytes. Repeatedly find the most frequent pair of
adjacent tokens, give that pair a new token ID, and replace every occurrence. Repeat
until the vocabulary reaches the requested size.

DO NOT CHANGE the pre-tokenisation pattern or the merge tie-breaking without
retraining: saved models depend on both. Pair counts are updated incrementally and
kept in a heap, so 10,000 merges on a few megabytes of text take seconds, not hours.
"""

from __future__ import annotations

import heapq
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

# Words, punctuation, and their leading spaces form the pieces that BPE may merge.
PRETOKEN_PATTERN = re.compile(r" ?[A-Za-z]+| ?[0-9]+| ?[^\sA-Za-z0-9]+|\s+")


class BPETokenizer:
    def __init__(self, merges: list[tuple[int, int]]):
        self.merges = [tuple(pair) for pair in merges]
        self.ranks = {pair: rank for rank, pair in enumerate(self.merges)}
        self.vocab: list[bytes] = [bytes([value]) for value in range(256)]
        for left, right in self.merges:
            self.vocab.append(self.vocab[left] + self.vocab[right])
        self._cache: dict[str, list[int]] = {}

    @property
    def vocab_size(self) -> int:
        return len(self.vocab)

    @classmethod
    def train(cls, text: str, vocab_size: int) -> "BPETokenizer":
        if vocab_size < 256:
            raise ValueError("The vocabulary must have at least 256 entries (one per byte).")
        chunk_counts = Counter(PRETOKEN_PATTERN.findall(text))
        words = [[list(chunk.encode("utf-8")), count] for chunk, count in chunk_counts.items()]
        pair_counts: Counter[tuple[int, int]] = Counter()
        where: dict[tuple[int, int], set[int]] = defaultdict(set)
        for index, (ids, count) in enumerate(words):
            for pair in zip(ids, ids[1:]):
                pair_counts[pair] += count
                where[pair].add(index)
        # Max-heap by count; ties broken by the smaller pair so training is deterministic.
        heap = [(-count, pair) for pair, count in pair_counts.items()]
        heapq.heapify(heap)
        merges: list[tuple[int, int]] = []
        next_id = 256
        while next_id < vocab_size and heap:
            negative, pair = heapq.heappop(heap)
            if pair_counts.get(pair, 0) != -negative:
                continue  # stale heap entry
            if pair_counts[pair] < 2:
                break  # nothing repeats any more
            merges.append(pair)
            new_id = next_id
            next_id += 1
            for index in list(where[pair]):
                ids, count = words[index]
                for old in zip(ids, ids[1:]):
                    pair_counts[old] -= count
                    if pair_counts[old] <= 0:
                        del pair_counts[old]
                merged: list[int] = []
                position = 0
                while position < len(ids):
                    if position + 1 < len(ids) and ids[position] == pair[0] and ids[position + 1] == pair[1]:
                        merged.append(new_id)
                        position += 2
                    else:
                        merged.append(ids[position])
                        position += 1
                words[index][0] = merged
                for new_pair in zip(merged, merged[1:]):
                    pair_counts[new_pair] += count
                    where[new_pair].add(index)
                    heapq.heappush(heap, (-pair_counts[new_pair], new_pair))
            where.pop(pair, None)
            pair_counts.pop(pair, None)
        return cls(merges)

    def _encode_chunk(self, chunk: str) -> list[int]:
        cached = self._cache.get(chunk)
        if cached is not None:
            return cached
        ids = list(chunk.encode("utf-8"))
        while len(ids) > 1:
            candidates = [(self.ranks[pair], pair) for pair in zip(ids, ids[1:]) if pair in self.ranks]
            if not candidates:
                break
            _, best = min(candidates)
            merged: list[int] = []
            position = 0
            while position < len(ids):
                if position + 1 < len(ids) and (ids[position], ids[position + 1]) == best:
                    merged.append(self._id_of(best))
                    position += 2
                else:
                    merged.append(ids[position])
                    position += 1
            ids = merged
        if len(self._cache) < 200_000:
            self._cache[chunk] = ids
        return ids

    def _id_of(self, pair: tuple[int, int]) -> int:
        return 256 + self.ranks[pair]

    def encode(self, text: str) -> list[int]:
        output: list[int] = []
        for chunk in PRETOKEN_PATTERN.findall(text):
            output.extend(self._encode_chunk(chunk))
        return output

    def decode(self, ids) -> str:
        return b"".join(self.vocab[int(token)] for token in ids).decode("utf-8", errors="replace")

    def save(self, path: Path) -> None:
        path.write_text(json.dumps({"merges": self.merges}), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "BPETokenizer":
        return cls([tuple(pair) for pair in json.loads(path.read_text(encoding="utf-8"))["merges"]])
