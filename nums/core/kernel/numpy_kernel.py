# Copyright (C) 2020 NumS Development Team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import operator
import random

import numpy as np
import scipy.linalg
import scipy.special
from numpy.random import Generator
from numpy.random import PCG64

from nums.core.kernel.kernel_interface import KernelImp, RNGInterface
from nums.core.grid.grid import ArrayGrid
from nums.core.settings import np_ufunc_map


def block_rng(seed, jump_index):
    return Generator(PCG64(seed).jumped(jump_index))


class RNG(RNGInterface):
    # A naive approach to implementing a parallel random state is to simply
    # increment the random seed given by the user, but this
    # will lead to many collisions if the user is also incrementing the random seed,
    # so a more principled approach is needed.
    # In particular, our generator should work just as serial generators would
    # if a block array is generated with a random seed of 0, and then
    # a random seed of 1.
    # See: https://numpy.org/doc/stable/reference/random/parallel.html
    # The easiest parallel RNG approach for NumS is to jump the BitGenerator state.
    # The way this works is as follows:
    # The user provides a seed, which we use to seed all bit generators.
    # Whenever a new array is sampled, increment the jump index for each block in the array,
    # and in the remote function, sample the block shape using a newly initialized
    # random state with the user provided seed and the jump index created for the block.
    # Blocks are sampled for new arrays by incrementing the jump index as before.
    # This can be viewed simply as incrementing the jump index whenever a new block needs to be
    # sampled, regardless of the array the block belongs to.
    # A global random state is maintained, just like in numpy, so that
    # a random state is not required to sample numbers.
    # The seed can be set for the global random state, which
    # re-instantiates the global random state.

    # One issue is the following:
    # nrs1 = NPRandom(1337)
    # nrs2 = NPRandom(1337)
    # x1 = nrs1.sample(shape=(100,), block_shape=(10,))
    # x2 = nrs1.sample(shape=(100,), block_shape=(11,))
    # x1 != x2
    # x1 performs more jumps because it has more blocks.
    # One way to remedy this is to always sample using the default internal block shape,
    # and then reshape to the required block shape.
    # This is of course sub-optimal -- we could alternatively jump according to a small
    # block shape, and generate as many jumps needed to sample the required block shape,
    # but this is tedious, and since we won't expose block_shape as a parameter in the final
    # api, the proposed approach works fine as-is.

    def __init__(self, seed=None, jump_index=0):
        # pylint: disable=no-member
        if seed is None:
            seed = random.getrandbits(128)
        self.seed = seed
        self.rng = PCG64(seed)
        self.jump_index = jump_index

    def new_block_rng_params(self):
        params = self.seed, self.jump_index
        self.jump_index += 1
        return params


