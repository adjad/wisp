"""Line-delimited English MLX inference subprocess; local synthetic fixtures only."""
import json
import os
import resource
import sys
import time
import warnings

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
# Keep loader/calibration diagnostics off the line-JSON stdout protocol.
warnings.simplefilter("always")

from common import LAYA_QUESTION, NOW
import laya_mlx as laya

model = "/Users/adijain/Desktop/OMLX_Model_Files/aac6fef/laya-mlx"
started = time.perf_counter()
agent = laya.load(
    model,
    device="gpu",
    dtype="float16",
    batch_size=1,
    compile=False,
    cache_prompts=False,
)
print(
    json.dumps({
        "ready": True,
        "load_seconds": time.perf_counter() - started,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }),
    flush=True,
)

for line in sys.stdin:
    try:
        item = json.loads(line)
        case = item["case"]
        questions = item.get("question", LAYA_QUESTION)
        state = [
            {"role": "system", "content": f"Current time {NOW} America/Los_Angeles."}
        ] + case.get("context", []) + [{"role": "user", "content": case["prompt"]}]
        started = time.perf_counter()
        result = agent.predict(state, questions)
        print(
            json.dumps({
                "id": case["id"],
                "result": result,
                "seconds": time.perf_counter() - started,
                "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            }),
            flush=True,
        )
    except Exception as error:
        print(json.dumps({"error": type(error).__name__ + ": " + str(error)}), flush=True)
