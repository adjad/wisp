# Ling LoRA on Brev: compatibility stage

User authorization: Axolotl LoRA on Brev, at most $30 existing credits total,
including at most $5 for initial compatibility. One A6000 48GB, no card charge,
auto-recharge, automatic paid retry, public upload, or production activation.
The first smoke uses 20 disposable synthetic examples and six development probes.
They test the pipeline; they cannot establish routing-quality improvement.
The 2,000/200/300 pilot and independent final quality set have not been generated.

## Current state

Prepared locally; **no instance, training, or spend**. Account sign-in is pending.
`launch-admission.template.json` deliberately fails the launch gate. No credentials
belong in the repository, package, logs, or report.

Source pins are in `pins.json`. The routing system prompt/schema are copied from
the frozen Wisp 1.3 candidate, not imported or executed. Data comes from new
specifications, never personal logs or the existing sealed evaluation.

## Before allocating compute

1. Verify the authenticated credit balance, auto-recharge disabled, exact eligible
   A6000 offer, billing increment, disk/egress/tax charges, and private access.
2. Establish independent provider/control-plane termination with a conservative
   deadline and margin, and a private backup destination. A training-process
   timeout or an exited Docker container **does not stop VM billing**. If a reliable
   spending cap/termination path is unavailable, do not launch.
3. Fill a private `launch-admission.json` from verified facts; retain its sanitized
   evidence. Runtime recipe qualification here means static dependency/image
   review; actual kernel/backward qualification happens in the capped cloud smoke.
4. Runtime admission requires the actual already-allocated instance ID and billing
   start at or before the current time, bound to provider allocation and termination
   evidence. A prelaunch cost estimate cannot authorize model work.
5. Inspect the precise VM driver/CUDA image. The pinned Axolotl cloud image is
   only a candidate: its source build targets newer GPU architectures. A6000
   compatibility remains unverified. Do not launch an unsuitable image or assume
   a public catalog offer guarantees installed driver compatibility.

At $0.60/hour, a two-hour compute window is $1.20. This is illustrative only; the
gate includes actual billing increments, noncompute reserves and at least $0.50
for teardown latency. Record billing start from allocation, including setup.

## GPU preparation and smoke

Upload only this synthetic bundle privately. Keep the complete source pins and
data manifests. Do not sync the entire Wisp repository or user home.

`bootstrap_cloud.sh` installs the pinned official Axolotl source into an isolated
qualified cloud environment. It does not install a GPU driver. The source now
requires FLA0.5.2; do not blindly apply the README's old FLA0.4.1/Triton3.5.1 fix.

Run `bash run_cloud_smoke.sh prepare`. It checks actual CUDA operations, immutable
Axolotl source binding, complete token lengths, generation/training template
prefixes, and Axolotl preprocessing. Parent then inspects decoded actual training
label spans: system/user/prior assistant masked, complete final answer and EOT
supervised. Save `artifacts/label-audit.json` with `passed`, complete `input_identity` copied from
`artifacts/prepared-input-identity.json`,
`prompt_and_prior_assistant_masked`, and
`complete_final_assistant_and_eos_supervised`. Failure stops the stage.

Run `bash run_cloud_smoke.sh train` only after the audit. This runs matched-base
development inference, four actual attention-LoRA optimizer updates, finite
gradient/update checks, exits training, and reloads the adapter in a fresh process
through Axolotl's Bailing classes with checkpoint conversions registered. It compares
loaded adapter keys/values/dtypes to both the saved checkpoint and the posttraining
reference, and checks a fresh-process forward against recorded posttraining logits
(rtol 0.001, atol 0.02; failures are retained and block scaling).
The guard callback inventories actual trainables and hashes all frozen state before
and after training. It requires FP32 attention adapter parameters and unchanged
experts/router/embedding/head tensors; no native tools execute.
SDPA and unpacked examples reduce optional dependencies; this differs from the
published FlashAttention2 packed H100 benchmark and has no measured throughput yet.

## Backup, teardown, and next gate

Download adapter/config, tokenizer/template identity, raw logs and receipts into
private local evidence. Generate SHA256 manifests on both sides and compare them
before terminating/deleting owned billable resources. Verify resource state and
final charges through Brev; do not infer teardown from SSH disconnect or process
exit. Never delete the only checkpoint copy. The operator must implement the
provider-specific backup/termination command after authenticated access is known.

Cloud smoke success is insufficient to start the pilot. Verify merge/export,
published expert weight layout, identical-base quantization controls, and isolated
Mac serving reload under separately admitted shared-resource scope. Production
Wisp stays unchanged. Record failures honestly and retain partial evidence.

## Local checks

`python3 build_smoke.py` rebuilds only disposable synthetic smoke files.
`python3 -m unittest -v test_package.py` verifies integrity, supervision metadata,
split IDs and fail-closed budget arithmetic. Cloud dependencies/GPU code have not
been executed locally. Full schema validation and independent static review are
recorded separately. No pilot accuracy, latency, or training gain is claimed.
