"""Explicit user-run backends. Model and transport imports are lazy."""
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def endpoint_url(value):
    u = urlsplit(value)
    if u.scheme not in ("http", "https") or u.hostname not in ("127.0.0.1", "::1") or u.username or u.password or u.query or u.fragment:
        raise ValueError("endpoint must be an explicit numeric loopback URL without credentials/query/fragment")
    if u.path.rstrip("/") not in ("/v1", "/v1/chat/completions"):
        raise ValueError("endpoint path must be /v1 or /v1/chat/completions")
    _ = u.port  # reject malformed ports before execution
    return value.rstrip("/") if u.path.rstrip("/").endswith("chat/completions") else value.rstrip("/") + "/chat/completions"


def describe(config):
    backend = config.get("backend")
    allowed = {
        "mock": {"backend"},
        "hf_probe": {"backend", "probe_dir", "adapter"},
        "http": {"backend", "endpoint", "model", "identity", "token_env"},
    }
    if backend not in allowed or set(config) - allowed[backend]:
        raise ValueError("unknown backend or configuration field")
    if backend == "mock":
        return {"backend": backend, "simulation": True, "identity": "oracle for harness self-test only"}
    if backend == "http":
        if not isinstance(config.get("model"), str) or not config["model"]:
            raise ValueError("explicit endpoint model required")
        identity = config.get("identity", {})
        if not isinstance(identity, dict) or set(identity) - {"revision", "checkpoint_sha256", "quantization", "tokenizer_revision", "chat_template_sha256"}:
            raise ValueError("unsupported identity fields")
        if any(not isinstance(v, str) or not v for v in identity.values()):
            raise ValueError("identity values must be nonempty strings, including 'unknown' where unavailable")
        env = config.get("token_env")
        if env is not None and (not isinstance(env, str) or not env.isidentifier()):
            raise ValueError("token_env must name an environment variable; never put a token in configuration")
        return {"backend": backend, "endpoint": endpoint_url(config["endpoint"]), "model": config["model"],
                "identity": identity, "binding": "user-declared; endpoint weights/template not verified",
                "token_env": env, "simulation": False}
    folder = Path(config["probe_dir"]).expanduser().resolve(strict=True)
    probe = folder / "cloud_probe.py"
    if not folder.is_dir() or probe.is_symlink() or not probe.is_file():
        raise ValueError("probe requires a regular canonical cloud_probe.py")
    pins_file = folder / "pins.json"
    pins = json.loads(pins_file.read_text())
    required = ("model_id", "model_revision", "tokenizer_revision", "chat_template_sha256", "axolotl_revision")
    if any(not isinstance(pins.get(k), str) or not pins[k] for k in required):
        raise ValueError("probe pins missing required model/tokenizer/template/revision")
    if pins["tokenizer_revision"] != pins["model_revision"]:
        raise ValueError("probe tokenizer uses model revision; tokenizer pin must match")
    adapter = config.get("adapter")
    inventory = {}
    adapter_metadata = None
    if adapter:
        af = Path(adapter).expanduser().resolve(strict=True)
        if not (af / "adapter_config.json").is_file() or not any((af / n).is_file() for n in ("adapter_model.safetensors", "adapter_model.bin")):
            raise ValueError("adapter must contain saved PEFT configuration and weights")
        ac = json.loads((af / "adapter_config.json").read_text())
        if ac.get("base_model_name_or_path") != pins["model_id"]:
            raise ValueError("adapter base model differs from pinned probe base")
        adapter_metadata = {k: ac.get(k) for k in ("base_model_name_or_path", "revision", "peft_type", "r", "lora_alpha", "target_modules")}
        inventory = {p.name: sha(p) for p in af.iterdir() if p.is_file() and
                     (p.name.startswith(("adapter_", "tokenizer", "special_tokens", "chat_template")) or p.name == "config.json")}
        adapter = str(af)
    probe_files = {p.name: sha(p) for p in folder.glob("*.py")}
    return {"backend": backend, "probe_dir": str(folder), "probe_files": probe_files,
            "probe_entrypoint": {"path": str(probe), "sha256": probe_files[probe.name]},
            "pins_sha256": sha(pins_file), "identity": {k: pins[k] for k in required}, "adapter": adapter,
            "adapter_files": inventory, "adapter_metadata": adapter_metadata, "quantization": "unquantized bf16 base; actual tensor dtypes recorded at load",
            "binding": "pinned probe loader; template and local adapter hashes checked", "simulation": False}


def load_probe_source(descriptor):
    """Execute the selected, verified source bytes, never a cached/name-based import.

    The probe remains explicitly supplied executable code, not sandboxed code.
    Dependencies imported by that code are outside this entry-point binding.
    """
    import importlib.util
    folder = Path(descriptor["probe_dir"]).resolve(strict=True)
    path = folder / "cloud_probe.py"
    expected = descriptor.get("probe_entrypoint", {})
    if (not folder.is_dir() or path.is_symlink() or not path.is_file()
            or expected.get("path") != str(path)
            or expected.get("sha256") != descriptor["probe_files"].get(path.name)):
        raise ValueError("probe entry-point origin does not match descriptor")
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest != expected.get("sha256"):
        raise ValueError("probe entry-point source changed after description")
    name = "_wisp_hf_probe_" + digest
    spec = importlib.util.spec_from_file_location(name, path)
    probe = importlib.util.module_from_spec(spec)
    # Compile the verified snapshot directly; a loader could otherwise use .pyc
    # or reopen a changed source. Neither sys.path nor sys.modules is consulted.
    exec(compile(source, str(path), "exec"), probe.__dict__)
    if probe.__file__ != str(path) or probe.__spec__ is not spec or spec.origin != str(path):
        raise ValueError("loaded probe origin differs from selected source")
    return probe, {"path": str(path), "sha256": digest, "source_bytes": len(source)}


