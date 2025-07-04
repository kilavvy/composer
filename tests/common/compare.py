# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

import datetime
from typing import Any, Optional, Union

import numpy as np
import torch
import torchmetrics
from torch.distributed._shard.sharded_tensor import ShardedTensor
from torch.distributed._tensor import DTensor

from composer import Time
from composer.core.time import TimeUnit


def deep_compare(item1: Any, item2: Any, atol: float = 0.0, rtol: float = 0.0, ignore_keys: Optional[list[str]] = None):
    """Compare two items recursively. Supports dicts, lists, tuples, tensors, numpy arrays, Composer Time objects, and callables.

    Args:
        item1 (Any): The first item
        item2 (Any): The second item
        atol (bool): Atol tolerance for torch tensors and numpy arrays (default: 0.0)
        rtol (float): Rtol tolerance for torch tensors and numpy arrays (default: 0.0)
    """
    return _check_item(item1, item2, path='', atol=atol, rtol=rtol, ignore_keys=ignore_keys)


def _check_item(
    item1: Any,
    item2: Any,
    path: str,
    rtol: float = 0.0,
    atol: float = 0.0,
    ignore_keys: Optional[list[str]] = None,
):
    if item1 is None:
        assert item2 is None, f'{path} differs: {item1} != {item2}'
        return
    if isinstance(item1, (str, float, int, bool, Time, datetime.timedelta, TimeUnit)):
        assert type(item1) == type(item2)
        assert item1 == item2, f'{path} differs: {item1} != {item2}'
        return
    if isinstance(item1, torch.Tensor) and not (isinstance(item1, ShardedTensor) or isinstance(item1, DTensor)):
        assert isinstance(item2, torch.Tensor)
        if item1.device != item2.device:
            item1 = item1.cpu()
            item2 = item2.cpu()
        assert item1.allclose(item2, rtol=rtol, atol=atol), f'{path} differs'
        return
    if isinstance(item1, np.ndarray):
        assert isinstance(item2, np.ndarray)
        assert np.allclose(item1, item2, rtol=0.1, atol=0.1), f'{path} differs'
        return
    if isinstance(item1, dict):
        assert isinstance(item2, dict), f'{path} differs: {item1} != {item2}'
        _check_dict_recursively(item1, item2, path, atol=atol, rtol=rtol, ignore_keys=ignore_keys)
        return
    if isinstance(item1, (tuple, list)):
        # When we are broadcasting lists/tuples from rank0 (e.g. State.load_optim_state)
        # tuples get converted to lists and so we don't want to validate the type, just
        # the values
        _check_list_recursively(item1, item2, path, atol=atol, rtol=rtol)
        return
    if isinstance(item1, ShardedTensor):
        assert isinstance(item2, type(item1)), f'{path} differs: {item1} != {item2}'
        _check_sharded_tensor_recursively(item1, item2, path, atol=atol, rtol=rtol)
        return

    if isinstance(item1, DTensor):
        assert isinstance(item2, type(item1)), f'{path} differs: {item1} != {item2}'
        _check_dtensor_recursively(item1, item2, path, atol=atol, rtol=rtol)
        return

    if isinstance(item1, torchmetrics.Metric):
        assert isinstance(item2, torchmetrics.Metric), f'{path} differs: {item1} != {item2}'
        # Increase update count so Torchmetrics doesn't throw warning when computing two metrics which haven't been updated
        item1._update_count += 1
        item2._update_count += 1
        item1_compute = item1.compute()
        item2_compute = item2.compute()
        if isinstance(item1_compute, torch.Tensor) and isinstance(item2_compute, torch.Tensor):
            assert item1_compute.allclose(
                item2_compute,
                atol=atol,
                rtol=rtol,
                equal_nan=True,
            ), f'{path} differs: {item1_compute} != {item2_compute}'
        elif isinstance(item1_compute, dict):
            assert isinstance(item2_compute, dict)
            _check_dict_recursively(item1_compute, item2_compute, path, atol, rtol)
        else:
            assert 'Torchmetric compute() returned unexpected type, please add support in `_check_item`'
        item1._update_count -= 1
        item2._update_count -= 1
        return

    raise NotImplementedError(f'Unsupported item type: {type(item1)}')


def _check_dtensor_recursively(
    dtensor1: DTensor,
    dtensor2: DTensor,
    path: str,
    atol: float,
    rtol: float,
):
    tensor1, tensor2 = dtensor1.to_local(), dtensor2.to_local()
    _check_item(tensor1, tensor2, path, atol=atol, rtol=rtol)


def _check_sharded_tensor_recursively(
    sharded_tensor1: ShardedTensor,
    sharded_tensor2: ShardedTensor,
    path: str,
    atol: float,
    rtol: float,
):
    tensor1, tensor2 = sharded_tensor1.local_tensor(), sharded_tensor2.local_tensor()
    _check_item(tensor1, tensor2, path, atol=atol, rtol=rtol)


def _check_list_recursively(
    list1: Union[tuple[Any], list[Any]],
    list2: Union[tuple[Any], list[Any]],
    path: str,
    atol: float,
    rtol: float,
):
    assert len(list1) == len(list2), f'{path} differs: {list1} != {list2}'
    for i, (item1, item2) in enumerate(zip(list1, list2)):
        _check_item(item1, item2, path=f'{path}/{i}', atol=atol, rtol=rtol)


def _check_dict_recursively(
    dict1: dict[str, Any],
    dict2: dict[str, Any],
    path: str,
    atol: float,
    rtol: float,
    ignore_keys: Optional[list[str]] = None,
):
    # Starting in PyTorch 2.7, verbose is no longer a valid key in any LRScheduler
    # https://github.com/pytorch/pytorch/pull/147301
    if 'schedulers' in path.lower() and 'lr' in path.lower():
        dict1.pop('verbose', None)
        dict2.pop('verbose', None)
    assert len(dict1) == len(dict2), f'{path} differs: {dict1} != {dict2}'
    for k, val1 in dict1.items():
        if ignore_keys is not None and k in ignore_keys:
            continue
        val2 = dict2[k]

        # special case fused optimizer to allow comparing a GPU checkpoint with a CPU checkpoint
        if isinstance(k, str) and k == 'fused' and path == '/state/optimizers/Adam/param_groups/0':
            assert bool(val1) == bool(val2)
            continue
        _check_item(val1, val2, path=f'{path}/{k}', atol=atol, rtol=rtol)
