"""Cloud-only qualification and fresh-process inference; no native/user-data tools."""
import argparse
import hashlib
import importlib.metadata as md
import json
import math
import platform
import subprocess
import time
from pathlib import Path
from parameter_contract import token_ids, check_frozen, check_adapter_records

ROOT = Path(__file__).resolve().parent
PINS = json.loads((ROOT / "pins.json").read_text())
TARGETS = {"q_proj", "k_proj", "v_proj", "o_proj", "q_a_proj", "q_b_proj", "kv_a_proj_with_mqa", "kv_b_proj", "dense"}


def save(name, value):
    out = ROOT / "artifacts"
    out.mkdir(exist_ok=True)
    (out / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def runtime():
    import torch
    from axolotl.model_support.bailing_hybrid.modeling_bailing_moe_v3 import BailingMoeV3ForCausalLM  # noqa: F401
    assert platform.system() == "Linux" and torch.cuda.is_available(), "Cloud CUDA only"
    direct = json.loads(md.distribution("axolotl").read_text("direct_url.json") or "{}")
    assert direct.get("vcs_info", {}).get("commit_id") == PINS["axolotl_revision"], "Install the exact pinned Axolotl Git revision; unbound image code is not admitted"
    assert torch.cuda.device_count() == 1, "Only one GPU admitted"
    info = torch.cuda.get_device_properties(0)
    assert "A6000" in info.name and info.total_memory >= 44 * 1024**3, info.name
    assert torch.cuda.is_bf16_supported(), "BF16 not supported"
    # A real CUDA allocation/operation is stronger evidence than architecture metadata.
    x = torch.ones((32, 32), device="cuda", dtype=torch.bfloat16)
    assert (x @ x).float().mean().item() == 32
    torch.cuda.synchronize()
    receipt = {"gpu": info.name, "capability": torch.cuda.get_device_capability(0),
               "memory_bytes": info.total_memory, "torch_cuda_arch_list": torch.cuda.get_arch_list(),
               "torch_cuda": torch.version.cuda,
               "axolotl_commit": direct["vcs_info"]["commit_id"],
               "packages": {name: md.version(name) for name in ("axolotl", "torch", "transformers", "peft", "fla-core", "flash-linear-attention", "triton")},
               "cuda_matmul_passed": True, "ling_backward_qualified": False}
    save("runtime.json", receipt)
    print(json.dumps(receipt, indent=2))


def tokenizer():
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(PINS["model_id"], revision=PINS["model_revision"], trust_remote_code=True)


def check_tokens():
    tok = tokenizer()
    rows = []
    for split in ("train", "dev"):
        for line in (ROOT / "data" / (split + ".jsonl")).read_text().splitlines():
            row = json.loads(line)
            full = tok.apply_chat_template(row["messages"], tokenize=True, return_dict=False, add_generation_prompt=False, enable_thinking=False)
            prefix = tok.apply_chat_template(row["messages"][:-1], tokenize=True, return_dict=False, add_generation_prompt=True, enable_thinking=False)
            token_ids(full); token_ids(prefix)
            assert len(full) <= 2048, f"No truncation allowed: {row['id']} has {len(full)} tokens"
            assert full[:len(prefix)] == prefix, f"Training/generation prefix mismatch: {row['id']}"
            assert len(full) > len(prefix), "Empty target"
            rows.append({"id": row["id"], "tokens": len(full), "target_tokens": len(full)-len(prefix),
                         "target_decoded": tok.decode(full[len(prefix):], skip_special_tokens=False)})
    template = tok.chat_template
    assert isinstance(template, str) and template
    assert hashlib.sha256(template.encode()).hexdigest() == PINS["chat_template_sha256"], "Pinned chat template mismatch"
    save("tokenization.json", {"rows": rows, "template_sha256": hashlib.sha256(template.encode()).hexdigest(),
                               "template": template, "thinking": False, "actual_axolotl_label_audit_required": True})
    print("All complete smoke examples fit; generation prefixes match. Inspect Axolotl label masking separately.")


def load_model(adapter=None):
    import torch
    from transformers.conversion_mapping import register_checkpoint_conversion_mapping
    from axolotl.model_support.bailing_hybrid import _weight_conversions
    from axolotl.model_support.bailing_hybrid.configuration_bailing_moe_v3 import BailingMoeV3Config
    from axolotl.model_support.bailing_hybrid.modeling_bailing_moe_v3 import BailingMoeV3ForCausalLM
    for key, conversions in _weight_conversions().items():
        register_checkpoint_conversion_mapping(key, list(conversions), overwrite=True)
    cfg = BailingMoeV3Config.from_pretrained(PINS["model_id"], revision=PINS["model_revision"])
    model, info = BailingMoeV3ForCausalLM.from_pretrained(
        PINS["model_id"], revision=PINS["model_revision"], config=cfg,
        dtype=torch.bfloat16, device_map={"": 0}, attn_implementation="sdpa", output_loading_info=True)
    assert not info.get("missing_keys") and not info.get("unexpected_keys") and not info.get("mismatched_keys"), str(info)
    if adapter:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, adapter, is_trainable=False, autocast_adapter_dtype=True)
    model.eval()
    return model


