from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class GPUDevice:
    index: int
    name: str
    memory_total_mb: int
    memory_used_mb: int
    memory_free_mb: int
    utilization_percent: float
    temperature_c: float
    power_watts: float
    compute_capability: str = ""
    processes: list[dict[str, Any]] = field(default_factory=list)


class GPUManager:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._devices: list[GPUDevice] = []
        self._nvidia_available = self._check_nvidia()
        self._next_llm_gpu = 0
        self._next_embed_gpu = 0

        if self._nvidia_available:
            self._refresh_devices()
            logger.info(
                "GPUManager: detected %d NVIDIA GPU(s)",
                len(self._devices),
            )
            for d in self._devices:
                logger.info(
                    "  GPU %d: %s | %d MB free / %d MB total",
                    d.index, d.name, d.memory_free_mb, d.memory_total_mb,
                )
        else:
            logger.warning("GPUManager: no NVIDIA GPUs detected via nvidia-smi")

        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    @staticmethod
    def _check_nvidia() -> bool:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def _refresh_devices(self) -> None:
        if not self._nvidia_available:
            return
        try:
            r = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw,compute_cap",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return
            devices = []
            for line in r.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = [p.strip() for p in line.split(", ")]
                if len(parts) < 8:
                    continue
                try:
                    def _mb(v: str) -> int:
                        v = v.replace(" MiB", "").replace(" MiB", "").strip()
                        if v in ("N/A", "[N/A]", ""):
                            return 0
                        return int(float(v))

                    device = GPUDevice(
                        index=int(parts[0]),
                        name=parts[1],
                        memory_total_mb=_mb(parts[2]),
                        memory_used_mb=_mb(parts[3]),
                        memory_free_mb=_mb(parts[4]),
                        utilization_percent=float(parts[5]) if parts[5] not in ("N/A", "[N/A]", "") else 0.0,
                        temperature_c=float(parts[6]) if parts[6] not in ("N/A", "[N/A]", "") else 0.0,
                        power_watts=float(parts[7]) if parts[7] not in ("N/A", "[N/A]", "") else 0.0,
                        compute_capability=parts[8] if len(parts) > 8 else "",
                    )
                    devices.append(device)
                except (ValueError, IndexError) as e:
                    logger.debug("Failed to parse nvidia-smi line: %s — %s", line, e)
            with self._lock:
                self._devices = devices
        except Exception as e:
            logger.warning("nvidia-smi refresh failed: %s", e)

    def _monitor_loop(self) -> None:
        while True:
            time.sleep(5)
            if self._nvidia_available:
                self._refresh_devices()

    def get_devices(self) -> list[GPUDevice]:
        with self._lock:
            return list(self._devices)

    def get_device(self, index: int) -> GPUDevice | None:
        with self._lock:
            for d in self._devices:
                if d.index == index:
                    return d
            return None

    def get_status(self) -> dict[str, Any]:
        devices = self.get_devices()
        return {
            "available": self._nvidia_available,
            "count": len(devices),
            "devices": [
                {
                    "index": d.index,
                    "name": d.name,
                    "memory_total_mb": d.memory_total_mb,
                    "memory_used_mb": d.memory_used_mb,
                    "memory_free_mb": d.memory_free_mb,
                    "utilization_percent": d.utilization_percent,
                    "temperature_c": d.temperature_c,
                    "power_watts": d.power_watts,
                    "compute_capability": d.compute_capability,
                }
                for d in devices
            ],
            "platform": platform.platform(),
            "processor": platform.processor(),
            "python": platform.python_version(),
        }

    def get_next_llm_gpu(self) -> int:
        with self._lock:
            gpu = self._next_llm_gpu
            count = max(len(self._devices), 1)
            self._next_llm_gpu = (self._next_llm_gpu + 1) % count
            return gpu

    def get_next_embed_gpu(self) -> int:
        with self._lock:
            gpu = self._next_embed_gpu
            count = max(len(self._devices), 1)
            self._next_embed_gpu = (self._next_embed_gpu + 1) % count
            return gpu

    def device_count(self) -> int:
        return len(self._devices)

    def set_cuda_device(self, device_id: int) -> None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(device_id)
        os.environ["HIP_VISIBLE_DEVICES"] = str(device_id)

    def get_ollama_url(self, gpu_id: int) -> str:
        instance = self.config.gpu_ollama_instance(gpu_id)
        if instance and "base_url" in instance:
            return instance["base_url"]
        # No per-GPU instances configured: route back to the app's configured
        # Ollama endpoint (e.g. the workbench relay host.docker.internal:11435),
        # not a bare localhost port that only exists if we launched our own
        # Ollama here.
        base = self.config.ollama_base_url.rstrip("/")
        if base:
            return base
        port = 11434 + gpu_id
        return f"http://localhost:{port}"

    def launch_ollama_instances(self) -> None:
        if not self._nvidia_available:
            return
        count = self.device_count()
        if count < 1:
            return
        logger.info("Launching %d Ollama instance(s) — one per GPU", count)
        for gpu_id in range(count):
            port = 11434 + gpu_id
            url = f"http://localhost:{port}"
            try:
                r = subprocess.run(
                    ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", f"{url}/api/tags"],
                    capture_output=True, text=True, timeout=3,
                )
                if r.stdout.strip() == "200":
                    logger.info("  Ollama already running on GPU %d at %s", gpu_id, url)
                    continue
            except Exception:
                pass
            env = os.environ.copy()
            env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
            env["OLLAMA_HOST"] = f"127.0.0.1:{port}"
            env["OLLAMA_KEEP_ALIVE"] = "24h"
            env["OLLAMA_NUM_PARALLEL"] = "4"
            env["OLLAMA_MAX_LOADED_MODELS"] = "2"
            try:
                proc = subprocess.Popen(
                    ["ollama", "serve"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                logger.info("  Launched Ollama on GPU %d (PID %d, port %d)", gpu_id, proc.pid, port)
            except FileNotFoundError:
                logger.warning("  'ollama' binary not found; please start Ollama manually for GPU %d", gpu_id)
                break
        time.sleep(3)
