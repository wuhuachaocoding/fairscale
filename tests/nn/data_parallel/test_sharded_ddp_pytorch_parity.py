# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

"""
Testing ShardedDDP
"""

from contextlib import suppress
import copy
import tempfile

import numpy as np
import pytest
import torch
from torch.cuda.amp import GradScaler as TorchGradScaler
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn import Linear, Sequential
from torch.nn.parallel import DistributedDataParallel as DDP

from fairscale.nn.data_parallel import ShardedDataParallel
from fairscale.optim import OSS
from fairscale.optim.grad_scaler import ShardedGradScaler
from fairscale.utils.testing import check_same_model_params, skip_if_no_cuda, skip_if_single_gpu

"""
Check that ShardedDDP gets the same results as DDP in a variety of scenarii
"""

_test_fp16_reduction = [False]

if hasattr(dist, "algorithms.ddp_com_hooks.default_hooks"):
    _test_fp16_reduction.append(True)


def _get_mlp():
    return Sequential(Linear(2, 3), Linear(3, 3), Linear(3, 3), Linear(3, 3), Linear(3, 3), Linear(3, 3))


def run_ddp_parity(
    rank, world_size, backend, temp_file_name, reduce_buffer_size, grad_accumulation, change_train_graph, fp16_reduction
):
    dist.init_process_group(init_method="file://" + temp_file_name, backend=backend, rank=rank, world_size=world_size)

    device = torch.device("cuda")
    torch.cuda.set_device(rank)
    torch.manual_seed(rank)
    np.random.seed(rank)
    NUMBER_BATCHS = 5
    BATCH_SIZE = 8

    def check_parity(amp: bool, manual_reduction: bool):

        # The API should be the exact same in between the sharded and non-sharded variants, generic closure
        def closure(model, scaler, input_tensor, should_accumulate, _manual_reduction=False):
            accumulate_steps = 3 if should_accumulate else 1

            model.zero_grad()

            def step():
                if scaler is not None:
                    with torch.cuda.amp.autocast():
                        loss = model(input_tensor).abs().sum()
                        scaler.scale(loss).backward()
                else:
                    loss = model(input_tensor).abs().sum()
                    loss.backward()

            with model.no_sync() if should_accumulate else suppress():
                for _ in range(accumulate_steps - 1):
                    step()

            if not _manual_reduction:
                step()
            else:
                with model.no_sync():
                    step()

                model.reduce()

        # Any model works. Add one different buffer per rank
        model = _get_mlp()
        model.register_buffer("test_buffer", torch.ones((1)) * rank)
        model.to(device)

        # Make sure that the model starts with non-trainable, so that we check for the buckets to be
        # properly reassigned when/if this changes
        next(model.parameters()).requires_grad = False

        sharded_optimizer = OSS(params=model.parameters(), optim=torch.optim.SGD, lr=1e-4, momentum=0.99)
        sharded_ddp_model = ShardedDataParallel(
            module=model,
            sharded_optimizer=sharded_optimizer,
            broadcast_buffers=True,
            reduce_buffer_size=reduce_buffer_size,
            reduce_fp16=fp16_reduction,
        )

        ddp_model_single = copy.deepcopy(model)
        ddp_optimizer = torch.optim.SGD(ddp_model_single.parameters(), lr=1e-4, momentum=0.99)
        ddp_model = DDP(ddp_model_single, device_ids=[rank], broadcast_buffers=True, find_unused_parameters=True)

        if fp16_reduction:
            from dist.algorithms.ddp_com_hooks.default_hooks import fp16_compress_hook

            ddp_model.register_comm_hook(state=None, hook=fp16_compress_hook)  # type: ignore

        ddp_scaler = TorchGradScaler() if amp else None
        sharded_ddp_scaler = ShardedGradScaler() if amp else None

        # The model should be synchronized in between the ranks at construction time, check that
        check_same_model_params(sharded_ddp_model, ddp_model)

        # Typical training loop, check that we get the exact same results as DDP
        for i in range(NUMBER_BATCHS):
            input_tensor = torch.rand((BATCH_SIZE, 2)).to(device)

            def closure_ddp(input_tensor=input_tensor):
                return closure(ddp_model, ddp_scaler, input_tensor, grad_accumulation)

            def closure_sharded(input_tensor=input_tensor):
                return closure(
                    sharded_ddp_model,
                    sharded_ddp_scaler,
                    input_tensor,
                    grad_accumulation,
                    _manual_reduction=manual_reduction,
                )

            # Step/scale both
            if ddp_scaler is not None:
                _ = closure_ddp(input_tensor)
                ddp_scaler.step(ddp_optimizer)
                ddp_scaler.update()
            else:
                ddp_optimizer.step(closure=closure_ddp)

            if sharded_ddp_scaler is not None:
                _ = closure_sharded(input_tensor)
                sharded_ddp_scaler.step(sharded_optimizer)
                sharded_ddp_scaler.update()
            else:
                sharded_optimizer.step(closure=closure_sharded)

            check_same_model_params(sharded_ddp_model, ddp_model, f"Rank: {rank} - Step {i} broke")

            # Flip the trainability of the first parameter back and forth
            if i == 0 and change_train_graph:
                next(sharded_ddp_model.parameters()).requires_grad = not next(
                    sharded_ddp_model.parameters()
                ).requires_grad
                next(ddp_model.parameters()).requires_grad = not next(ddp_model.parameters()).requires_grad
                check_same_model_params(sharded_ddp_model, ddp_model, f"Rank: {rank} - Trainability refresh {i} broke")

    # Test all combinations: AMP, Accumulate, Change train graph, reduce buckets
    amp_tests = [False]
    if hasattr(torch.cuda.amp, "autocast"):
        amp_tests.append(True)

    manual_reductions = [False, True] if not grad_accumulation and not change_train_graph else [False]
    for manual_reduction in manual_reductions:
        for amp in amp_tests:
            print(
                f"Checking configuration: accumulate {grad_accumulation}"
                + f" - change train graph {change_train_graph}"
                + f" - amp {amp}"
                + f" - manual reduction {manual_reduction}"
                + f" - buffers {reduce_buffer_size}",
                flush=True,
            )
            check_parity(
                amp=amp, manual_reduction=manual_reduction,
            )

    dist.destroy_process_group()


