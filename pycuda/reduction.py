"""Computation of reductions on vectors."""

from __future__ import division

__copyright__ = "Copyright (C) 2009 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person
obtaining a copy of this software and associated documentation
files (the "Software"), to deal in the Software without
restriction, including without limitation the rights to use,
copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following
conditions:

The above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.

Based on code/ideas by Mark Harris <mharris@nvidia.com>.

Original License:

Copyright 1993-2007 NVIDIA Corporation.  All rights reserved.

NOTICE TO USER:

This source code is subject to NVIDIA ownership rights under U.S. and
international Copyright laws.

NVIDIA MAKES NO REPRESENTATION ABOUT THE SUITABILITY OF THIS SOURCE
CODE FOR ANY PURPOSE.  IT IS PROVIDED "AS IS" WITHOUT EXPRESS OR
IMPLIED WARRANTY OF ANY KIND.  NVIDIA DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOURCE CODE, INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY, NONINFRINGEMENT, AND FITNESS FOR A PARTICULAR PURPOSE.
IN NO EVENT SHALL NVIDIA BE LIABLE FOR ANY SPECIAL, INDIRECT, INCIDENTAL,
OR CONSEQUENTIAL DAMAGES, OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS
OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE
OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE
OR PERFORMANCE OF THIS SOURCE CODE.