def evaluate(adapter=None):
    import torch
    tok = tokenizer()
    model = load_model(adapter)
    if adapter:
        from peft import get_peft_model_state_dict
        from safetensors.torch import load_file
        from training_guard import tensor_records
        reference = json.loads((ROOT / "artifacts/posttrain-reference.json").read_text())
        check_adapter_records(reference["adapter_tensors"], tensor_records(get_peft_model_state_dict(model)))
        saved = load_file(Path(adapter) / "adapter_model.safetensors", device="cpu")
        check_adapter_records(reference["adapter_tensors"], tensor_records(saved))
        inp = torch.tensor([token_ids(reference["input_ids"])], device="cuda")
        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
            actual_logits = model(input_ids=inp, attention_mask=torch.ones_like(inp), use_cache=False).logits[:, -1, :].float().cpu()
        expected_logits = load_file(ROOT / "artifacts/posttrain-reference.safetensors", device="cpu")["last_token_logits"]
        torch.testing.assert_close(actual_logits, expected_logits, rtol=0.001, atol=0.02, equal_nan=False)
        save("reload-equivalence.json", {"adapter_keys_values_dtypes_exact": True,
            "posttrain_forward_reproduced": True, "reference_global_step": reference["global_step"],
            "max_absolute_logit_error": (actual_logits-expected_logits).abs().max().item(),
            "rtol": 0.001, "atol": 0.02, "fresh_process": True,
            "adapter_sha256": hashlib.sha256((Path(adapter)/"adapter_model.safetensors").read_bytes()).hexdigest()})
    result = []
    for line in (ROOT / "data/dev.jsonl").read_text().splitlines():
        row = json.loads(line)
        ids = tok.apply_chat_template(row["messages"][:-1], tokenize=True, return_dict=False, add_generation_prompt=True, enable_thinking=False)
        token_ids(ids)
        inp = torch.tensor([ids], device="cuda")
        torch.cuda.synchronize()
        start = time.monotonic()
        with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
            output = model.generate(input_ids=inp, attention_mask=torch.ones_like(inp), do_sample=False,
                                    max_new_tokens=900, pad_token_id=tok.pad_token_id or tok.eos_token_id,
                                    eos_token_id=tok.eos_token_id)
        torch.cuda.synchronize()
        generated = output[0, len(ids):].tolist()
        raw = tok.decode(generated, skip_special_tokens=True)
        actual = raw.strip()
        if row["category"] == "routing":
            try:
                actual = json.loads(actual)
            except ValueError:
                actual = None
        result.append({"id": row["id"], "category": row["category"], "raw": raw,
                       "expected": row["expected"], "exact_match": actual == row["expected"],
                       "ended_with_eos": bool(generated and generated[-1] == tok.eos_token_id),
                       "seconds": time.monotonic()-start, "new_tokens": len(generated)})
    name = "reloaded" if adapter else "base"
    receipt = {"stage": name, "pins": PINS, "adapter": str(adapter) if adapter else None,
               "adapter_dtype_policy": "PEFT autocast_adapter_dtype=True; verify actual float32 adapter tensors against training inventory",
               "rows": result, "exact": sum(r["exact_match"] for r in result), "total": len(result),
               "max_allocated_bytes": torch.cuda.max_memory_allocated(),
               "quality_uplift_claim": False, "serving_export_qualified": False}
    save(name + "-dev.json", receipt)
    print(json.dumps({k: v for k, v in receipt.items() if k != "rows"}, indent=2))
    if adapter:
        assert receipt["exact"] > 0, "Reload smoke must score non-zero; do not scale"


def inspect_adapter():
    import torch
    from safetensors.torch import load_file
    folder = ROOT / "artifacts/smoke-adapter"
    path = folder / "adapter_model.safetensors"
    cfg = json.loads((folder / "adapter_config.json").read_text())
    assert set(cfg["target_modules"]) == TARGETS
    assert not cfg.get("target_parameters") and not cfg.get("modules_to_save")
    assert cfg["r"] == 16 and cfg["lora_alpha"] == 32
    tensors = load_file(path, device="cpu")
    assert tensors
    nonzero_b = 0
    for key, tensor in tensors.items():
        assert ".lora_A." in key or ".lora_B." in key, key
        assert any("." + target + "." in key for target in TARGETS), key
        assert torch.isfinite(tensor).all(), key
        if ".lora_B." in key and torch.count_nonzero(tensor).item():
            nonzero_b += 1
    assert nonzero_b > 0, "Default-zero LoRA B must change during training"
    states = list(folder.glob("checkpoint-*/trainer_state.json"))
    assert states, "Missing persisted trainer evidence"
    latest = max(states, key=lambda p: int(p.parent.name.split("-")[-1]))
    state = json.loads(latest.read_text())
    before = json.loads((ROOT / "artifacts/frozen-before.json").read_text())
    after = json.loads((ROOT / "artifacts/frozen-after.json").read_text())
    check_frozen(before["frozen"], after["frozen"])
    assert after["unchanged"] is True and after["global_step"] == state["global_step"]
    from training_guard import tensor_records
    reference = json.loads((ROOT / "artifacts/posttrain-reference.json").read_text())
    assert reference["global_step"] == state["global_step"]
    check_adapter_records(reference["adapter_tensors"], tensor_records(tensors))
    losses = [h["loss"] for h in state["log_history"] if "loss" in h]
    norms = [h["grad_norm"] for h in state["log_history"] if "grad_norm" in h]
    assert losses and all(math.isfinite(x) for x in losses)
    assert norms and all(math.isfinite(x) for x in norms) and any(x > 0 for x in norms)
    save("adapter-verification.json", {"adapter_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
         "tensor_count": len(tensors), "nonzero_lora_B_tensors": nonzero_b, "losses": losses,
         "gradient_norms": norms, "global_step": state["global_step"],
         "reload_qualified": False, "intended_mac_serving_qualified": False})
    print("Finite adapter update and gradient evidence verified; fresh reload is still required.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["runtime", "tokens", "base", "inspect-adapter", "reload"])
    a = p.parse_args()
    if a.action == "runtime": runtime()
    elif a.action == "tokens": check_tokens()
    elif a.action == "base": evaluate()
    elif a.action == "inspect-adapter": inspect_adapter()
    else: evaluate(ROOT / "artifacts/smoke-adapter")
