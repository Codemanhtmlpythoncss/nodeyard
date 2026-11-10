"""Read the weights in a model.pt that PyTorch saved, without PyTorch: the dashboard shows every number of a model.

torch.save writes a zip: data.pkl (a pickle describing each tensor: its storage, offset, shape and strides) and
data/<key> (each storage's raw bytes, little-endian). This unpickles data.pkl with stand-ins for the few torch
functions it names, so nothing from the file is ever executed, and reads float32 values straight from the zip.
"""
import collections
import pickle
import struct
import zipfile

DTYPES = {"FloatStorage": ("f", 4), "DoubleStorage": ("d", 8), "HalfStorage": ("e", 2), "LongStorage": ("q", 8),
          "IntStorage": ("i", 4), "BFloat16Storage": (None, 2)}


class Tensor:
    def __init__(self, storage, offset, shape, stride):
        self.storage, self.offset, self.shape, self.stride = storage, offset, tuple(shape), tuple(stride)

    @property
    def count(self):
        n = 1
        for s in self.shape:
            n *= s
        return n


class _Storage:
    def __init__(self, kind, key):
        self.kind, self.key = kind, key


def _rebuild_tensor(storage, offset, shape, stride, *_rest):
    return Tensor(storage, offset, shape, stride)


class _Unpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module == "collections" and name == "OrderedDict":
            return collections.OrderedDict
        if module == "torch._utils" and name in ("_rebuild_tensor_v2", "_rebuild_tensor"):
            return _rebuild_tensor
        if module == "torch" and name in DTYPES:
            return name
        raise pickle.UnpicklingError("unexpected %s.%s in a model file" % (module, name))

    def persistent_load(self, pid):
        # ("storage", <storage type>, key, location, numel)
        if not (isinstance(pid, tuple) and pid and pid[0] == "storage"):
            raise pickle.UnpicklingError("unexpected persistent id")
        kind = pid[1] if isinstance(pid[1], str) else getattr(pid[1], "__name__", "FloatStorage")
        return _Storage(kind, str(pid[2]))


class Model:
    """The tensors of one model.pt, read lazily: open it once, then ask for rows."""

    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        names = self.zip.namelist()
        pkl = next(n for n in names if n.endswith("/data.pkl") or n == "data.pkl")
        self.prefix = pkl[: -len("data.pkl")]
        with self.zip.open(pkl) as f:
            self.tensors = _Unpickler(f).load()
        self.cache = {}

    def close(self):
        self.zip.close()

    def shapes(self):
        return [{"name": k, "shape": list(t.shape), "count": t.count} for k, t in self.tensors.items()]

    def _storage(self, st):
        if st.key not in self.cache:
            if len(self.cache) > 8:
                self.cache.clear()
            self.cache[st.key] = self.zip.read(self.prefix + "data/" + st.key)
        return self.cache[st.key]

    def rows(self, name, start=0, count=None):
        """Rows [start, start+count) of a tensor as lists of floats (a 1-D tensor is one row)."""
        t = self.tensors[name]
        code, size = DTYPES.get(t.storage.kind, ("f", 4))
        if code is None:
            raise ValueError("bfloat16 weights aren't supported here")
        raw = self._storage(t.storage)
        shape = t.shape if len(t.shape) >= 2 else (1,) + t.shape
        stride = t.stride if len(t.stride) >= 2 else (0,) + t.stride
        n_rows, n_cols = shape[0], 1
        for s in shape[1:]:
            n_cols *= s
        start = max(0, min(start, n_rows))
        end = n_rows if count is None else min(n_rows, start + count)
        out = []
        contiguous = stride[-1] == 1 and (len(shape) == 2)
        for r in range(start, end):
            base = t.offset + r * stride[0]
            if contiguous:
                out.append(list(struct.unpack_from("<%d%s" % (n_cols, code), raw, base * size)))
            else:
                out.append([struct.unpack_from("<" + code, raw, (base + c * stride[-1]) * size)[0] for c in range(n_cols)])
        return out
