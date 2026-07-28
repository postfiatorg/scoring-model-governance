"""Templated Modal app for one exam candidate, adapted from the sidecar's app.

This module is a ``modal deploy`` target, never imported by the service:
the runtime manager parameterizes it entirely through ``GOVEXAM_*``
environment variables (the sidecar's ``SIDECAR_MODAL_*`` pattern), and the
deploy config is baked into the image environment so Modal's in-container
re-import of this module reads back exactly what was deployed.

The app serves two surfaces: the GPU-backed SGLang web server (the exam
inference endpoint), and a CPU control endpoint ``profile`` that reports
the deployed profile's content hash — the runtime manager's reuse check
asks the live app what it serves instead of trusting local state.
"""

import json
import os
import subprocess
import time
import urllib.request

import modal

_CONFIG_KEYS = (
    "GOVEXAM_APP_NAME",
    "GOVEXAM_IMAGE",
    "GOVEXAM_GPU",
    "GOVEXAM_MODEL_REPO_ID",
    "GOVEXAM_MODEL_REVISION",
    "GOVEXAM_TENSOR_PARALLELISM",
    "GOVEXAM_LAUNCH_ARGS",
    "GOVEXAM_ENVIRONMENT",
    "GOVEXAM_PROFILE_HASH",
    "GOVEXAM_SCALEDOWN_MINUTES",
)
_DEPLOY_CONFIG = {key: os.environ[key] for key in _CONFIG_KEYS}

APP_NAME = _DEPLOY_CONFIG["GOVEXAM_APP_NAME"]
IMAGE_REF = _DEPLOY_CONFIG["GOVEXAM_IMAGE"]
GPU_TYPE = _DEPLOY_CONFIG["GOVEXAM_GPU"]
MODEL_REPO_ID = _DEPLOY_CONFIG["GOVEXAM_MODEL_REPO_ID"]
MODEL_REVISION = _DEPLOY_CONFIG["GOVEXAM_MODEL_REVISION"]
TENSOR_PARALLELISM = int(_DEPLOY_CONFIG["GOVEXAM_TENSOR_PARALLELISM"])
LAUNCH_ARGS = json.loads(_DEPLOY_CONFIG["GOVEXAM_LAUNCH_ARGS"])
MANIFEST_ENVIRONMENT = json.loads(_DEPLOY_CONFIG["GOVEXAM_ENVIRONMENT"])
PROFILE_HASH = _DEPLOY_CONFIG["GOVEXAM_PROFILE_HASH"]
SCALEDOWN_MINUTES = int(_DEPLOY_CONFIG["GOVEXAM_SCALEDOWN_MINUTES"])

SGLANG_PORT = 8000
MINUTES = 60
HF_CACHE_PATH = "/model-cache/huggingface"
STARTUP_TIMEOUT = 35 * MINUTES

RUNTIME_ENV = {
    **MANIFEST_ENVIRONMENT,
    "HF_HOME": HF_CACHE_PATH,
    "HF_HUB_CACHE": HF_CACHE_PATH,
}

# HF_TOKEN rides a runtime Secret only, never the baked image environment.
HF_TOKEN = os.environ.get("HF_TOKEN", "")
CONTAINER_SECRETS = [modal.Secret.from_dict({"HF_TOKEN": HF_TOKEN})] if HF_TOKEN else []

app = modal.App(name=APP_NAME)

sglang_image = (
    modal.Image.from_registry(IMAGE_REF)
    .entrypoint([])
    .pip_install("huggingface_hub", "hf_xet")
    .env({**RUNTIME_ENV, **_DEPLOY_CONFIG})
)
model_volume = modal.Volume.from_name(f"{APP_NAME}-model-weights", create_if_missing=True)


def _wait_for_server(timeout: int = 30 * MINUTES) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{SGLANG_PORT}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if 200 <= response.status < 300:
                    return
        except OSError:
            pass
        time.sleep(5)
    raise TimeoutError(f"SGLang server not ready within {timeout}s")


@app.cls(
    image=sglang_image,
    gpu=GPU_TYPE,
    volumes={HF_CACHE_PATH: model_volume},
    secrets=CONTAINER_SECRETS,
    timeout=60 * MINUTES,
    scaledown_window=SCALEDOWN_MINUTES * MINUTES,
    max_containers=1,
)
class ExamCandidateEndpoint:
    @modal.enter()
    def start_server(self):
        from huggingface_hub import snapshot_download

        model_path = snapshot_download(repo_id=MODEL_REPO_ID, revision=MODEL_REVISION)
        command = [
            "python",
            "-m",
            "sglang.launch_server",
            "--model-path",
            model_path,
            "--served-model-name",
            MODEL_REPO_ID,
            "--host",
            "0.0.0.0",
            "--port",
            str(SGLANG_PORT),
            "--tp",
            str(TENSOR_PARALLELISM),
            *LAUNCH_ARGS,
        ]
        self.process = subprocess.Popen(command)
        _wait_for_server()

    @modal.web_server(port=SGLANG_PORT, startup_timeout=STARTUP_TIMEOUT, requires_proxy_auth=True)
    def serve(self):
        pass

    @modal.exit()
    def stop(self):
        self.process.terminate()


control_image = modal.Image.debian_slim().pip_install("fastapi[standard]").env(_DEPLOY_CONFIG)


@app.function(image=control_image, timeout=60)
@modal.fastapi_endpoint(method="GET", requires_proxy_auth=True)
def profile() -> dict:
    """What this app serves — the reuse check's source of truth."""
    return {
        "profile_hash": PROFILE_HASH,
        "hf_repo": MODEL_REPO_ID,
        "revision": MODEL_REVISION,
        "gpu": GPU_TYPE,
        "image": IMAGE_REF,
    }
