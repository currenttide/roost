"""Bare-worker (kind: auto) pre-filter tests — the cheap deterministic gate that
declines an obvious capability mismatch without spending an LLM triage call."""
from __future__ import annotations

import pytest

from roost.worker import _auto_prefilter

NO_GPU = {"cpus": 4, "tools": ["claude"]}
GPU = {"cpus": 32, "gpu_count": 1, "gpu_vram_gb": 24, "tools": ["claude"]}
DOCKER_GPU = {"cpus": 8, "docker_gpu": True, "tools": ["claude"]}


@pytest.mark.parametrize("task", [
    "Run a CUDA matmul benchmark and report GFLOP/s.",
    "Report the GPU model and total VRAM via nvidia-smi.",
    "This task requires a GPU with >=16GB VRAM.",
    "Train a model on the GPU for 50 steps.",
    "Check torch.cuda.is_available() and report.",
])
def test_prefilter_declines_gpu_task_on_cpu_node(task):
    assert _auto_prefilter(task, NO_GPU) is not None


@pytest.mark.parametrize("task", [
    "Run a CUDA matmul benchmark and report GFLOP/s.",
    "Report the GPU model and total VRAM via nvidia-smi.",
])
def test_prefilter_passes_gpu_task_on_gpu_node(task):
    assert _auto_prefilter(task, GPU) is None
    assert _auto_prefilter(task, DOCKER_GPU) is None


@pytest.mark.parametrize("task", [
    "Print the hostname and number of CPU cores.",
    "Count how many prime numbers are below 10000.",
    "Reverse the string orchestrator and print it.",
    "In one sentence, explain what a GPU is.",   # mentions gpu but doesn't require one
])
def test_prefilter_passes_cpu_task_on_cpu_node(task):
    assert _auto_prefilter(task, NO_GPU) is None
