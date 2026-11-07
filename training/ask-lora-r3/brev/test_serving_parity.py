"""render.py's prompts against oMLX's own request code. Run with oMLX's Python, because only it has that code:

    R=/Applications/oMLX.app/Contents/Resources
    PYTHONPATH="$R:$R/Python/framework-mlx-base/lib/python3.11/site-packages" \
        $R/Python/cpython-3.11/bin/python3.11 test_serving_parity.py MODEL_DIR ROWS.jsonl META.jsonl [N]

No model is loaded; only the tokenizer and template are used.
"""
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import render as rd  # noqa: E402

from omlx.api.openai_models import Message  # noqa: E402
from omlx.api.tool_calling import convert_tools_for_template, enrich_tool_params_for_gemma4  # noqa: E402
from omlx.adapter.gemma4 import extract_gemma4_messages  # noqa: E402
from omlx.api.utils import prepare_system_messages_for_template  # noqa: E402
from transformers import AutoTokenizer  # noqa: E402


def served_prompt(tok, messages, tools, kwargs):
    # The engine's own extractor for Gemma 4 (adapter.output_parser.detect_message_extractor), not the generic ones.
    extracted = extract_gemma4_messages([Message(**m) for m in messages], None, tok, consolidate_system_messages=False)
    # oMLX turns preserve_thinking on for this template (model_discovery.detect_preserve_thinking).
    served_kwargs = {**kwargs, "preserve_thinking": True}
    served_tools = enrich_tool_params_for_gemma4(convert_tools_for_template(tools)) if tools else None
    prepared = prepare_system_messages_for_template(extracted, tok, tools=served_tools, chat_template_kwargs=served_kwargs,
                                                    unsupported_mid_system_policy="user_note_safe")
    return tok.apply_chat_template(prepared, tools=served_tools, tokenize=False, add_generation_prompt=True, **served_kwargs)


def main():
    model, rows_path, meta_path = sys.argv[1:4]
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 500
    tok = AutoTokenizer.from_pretrained(model)
    tok.has_tool_calling = True   # mlx-lm and mlx-vlm set this on Gemma 4's tokenizer; the live probe confirms it
    rows = list(zip(open(rows_path), open(meta_path)))
    random.seed(7)
    bad = []
    picked = random.sample(rows, min(n, len(rows)))
    for line, meta_line in picked:
        ex, meta = json.loads(line), json.loads(meta_line)
        ours = rd.render(tok, ex, meta)["prompt"]
        served = served_prompt(tok, ex["messages"][:-1], ex.get("tools") or None, dict(meta.get("chat_template_kwargs") or {}))
        if ours != served:
            bad.append(meta.get("id"))
    print(f"{len(picked) - len(bad)}/{len(picked)} prompts byte-identical to oMLX's", bad[:10])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
