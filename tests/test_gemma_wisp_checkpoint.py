"""0.5.0 ships Gemma Wisp as the only offered chat model, so its published files must turn on
look-at-screen and voice: vision.py and voice.py decide from config.json and the weight index
alone (the extract below), and personal_style.py leaves out the MTP head by its file name.

What this does and doesn't prove: the fixture is an extract of the local files, so these tests pin the
LAYOUT the code depends on, not the published bytes. The published files are checked separately, by
sha256 against the upload manifest, once the Hub upload is complete (training/ask-lora-r5/hf/), and every
download is checked against the same hashes (catalog.GEMMA_WISP_FILES, local_models.verify_download)."""
import json
from pathlib import Path

from service import vision, voice
from service.setup import catalog

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "gemma_wisp_checkpoint.json").read_text())


def _checkpoint(tmp_path) -> Path:
    root = tmp_path / "models" / "Neo-Arid" / catalog.GEMMA_E4B_WISP.id
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps(FIXTURE["config"]))
    weight_map = {name: "model-00001-of-00002.safetensors" for name in FIXTURE["weight_names"]}
    (root / "model.safetensors.index.json").write_text(json.dumps({"weight_map": weight_map}))
    return root


def test_the_published_layout_has_vision_and_audio(tmp_path):
    root = _checkpoint(tmp_path)
    assert vision.checkpoint_has_vision(str(root)) and voice.checkpoint_has_audio(str(root))
    assert FIXTURE["counts"]["vision"] > 0 and FIXTURE["counts"]["audio"] > 0


def test_without_the_towers_neither_turns_on(tmp_path):
    root = _checkpoint(tmp_path)
    text_only = [n for n in FIXTURE["weight_names"] if n.startswith("language_model.")]
    (root / "model.safetensors.index.json").write_text(json.dumps({"weight_map": dict.fromkeys(text_only, "x")}))
    assert not vision.checkpoint_has_vision(str(root)) and not voice.checkpoint_has_audio(str(root))


def test_the_fixture_is_the_uploaded_files_and_the_mtp_head_has_the_expected_name():
    uploaded = FIXTURE["uploaded_files"]           # the HF upload manifest, file by file
    for name in ("config.json", "model.safetensors.index.json"):
        assert uploaded[name]["sha256"] == FIXTURE["source_sha256"][name]
    assert "model-mtp.safetensors" in uploaded     # personal_style.py leaves out exactly this name
    assert "model-mtp.safetensors" in Path(vision.__file__).with_name("personal_style.py").read_text()


def test_the_catalog_pins_are_the_manifest():
    pins = {name: (sha, size) for name, sha, size in catalog.GEMMA_E4B_WISP.pinned_files}
    for name, row in FIXTURE["uploaded_files"].items():
        if name != "README.md":
            assert pins[name] == (row["sha256"], row["bytes"]), name