class KernelCls(KernelImp):
    # pylint: disable=no-member, unused-variable

    # I/O operations.
    def touch(self, arr):
        return isinstance(arr, np.ndarray)

    def new_block(self, op_name, grid_entry, grid_meta):
        op_func = np.__getattribute__(op_name)
        grid = ArrayGrid.from_meta(grid_meta)
        block_shape = grid.get_block_shape(grid_entry)
        if op_name == "eye":
            assert np.all(np.diff(grid_entry) == 0)
            return op_func(*block_shape, dtype=grid.dtype)
        else:
            return op_func(block_shape, dtype=grid.dtype)

    def random_block(self, rng_params, rfunc_name, rfunc_args, shape, dtype):
        rng: Generator = block_rng(*rng_params)
        op_func = rng.__getattribute__(rfunc_name)
        result = op_func(*rfunc_args).reshape(shape)
        if rfunc_name not in ("random", "integers"):
            # Only random and integer supports sampling of a specific type.
            result = result.astype(dtype)
        return result

    def permutation(self, rng_params, size):
        rng: Generator = block_rng(*rng_params)
        return rng.permutation(size)

    def create_block(self, *src_arrs, src_params, dst_params, dst_shape, dst_shape_bc):
        result = np.empty(shape=dst_shape, dtype=src_arrs[0].dtype)
        assert len(src_params) == len(dst_params)
        for i in range(len(src_params)):
            src_arr: np.ndarray = src_arrs[i]
            src_sel, srcT = src_params[i]
            if srcT:
                src_arr = src_arr.T
            dst_sel, dstT = dst_params[i]
            if dst_shape_bc is not None:
                result.reshape(dst_shape_bc)[dst_sel] = src_arr[src_sel]
            else:
                result[dst_sel] = src_arr[src_sel]
        return result

    def update_block(self, dst_arr, *src_arrs, src_params, dst_params):
        assert len(src_params) == len(dst_params)
        # We need to copy here. If we modify this after a no-copy assignment
        # of a block from array A to B, modifying B will modify the contents of A.
        dst_arr = dst_arr.copy()
        _, dstT = dst_params[0]
        if dstT:
            dst_arr = dst_arr.T
        for i in range(len(src_params)):
            src_arr: np.ndarray = src_arrs[i]
            src_sel, src_shape_bc, srcT = src_params[i]
            if srcT:
                src_arr = src_arr.T
            dst_sel, dstT = dst_params[i]
            if src_shape_bc is not None:
                dst_arr[dst_sel] = src_arr.reshape(src_shape_bc)[src_sel]
            else:
                dst_arr[dst_sel] = src_arr[src_sel]
        return dst_arr

    def update_block_by_index(self, dst_arr, src_arr, index_pairs):
        result = dst_arr.copy()
        for dst_index, src_index in index_pairs:
            result[tuple(dst_index)] = src_arr[tuple(src_index)]
        return result

    def advanced_select_block_along_axis(
        self,
        dst_arr_or_args,
        src_arr,
        ss,
        dst_axis,
        src_axis,
        dst_coord,
        src_coord,
    ):
        # Can this be optimized using sparse arrays?

        # We don't want to create or copy the destination array
        # unless we're performing an operation on it.
        # We just need the destination array's shape
        # to determine whether we need to modify it.
        if isinstance(dst_arr_or_args, np.ndarray):
            dst_arr_shape = dst_arr_or_args.shape
        else:
            dst_arr_shape, _ = dst_arr_or_args

        # Note that here, we only compute the set of indices that need to be updated for axis.
        # Slice and index subscript bounds are tested in the control process.
        # This could be optimized, but in its current state, it's simple and not a bottleneck.
        array = ss[src_axis]
        dst_vec = np.arange(len(array))
        dst_mask = (dst_coord[dst_axis] <= dst_vec) & (
            dst_vec < dst_coord[dst_axis] + dst_arr_shape[dst_axis]
        )
        src_vec = np.array(array)
        src_mask = (src_coord[src_axis] <= src_vec) & (
            src_vec < src_coord[src_axis] + src_arr.shape[src_axis]
        )
        mask = dst_mask & src_mask
        src_vec = src_vec[mask] - src_coord[src_axis]
        dst_vec = dst_vec[mask] - dst_coord[dst_axis]
        if dst_vec.shape[0] == 0:
            # Nothing to do for this array.
            # Return input args.
            # We do this to save on copy operations,
            # which dominate the execution time of this function.
            return dst_arr_or_args

        if isinstance(dst_arr_or_args, np.ndarray):
            # We need to update the array.
            dst_arr = dst_arr_or_args.copy()
        else:
            # We allocate an array of zeros on initial update.
            dst_arr = np.zeros(shape=dst_arr_or_args[0], dtype=dst_arr_or_args[1])

        # Create and apply the subscript argument.
        src_sel, dst_sel = [], []
        for i in range(len(ss)):
            if i == src_axis:
                src_sel.append(src_vec)
            elif isinstance(ss[i], slice):
                src_sel.append(ss[i])
            else:
                src_sel.append(ss[i] - src_coord[i])
            if i == dst_axis:
                dst_sel.append(dst_vec)
            elif isinstance(ss[i], slice):
                dst_sel.append(ss[i])
        dst_arr[tuple(dst_sel)] = src_arr[tuple(src_sel)]
        return dst_arr

    def advanced_assign_block_along_axis(
        self, dst_arr, src_arr, ss, axis, dst_coord, src_coord
    ):

        # Note that here, we only compute the set of indices that need to be updated for axis.
        # Slice and index subscript bounds are tested in the control process.
        # This could be optimized, but in its current state, it's simple and not a bottleneck.
        array = ss[axis]
        dst_vec = np.array(array)
        dst_mask = (dst_coord[axis] <= dst_vec) & (
            dst_vec < dst_coord[axis] + dst_arr.shape[axis]
        )

        # Compute a different mask for different assignment operations.
        # For select operations, src_arr matches the dims of dst_arr.
        if len(src_arr.shape) == 0:
            # scalar
            mask = dst_mask
        elif len(src_arr.shape) == 1:
            src_vec = np.arange(len(array))
            src_mask = (src_coord[0] <= src_vec) & (
                src_vec < src_coord[0] + src_arr.shape[0]
            )
            mask = dst_mask & src_mask
            src_vec = src_vec[mask] - src_coord[0]
        else:
            src_vec = np.arange(len(array))
            src_mask = (src_coord[axis] <= src_vec) & (
                src_vec < src_coord[axis] + src_arr.shape[axis]
            )
            mask = dst_mask & src_mask
            src_vec = src_vec[mask] - src_coord[axis]
        dst_vec = dst_vec[mask] - dst_coord[axis]

        if dst_vec.shape[0] == 0:
            # Nothing to do for this array.
            return dst_arr

        dst_arr = dst_arr.copy()
        # Create and apply the subscript argument.
        dst_sel = []
        for i in range(len(ss)):
            if i == axis:
                dst_sel.append(dst_vec)
            elif isinstance(ss[i], slice):
                dst_sel.append(ss[i])
            else:
                dst_sel.append(ss[i] - dst_coord[i])

        if len(src_arr.shape) == 0:
            # scalar
            dst_arr[tuple(dst_sel)] = src_arr
        elif len(src_arr.shape) == 1:
            # single-dim
            dst_arr[tuple(dst_sel)] = src_arr[(src_vec,)]
        else:
            # multi-dim
            src_sel = [slice(None, None)] * len(ss)
            src_sel[axis] = src_vec
            dst_arr[tuple(dst_sel)] = src_arr[tuple(src_sel)]
        return dst_arr

    def diag(self, arr, offset):
        return np.diag(arr, k=offset)

    def arange(self, start, stop, step, dtype):
        return np.arange(start, stop, step, dtype)

    def sum_reduce(self, *arrs):
        return np.add.reduce(arrs)

    def reduce_axis(self, op_name, arr, axis, keepdims, transposed):
        op_func = np.__getattribute__(op_name)
        if transposed:
            arr = arr.T
        if (
            axis is not None
            and arr.shape[axis] > 1
            and op_name == "sum"
            and not keepdims
        ):
            # We can optimize this with matmul or tensordot.
            if len(arr.shape) == 2:
                assert axis in (0, 1)
                if axis == 0:
                    return np.matmul(np.ones(arr.shape[0]), arr)
                else:
                    return np.matmul(arr, np.ones(arr.shape[1]))
            else:
                return np.tensordot(np.ones(arr.shape[axis]), arr, axes=(0, axis))
        return op_func(arr, axis=axis, keepdims=keepdims)

    # This is essentially a map.
    def map_uop(self, op_name, arr, args, kwargs):
        ufunc = np.__getattribute__(op_name)
        return ufunc(arr, *args, **kwargs)

    def where(self, arr, x, y, block_slice_tuples):
        if x is None:
            assert y is None
            res = np.where(arr)
            for i, (start, stop) in enumerate(block_slice_tuples):
                arr = res[i]
                arr += start
            shape = res[0].shape
            res = list(res)
            res.append(shape)
            return tuple(res)
        else:
            assert isinstance(x, np.ndarray) and isinstance(y, np.ndarray)
            assert arr.shape == x.shape == y.shape
            return np.where(arr, x, y)

    def xlogy(self, arr_x, arr_y):
        return scipy.special.xlogy(arr_x, arr_y)

    def astype(self, arr, dtype_str):
        dtype = getattr(np, dtype_str)
        return arr.astype(dtype)

    def transpose(self, arr):
        return arr.T

    def swapaxes(self, arr, axis1, axis2):
        return arr.swapaxes(axis1, axis2)

    def split(self, arr, indices_or_sections, axis, transposed):
        if transposed:
            arr = arr.T
        return np.split(arr, indices_or_sections, axis)

    def shape_dtype(self, arr):
        return arr.shape, arr.dtype

    def size(self, arr):
        return arr.size

    def tdigest_chunk(self, arr):
        # pylint: disable = import-outside-toplevel
        from crick import TDigest

        t = TDigest()
        t.update(arr)
        return t

    def percentiles_from_tdigest(self, q, *digests):
        # pylint: disable = import-outside-toplevel
        from crick import TDigest

        t = TDigest()
        t.merge(*digests)
        return np.array(t.quantile(q))

    def select_median(self, arr):
        """Find value in `arr` closest to median as part of quickselect algorithm."""
        assert arr.ndim == 1, "Only 1D 'arr' is supported."
        if arr.size == 0:
            return 0  # Dummy value that has no effect on weighted median.
        index = arr.size // 2
        return np.partition(arr, index)[index]

    def weighted_median(self, *arr_and_weights):
        """Find the weighted median of an array."""
        mid = len(arr_and_weights) // 2
        arr, weights = arr_and_weights[:mid], arr_and_weights[mid:]
        argsorted_arr = np.argsort(arr)
        sorted_weights_sum = np.cumsum(np.take(weights, argsorted_arr))
        half = sorted_weights_sum[-1] / 2
        return arr[argsorted_arr[np.searchsorted(sorted_weights_sum, half)]]

    def pivot_partition(
        self,
        arr,
        pivot: int,
        op: str,
    ):
        """Return all elements in `arr` for which the comparsion to `pivot` is True."""
        if arr.size == 0:
            return 0, arr
        ops = {
            "gt": operator.gt,
            "lt": operator.lt,
            "ge": operator.ge,
            "le": operator.le,
            "eq": operator.eq,
            "ne": operator.ne,
        }
        assert op in ops, "'op' must be a valid comparison operator."
        comp = arr[ops[op](arr, pivot)]
        return comp.size, comp

    def bop(self, op, a1, a2, a1_T, a2_T, axes):
        if a1_T:
            a1 = a1.T
        if a2_T:
            a2 = a2.T
        if op == "tensordot":
            if axes == 1 and max(len(a1.shape), len(a2.shape)) <= 2:
                # Execute this as a matmul.
                # TODO: Outer product is optimized.
                #  detect here and execute np.outer(...)
                return np.matmul(a1, a2)
            return np.tensordot(a1, a2, axes=axes)
        op = np_ufunc_map.get(op, op)
        try:
            ufunc = np.__getattribute__(op)
        except Exception as _:
            ufunc = scipy.special.__getattribute__(op)
        return ufunc(a1, a2)

    def bop_reduce(self, op, a1, a2, a1_T, a2_T):
        if a1_T:
            a1 = a1.T
        if a2_T:
            a2 = a2.T

        # These are faster.
        if op == "sum":
            r = a1 + a2
        elif op == "prod":
            r = a1 * a2
        else:
            reduce_op = np.__getattribute__(op)
            a = np.stack([a1, a2], axis=0)
            r = reduce_op(a, axis=0, keepdims=False)

        if a1 is np.nan or a2 is np.nan or r is np.nan:
            assert np.isscalar(a1) and np.isscalar(a2) and np.isscalar(r)
        else:
            assert a1.shape == a2.shape == r.shape
        return r

    def qr(self, *arrays, mode="reduced", axis=None):
        if len(arrays) > 1:
            assert axis is not None
            arr = np.concatenate(arrays, axis=axis)
        else:
            arr = arrays[0]
        return np.linalg.qr(arr, mode=mode)

    def cholesky(self, arr):
        return np.linalg.cholesky(arr)

    def svd(self, arr):
        u, sigma, vT = np.linalg.svd(arr)
        u = u[: sigma.shape[0]]
        return u, sigma, vT

    def inv(self, arr):
        return np.linalg.inv(arr)

    # Boolean

    def array_compare(self, func_name: str, a: np.ndarray, b: np.ndarray, args):
        eq_func = getattr(np, func_name)
        if func_name == "allclose":
            assert len(args) == 2
            rtol = args[0]
            atol = args[1]
            return np.allclose(a, b, rtol, atol)

        assert len(args) == 0
        return eq_func(a, b)

    # Logic

    def logical_and(self, *bool_list):
        return np.all(bool_list)

    def arg_op(
        self, op_name, arr, block_slice, other_argoptima=None, other_optima=None
    ):
        if op_name == "argmin":
            arr_argmin = np.argmin(arr)
            arr_min = arr[arr_argmin]
            if other_optima is not None and other_optima < arr_min:
                return other_argoptima, other_optima
            return block_slice.start + arr_argmin, arr_min
        elif op_name == "argmax":
            arr_argmax = np.argmax(arr)
            arr_max = arr[arr_argmax]
            if other_optima is not None and other_optima > arr_max:
                return other_argoptima, other_optima
            return block_slice.start + arr_argmax, arr_max
        else:
            raise Exception("Unsupported arg op.")

    def reshape(self, arr, shape):
        return arr.reshape(shape)

    def identity(self, value):
        return value
