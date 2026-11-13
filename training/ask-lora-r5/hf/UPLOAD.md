# Hugging Face upload: r5 step 1800 (prepared 2026-11-12, NOT uploaded)

**Proposed repo:** `Neo-Arid/gemma-4-E4B-it-r5-step-01800-oQ4e-mtp`. It must be public, because Wisp's setup downloads it.
- The owner is a guess, taken from Wisp's Mellum repo (`Neo-Arid/...`). The user confirms the account.
- The repo name must stay equal to the model id: oMLX serves a download under its folder name, and Wisp's roles name that id.
- A different owner is a one-line change in `service/setup/catalog.py` (`GEMMA_E4B_WISP.repo`) plus one test (`tests/test_setup_guide.py`).

**Staged folder:** `~/Desktop/r5-hf-upload/gemma-4-E4B-it-r5-step-01800-oQ4e-mtp`.
- It holds hard links to the quantized model, so it takes no extra space, plus `README.md` (the model card in this folder).
- It sits outside `OMLX_Model_Files`, so oMLX doesn't list it twice.
- Files, sizes and sha256 are in `MANIFEST.sha256-size.txt`: 11 files, 5,618,450,266 bytes in total (5.62 GB).

**Left out on purpose:**
- `wisp-quant-recipe.json`: it carries local paths (`/Users/<name>/Desktop/...`) and a write time.
- `oq_imatrix_report.json`: it carries a local cache path and this Mac's memory figures. oMLX only writes this file (`omlx/oq.py:7938`) and never reads it.

**Privacy checks run (read-only):**
- The safetensors headers' `__metadata__` is `{"format": "mlx"}` on all three files. No tensor name holds a path, user name, email, host or date.
- Text and JSON files: no `/Users/`, `/home/`, user name, email or host strings, except in the two excluded files.
- The tokenizer and chat template are byte-identical to `Jundot/gemma-4-E4B-it-oQ4e-mtp`, as is `model-mtp.safetensors`.
- Training data, in general terms: synthetic worlds, tool results and people.
  - Answers come from base Gemma (self-distillation).
  - Images were generated, and audio is macOS `say`.
  - `debug_log_r5` rows vary the user's own Ask question phrasings, with names and addresses replaced by made-up people. Their wording is the user's, not synthetic. Whether a public model may carry that is the user's call; the card says it in general terms.
  - The weights can't be scanned for memorized phrasing. No real email, message, contact or file content entered any row (scen_r5_debuglog.py's contract).

**Commands for the user** (they run these themselves; Claude never handles the token). Install the CLI once, if `hf` is missing:

    python3 -m pip install -U huggingface_hub

Log in (interactive; paste a write token from huggingface.co/settings/tokens):

    hf auth login

Upload the staged folder. This creates the repo, public by default:

    hf upload Neo-Arid/gemma-4-E4B-it-r5-step-01800-oQ4e-mtp ~/Desktop/r5-hf-upload/gemma-4-E4B-it-r5-step-01800-oQ4e-mtp . --repo-type model --commit-message "Wisp's tuned Gemma 4 E4B (Ask LoRA r5, step 1800), oQ4e + MTP"

**After the upload:** Claude can check the public repo's file list and sha256 values against the manifest, with no token needed.
