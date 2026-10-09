# Closed simulation-manifest dependency

The sole addition to `scripts/run_simulation_qa.py`, inside
`ADDITIONAL_FULL_TESTS`, is:

```python
    # Inert bounded attestation store; supplied facts and manual interleavings only.
    "tests/test_attestation_store.py",
```

Root recorded a narrow same-builder serial release in
`WISP_ATTESTATION_S2A_SERIAL_MANIFEST_RELEASE_20261009.json`, SHA-256
`c7c4782f487b27b26feb03675a14ce0aac6b25a6f2e08003c796fa2e23d68ee8`, after
verifying the prior writer's window closed. The builder freshly acknowledged
the scope and verified the 39,734-byte base manifest SHA-256
`fee580328be1f2190c74fee20d9a9f424cadc11bd0e1a9cbe8f257ed2e0d64d0` before
editing. The exact diff adds only those two lines. The result is 39,858 bytes,
SHA-256 `e81b7dda51349ecb7da76a720d282673363de9bc3dd44526de713c20b50de1a9`.

The window is **CLOSED; NO MORE MANIFEST EDITS**. Private ACK, before/after
inventory and explicit closure are retained in the execution packet. Any later
manifest edit requires a new recorded serialized window. This classification
does not authorize running the full simulation profile or unrelated tests.