class MockBackend:
    def __init__(self, descriptor):
        self.runtime = descriptor

    def generate(self, request):
        # Only the explicit simulation arm ever receives gold. No quality claim is permitted.
        return {"text": json.dumps(request.get("mock_expected", {})) if request["lane"] == "routing" else
                "SIMULATED RESPONSE: manual quality review required.", "finish_reason": "stop", "latency_s": 0.0,
                "output_tokens": None, "peak_allocated_bytes": None}


class HTTPBackend:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.runtime = descriptor

    def generate(self, request):
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        headers = {"Content-Type": "application/json"}
        env = self.descriptor["token_env"]
        if env:
            token = os.environ.get(env)
            if not token:
                raise ValueError("configured token environment variable missing")
            headers["Authorization"] = "Bearer " + token
        body = {"model": self.descriptor["model"], "messages": request["messages"],
                "temperature": 0.0, "max_tokens": request["max_tokens"], "stream": False,
                "chat_template_kwargs": {"enable_thinking": False}}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        started = time.monotonic()
        req = urllib.request.Request(self.descriptor["endpoint"], json.dumps(body).encode(), headers, method="POST")
        with opener.open(req, timeout=request["timeout_s"]) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("endpoint response exceeds limit")
        data = json.loads(raw)
        if data.get("model") != self.descriptor["model"]:
            raise ValueError("endpoint response model identity differs from requested model")
        choices = data.get("choices", [])
        if len(choices) != 1 or not isinstance(choices[0].get("message", {}).get("content"), str) or choices[0]["message"].get("tool_calls"):
            raise ValueError("endpoint must return one text completion with no tool calls")
        return {"text": choices[0]["message"]["content"], "finish_reason": choices[0].get("finish_reason"),
                "latency_s": time.monotonic() - started, "output_tokens": data.get("usage", {}).get("completion_tokens"),
                "peak_allocated_bytes": None, "endpoint_usage": data.get("usage", {})}


class HFProbeBackend:
    def __init__(self, descriptor):
        import importlib.metadata
        probe, actual_entrypoint = load_probe_source(descriptor)
        import torch
        self.torch = torch
        self.tok = probe.tokenizer()
        template = self.tok.chat_template
        if not isinstance(template, str) or hashlib.sha256(template.encode()).hexdigest() != descriptor["identity"]["chat_template_sha256"]:
            raise ValueError("actual tokenizer template does not match pins")
        # Probe is user-provided executable code; the README requires inspecting it before use.
        self.model = probe.load_model(descriptor["adapter"])
        from collections import Counter
        dtypes = Counter(str(p.dtype) for p in self.model.parameters())
        self.runtime = {**descriptor, "actual_probe_entrypoint": actual_entrypoint,
                        "actual_tensor_dtype_counts": dict(dtypes), "gpu": torch.cuda.get_device_name(0),
                        "versions": {n: importlib.metadata.version(n) for n in ("torch", "transformers", "peft", "axolotl")},
                        "hf_base_cache_weights_sha256": "not independently hashed; loader resolves pinned revision",
                        "no_thinking": True}

    def generate(self, request):
        torch = self.torch
        inputs = self.tok.apply_chat_template(request["messages"], tokenize=True, return_dict=True,
                  return_tensors="pt", add_generation_prompt=True, enable_thinking=False)
        inputs = {k: v.to(self.model.device) for k, v in inputs.items()}
        limit = getattr(self.model.config, "max_position_embeddings", None)
        if limit and inputs["input_ids"].shape[1] + request["max_tokens"] > limit:
            raise ValueError("prompt plus generation budget exceeds model context")
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
        started = time.monotonic()
        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
            output = self.model.generate(**inputs, do_sample=False, max_new_tokens=request["max_tokens"],
                                         pad_token_id=self.tok.pad_token_id, eos_token_id=self.tok.eos_token_id)
        torch.cuda.synchronize()
        elapsed = time.monotonic() - started
        generated = output[0, inputs["input_ids"].shape[1]:]
        length = len(generated)
        eos = self.tok.eos_token_id
        eos_ids = eos if isinstance(eos, list) else [eos]
        ended = length > 0 and generated[-1].item() in eos_ids
        return {"text": self.tok.decode(generated, skip_special_tokens=True),
                "finish_reason": "stop" if ended else "length", "latency_s": elapsed,
                "input_tokens": inputs["input_ids"].shape[1], "output_tokens": length,
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(),
                "peak_reserved_bytes": torch.cuda.max_memory_reserved()}


def make_backend(descriptor):
    return {"mock": MockBackend, "http": HTTPBackend, "hf_probe": HFProbeBackend}[descriptor["backend"]](descriptor)
