"""Cloud-only Axolotl plugin: prove attention-adapter-only updates."""
import hashlib
import json
from pathlib import Path
import torch
from transformers import TrainerCallback
from axolotl.integrations.base import BasePlugin
from parameter_contract import check_inventory, check_frozen, is_adapter

ROOT = Path(__file__).resolve().parent


def tensor_hash(tensor):
    # Bound host scratch memory even for the embedding matrix; no weight copies saved.
    raw = tensor.detach().contiguous().view(torch.uint8).reshape(-1)
    digest = hashlib.sha256()
    for chunk in raw.split(8 * 1024 * 1024):
        digest.update(chunk.cpu().numpy().tobytes())
    return digest.hexdigest()


def snapshot(model):
    rows = [{"name": name, "requires_grad": p.requires_grad, "shape": list(p.shape), "dtype": str(p.dtype), "numel": p.numel()} for name, p in model.named_parameters()]
    check_inventory(rows)
    frozen = {name: {"shape": list(t.shape), "dtype": str(t.dtype), "sha256": tensor_hash(t)} for name, t in model.state_dict().items() if not is_adapter(name)}
    return {"inventory": rows, "frozen": frozen}


def write(name, value):
    (ROOT / "artifacts").mkdir(exist_ok=True)
    (ROOT / "artifacts" / name).write_text(json.dumps(value, indent=2) + "\n")


def tensor_records(tensors):
    return {name: {"shape": list(t.shape), "dtype": str(t.dtype), "sha256": tensor_hash(t)} for name, t in tensors.items()}


def posttraining_reference(model, step):
    from peft import get_peft_model_state_dict
    from transformers import AutoTokenizer
    from safetensors.torch import save_file
    pins = json.loads((ROOT / "pins.json").read_text())
    tok = AutoTokenizer.from_pretrained(pins["model_id"], revision=pins["model_revision"], trust_remote_code=True)
    row = json.loads((ROOT / "data/dev.jsonl").read_text().splitlines()[0])
    ids = tok.apply_chat_template(row["messages"][:-1], tokenize=True, return_dict=False, add_generation_prompt=True, enable_thinking=False)
    from parameter_contract import token_ids
    token_ids(ids)
    inp = torch.tensor([ids], device="cuda")
    was_training = model.training
    model.eval()
    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        logits = model(input_ids=inp, attention_mask=torch.ones_like(inp), use_cache=False).logits[:, -1, :].float().cpu().contiguous()
    if not torch.isfinite(logits).all(): raise ValueError("Nonfinite posttraining reference logits")
    save_file({"last_token_logits": logits}, ROOT / "artifacts/posttrain-reference.safetensors")
    write("posttrain-reference.json", {"global_step": step, "probe_id": row["id"], "input_ids": ids,
        "adapter_tensors": tensor_records(get_peft_model_state_dict(model)),
        "autocast": "cuda-bfloat16", "use_cache": False, "model_eval": True,
        "logit_comparison": {"rtol": 0.001, "atol": 0.02}})
    model.train(was_training)


class GuardCallback(TrainerCallback):
    def on_train_begin(self, args, state, control, model=None, **kwargs):
        self.before = snapshot(model)
        write("frozen-before.json", self.before)
        return control

    def on_train_end(self, args, state, control, model=None, **kwargs):
        after = snapshot(model)
        check_frozen(self.before["frozen"], after["frozen"])
        after["unchanged"] = True
        after["global_step"] = state.global_step
        write("frozen-after.json", after)
        posttraining_reference(model, state.global_step)
        return control


class FrozenBaseGuard(BasePlugin):
    def add_callbacks_post_trainer(self, cfg, trainer):
        return [GuardCallback()]