@skip_if_no_cuda
@skip_if_single_gpu
@pytest.mark.parametrize("reduce_buffer_size", [0, 2 ** 20])
@pytest.mark.parametrize("grad_accumulation", [True, False])
@pytest.mark.parametrize("change_train_graph", [True, False])
@pytest.mark.parametrize("fp16_reduction", _test_fp16_reduction)
def test_ddp_parity(reduce_buffer_size, grad_accumulation, change_train_graph, fp16_reduction):
    world_size = torch.cuda.device_count()
    backend = dist.Backend.NCCL
    mp.spawn(
        run_ddp_parity,
        args=(
            world_size,
            backend,
            tempfile.mkstemp()[1],
            reduce_buffer_size,
            grad_accumulation,
            change_train_graph,
            fp16_reduction,
        ),
        nprocs=world_size,
        join=True,
    )


def run_ddp_parity_two_optim(rank, world_size, backend, temp_file_name, reduce_buffer_size):
    dist.init_process_group(init_method="file://" + temp_file_name, backend=backend, rank=rank, world_size=world_size)
    device = torch.device("cuda")
    torch.cuda.set_device(rank)
    torch.manual_seed(rank)
    np.random.seed(rank)  # Any model works. Add one different buffer per rank

    BATCHS = 20

    model = _get_mlp()
    model.register_buffer("test_buffer", torch.ones((1)) * rank)
    model.to(device)
    n_half_params = len(list(model.parameters())) // 2
    optim_settings = {"lr": 1e-3, "momentum": 0.99}

    sharded_optimizer = OSS(params=list(model.parameters())[:n_half_params], optim=torch.optim.SGD, **optim_settings)
    sharded_optimizer_2 = OSS(params=list(model.parameters())[n_half_params:], optim=torch.optim.SGD, **optim_settings)

    sharded_ddp_model = ShardedDataParallel(
        module=model,
        sharded_optimizer=[sharded_optimizer, sharded_optimizer_2],
        broadcast_buffers=True,
        reduce_buffer_size=reduce_buffer_size,
    )

    ddp_model_single = copy.deepcopy(model)
    ddp_optimizer = torch.optim.SGD(list(ddp_model_single.parameters())[:n_half_params], **optim_settings)
    ddp_optimizer_2 = torch.optim.SGD(list(ddp_model_single.parameters())[n_half_params:], **optim_settings)
    ddp_model = DDP(ddp_model_single, device_ids=[rank], broadcast_buffers=True)

    check_same_model_params(
        sharded_ddp_model,
        ddp_model,
        f"DDP parity two optim test failing. differing at startup, Buffers {reduce_buffer_size}",
    )

    for i in range(BATCHS):
        input_tensor = torch.rand((64, 2)).to(device)

        # Run DDP
        ddp_optimizer.zero_grad()
        ddp_optimizer_2.zero_grad()
        ddp_loss = ddp_model(input_tensor).abs().sum()
        ddp_loss.backward()
        ddp_optimizer.step()
        ddp_optimizer_2.step()
        torch.cuda.synchronize(device)

        # Run Sharded
        sharded_optimizer.zero_grad()
        sharded_optimizer_2.zero_grad()
        sharded_loss = sharded_ddp_model(input_tensor).abs().sum()
        sharded_loss.backward()
        sharded_optimizer.step()
        sharded_optimizer_2.step()
        torch.cuda.synchronize(device)

        check_same_model_params(
            sharded_ddp_model, ddp_model, f"DDP parity two optim test failing, step {i}, buffers {reduce_buffer_size}",
        )

    dist.destroy_process_group()


@skip_if_no_cuda
@skip_if_single_gpu
@pytest.mark.parametrize("reduce_buffer_size", [0, 2 ** 20])
def test_ddp_parity_two_optim(reduce_buffer_size):
    world_size = 2
    backend = dist.Backend.NCCL
    mp.spawn(
        run_ddp_parity_two_optim,
        args=(world_size, backend, tempfile.mkstemp()[1], reduce_buffer_size),
        nprocs=world_size,
        join=True,
    )