U.S. Government End Users.  This source code is a "commercial item" as
that term is defined at 48 C.F.R. 2.101 (OCT 1995), consisting  of
"commercial computer software" and "commercial computer software
documentation" as such terms are used in 48 C.F.R. 12.212 (SEPT 1995)
and is provided to the U.S. Government only as a commercial end item.
Consistent with 48 C.F.R.12.212 and 48 C.F.R. 227.7202-1 through
227.7202-4 (JUNE 1995), all U.S. Government End Users acquire the
source code with only those rights set forth herein.
"""




from pytools import memoize
from pycuda.tools import dtype_to_ctype




def get_reduction_module(out_type, block_size,
        neutral, reduce_expr, map_expr, arguments,
        name="reduce_kernel", keep=False, options=[], preamble=""):

    from pycuda.compiler import SourceModule
    src = """
        #define BLOCK_SIZE %(block_size)d
        #define READ_AND_MAP(i) (%(map_expr)s)
        #define REDUCE(a, b) (%(reduce_expr)s)

        typedef %(out_type)s out_type;

        %(preamble)s

        __global__ void %(name)s(out_type *out, %(arguments)s, 
          unsigned int seq_count, unsigned int n)
        {
          __shared__ out_type sdata[BLOCK_SIZE];

          unsigned int tid = threadIdx.x;

          unsigned int i = blockIdx.x*BLOCK_SIZE*seq_count + tid;

          out_type acc = %(neutral)s;
          for (unsigned s = 0; s < seq_count; ++s)
          { 
            if (i >= n)
              break;
            acc = REDUCE(acc, READ_AND_MAP(i)); 

            i += BLOCK_SIZE; 
          }

          sdata[tid] = acc;

          __syncthreads();

          #if (BLOCK_SIZE >= 512) 
            if (tid < 256) { sdata[tid] = REDUCE(sdata[tid], sdata[tid + 256]); }
            __syncthreads();
          #endif

          #if (BLOCK_SIZE >= 256) 
            if (tid < 128) { sdata[tid] = REDUCE(sdata[tid], sdata[tid + 128]); } 
            __syncthreads(); 
          #endif

          #if (BLOCK_SIZE >= 128) 
            if (tid < 64) { sdata[tid] = REDUCE(sdata[tid], sdata[tid + 64]); } 
            __syncthreads(); 
          #endif

          if (tid < 32) 
          {
            if (BLOCK_SIZE >= 64) sdata[tid] = REDUCE(sdata[tid], sdata[tid + 32]);
            if (BLOCK_SIZE >= 32) sdata[tid] = REDUCE(sdata[tid], sdata[tid + 16]);
            if (BLOCK_SIZE >= 16) sdata[tid] = REDUCE(sdata[tid], sdata[tid + 8]);
            if (BLOCK_SIZE >= 8)  sdata[tid] = REDUCE(sdata[tid], sdata[tid + 4]);
            if (BLOCK_SIZE >= 4)  sdata[tid] = REDUCE(sdata[tid], sdata[tid + 2]);
            if (BLOCK_SIZE >= 2)  sdata[tid] = REDUCE(sdata[tid], sdata[tid + 1]);
          }

          if (tid == 0) out[blockIdx.x] = sdata[0];
        }
        """ % {
            "out_type": out_type,
            "arguments": arguments,
            "block_size": block_size,
            "neutral": neutral,
            "reduce_expr": reduce_expr,
            "map_expr": map_expr,
            "name": name,
            "preamble": preamble
            }
    return SourceModule(src, options=options, keep=keep)




def get_reduction_kernel_and_types(out_type, block_size,
        neutral, reduce_expr, map_expr=None, arguments=None,
        name="reduce_kernel", keep=False, options=[], preamble=""):
    if map_expr is None:
        map_expr = "in[i]"

    if arguments is None:
        arguments = "const %s *in" % out_type

    mod = get_reduction_module(out_type, block_size,
            neutral, reduce_expr, map_expr, arguments,
            name, keep, options, preamble)

    from pycuda.tools import get_arg_type
    func = mod.get_function(name)
    arg_types = [get_arg_type(arg) for arg in arguments.split(",")]
    func.prepare("P%sII" % "".join(arg_types), (block_size,1,1))

    return func, arg_types




class ReductionKernel:
    def __init__(self, dtype_out, 
            neutral, reduce_expr, map_expr=None, arguments=None,
            name="reduce_kernel", keep=False, options=[]):

        self.dtype_out = dtype_out

        self.block_size = 512

        s1_func, self.stage1_arg_types = get_reduction_kernel_and_types(
                dtype_to_ctype(dtype_out), self.block_size, 
                neutral, reduce_expr, map_expr,
                arguments, name=name+"_stage1", keep=keep, options=options)
        self.stage1_func = s1_func.prepared_async_call

        # stage 2 has only one input and no map expression
        s2_func, self.stage2_arg_types = get_reduction_kernel_and_types(
                dtype_to_ctype(dtype_out), self.block_size, 
                neutral, reduce_expr,
                name=name+"_stage2", keep=keep, options=options)
        self.stage2_func = s2_func.prepared_async_call

        assert [i for i, arg_tp in enumerate(self.stage1_arg_types) if arg_tp == "P"], \
                "ReductionKernel can only be used with functions that have at least one " \
                "vector argument"

    def __call__(self, *args, **kwargs):
        MAX_BLOCK_COUNT = 1024
        SMALL_SEQ_COUNT = 4

        s1_func = self.stage1_func
        s2_func = self.stage2_func

        kernel_wrapper = kwargs.get("kernel_wrapper")
        if kernel_wrapper is not None:
            s1_func = kernel_wrapper(s1_func)
            s2_func = kernel_wrapper(s2_func)

        stream = kwargs.get("stream")

        from gpuarray import empty

        f = s1_func
        arg_types = self.stage1_arg_types

        while True:
            invocation_args = []
            vectors = []

            for arg, arg_tp in zip(args, arg_types):
                if arg_tp == "P":
                    vectors.append(arg)
                    invocation_args.append(arg.gpudata)
                else:
                    invocation_args.append(arg)

            repr_vec = vectors[0]
            sz = repr_vec.size

            if sz <= self.block_size*SMALL_SEQ_COUNT*MAX_BLOCK_COUNT:
                total_block_size = SMALL_SEQ_COUNT*self.block_size
                block_count = (sz + total_block_size - 1) // total_block_size
                seq_count = SMALL_SEQ_COUNT
            else:
                block_count = MAX_BLOCK_COUNT
                macroblock_size = block_count*self.block_size
                seq_count = (sz + macroblock_size - 1) // macroblock_size

            if block_count == 1:
                result = empty((), self.dtype_out, repr_vec.allocator)
            else:
                result = empty((block_count,), self.dtype_out, repr_vec.allocator)

            #print block_count, seq_count, self.block_size
            f((block_count, 1), stream,
                    *([result.gpudata]+invocation_args+[seq_count, sz]))

            if block_count == 1:
                return result
            else:
                f = s2_func
                arg_types = self.stage2_arg_types
                args = [result]




@memoize
def get_sum_kernel(dtype_out, dtype_in):
    if dtype_out is None:
        dtype_out = dtype_in

    return ReductionKernel(dtype_out, "0", "a+b",
            arguments="const %(tp)s *in" % {"tp": dtype_to_ctype(dtype_in)},
            keep=True)




@memoize
def get_dot_kernel(dtype_out, dtype_a=None, dtype_b=None):
    if dtype_out is None:
        dtype_out = dtype_a

    if dtype_b is None:
        if dtype_a is None:
            dtype_b = dtype_out
        else:
            dtype_b = dtype_a

    if dtype_a is None:
        dtype_a = dtype_out

    return ReductionKernel(dtype_out, neutral="0", 
            reduce_expr="a+b", map_expr="a[i]*b[i]", 
            arguments="const %(tp_a)s *a, const %(tp_b)s *b" % { 
                "tp_a": dtype_to_ctype(dtype_a),
                "tp_b": dtype_to_ctype(dtype_b), 
                }, keep=True)
            



@memoize
def get_subset_dot_kernel(dtype_out, dtype_a=None, dtype_b=None):
    if dtype_out is None:
        dtype_out = dtype_a

    if dtype_b is None:
        if dtype_a is None:
            dtype_b = dtype_out
        else:
            dtype_b = dtype_a

    if dtype_a is None:
        dtype_a = dtype_out

    # important: lookup_tbl must be first--it controls the length
    return ReductionKernel(dtype_out, neutral="0", 
            reduce_expr="a+b", map_expr="a[lookup_tbl[i]]*b[lookup_tbl[i]]", 
            arguments="const unsigned int *lookup_tbl, "
            "const %(tp_a)s *a, const %(tp_b)s *b" % {
            "tp_a": dtype_to_ctype(dtype_a),
            "tp_b": dtype_to_ctype(dtype_b), 
            })