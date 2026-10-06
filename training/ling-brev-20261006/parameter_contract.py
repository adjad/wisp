"""Framework-free invariant checks, also exercised with tiny inert fixtures."""
TARGETS = {"q_proj", "k_proj", "v_proj", "o_proj", "q_a_proj", "q_b_proj", "kv_a_proj_with_mqa", "kv_b_proj", "dense"}


def is_adapter(name):
    return (".lora_A." in name or ".lora_B." in name) and any("."+target+"." in name for target in TARGETS)


def check_inventory(rows):
    trainable = [row for row in rows if row["requires_grad"]]
    frozen = [row for row in rows if not row["requires_grad"]]
    if not trainable or not frozen: raise ValueError("Both trainable adapters and frozen base are required")
    for row in trainable:
        if not is_adapter(row["name"]): raise ValueError("Unexpected trainable parameter: " + row["name"])
        if row["dtype"] != "torch.float32": raise ValueError("Require explicit FP32 adapter training/reload policy")
    return trainable


def check_frozen(before, after):
    if not before or before != after: raise ValueError("Frozen base tensor identity/content changed")


def check_adapter_records(expected, actual):
    if not expected or expected != actual:
        raise ValueError("Adapter tensor keys, shapes, dtypes or values differ from posttraining reference")


def token_ids(value):
    if not isinstance(value, list) or not value or not all(type(n) is int for n in value):
        raise ValueError("Expected nonempty flat token ID list")
    return value
