# Snapshot3 mechanical advisory binding

Verdict: **PASS_WITH_NOTES**. No required repair remains. This update carries the snapshot2 advisory verdict forward to unchanged data and QA; it does not repeat semantic, QA, generation, or runtime gates.

The only changed existing file is `shards/arguments/generate.py`: exactly one trailing ASCII space was removed from each of lines **156 and 160**, totaling two bytes. Every other line is byte-identical. Parsed ASTs, including source locations, are equal. Compiled marshaled code objects are byte-identical using the same synthetic filename; compilation did not execute the generator. The new generator SHA256 is `98891f67995a6a95bfd0633455c6d5ea159f3ea99baa3ac79260d2c9620caa8d`.

All **39 other prior input files** match snapshot2 byte-for-byte by verified hashes. The three added files are the ZIP and its two receipts. All **43 snapshot3 files plus its manifest** and all **40 snapshot2 files plus its manifest** were verified at start and completion and remain unchanged.

- Snapshot3 manifest SHA256: `402e67d52f0547551d4f1b2f246ca80c296cc98dc592f71c1f6c58efa8b76383`.
- Prior snapshot2 manifest SHA256: `4fe27d9ec60788a234f857437af244c3c6ed2c935a5080049a126a7e39c2e391`.
- ZIP SHA256: `3c2a45238f8cc4669b440e24fa9a18c49990d284eaa005ed238da5db65cb3ded`, **1,316,226 bytes**.
- ZIP SHA, receipt, allowlisted 15-member inventory, member hashes, embedded upload manifest, and CRC agree with the frozen files.
- Source base: `20e256d91bcb0bc34f5eb3a6c506bb9edc90f6fe`; snapshot-declared integration base: `ebe82735d699a88c05c94fa41fd0fee0e35fdd68`.

The initial snapshot1 BLOCK and snapshot2 PASS_WITH_NOTES reports remain preserved. Snapshot2's bounded attribution/entailment and lexical deduplication limits still apply. Tokenizer target bounds, actual trainer masking/compatibility, measured model quality, production behavior, and activation/export readiness remain unqualified. This is advisory mechanical binding, not formal Release Auditor credit or merge/activation/deployment approval.

No check failed. Only advisory output under `/private/tmp/ling-dataset-v2-review/v3/**` was written. No snapshot/candidate writes, Git, helper, product execution, model, network, native/cloud operation, or original old/final/evaluation data access occurred. Reviewer is quiescent after delivery. `receipt.json` binds this report and the complete before/after hash evidence.
