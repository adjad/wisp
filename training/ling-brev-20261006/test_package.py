import copy
import datetime as dt
import hashlib
import json
import unittest
import tempfile
from pathlib import Path
from admission import validate
from bundle_identity import identity, check_audit
from parameter_contract import check_inventory, check_frozen, token_ids, check_adapter_records

ROOT = Path(__file__).resolve().parent


class PackageTests(unittest.TestCase):
    def test_reload_records_detect_missing_changed_and_wrong_dtype(self):
        expected={"q.lora_A.weight":{"shape":[16,32],"dtype":"torch.float32","sha256":"trained-A"},
                  "q.lora_B.weight":{"shape":[32,16],"dtype":"torch.float32","sha256":"trained-B"}}
        check_adapter_records(expected,copy.deepcopy(expected))
        for replacement in ({}, {"shape":[32,16],"dtype":"torch.float32","sha256":"untrained-zero-B"},
                            {"shape":[32,16],"dtype":"torch.bfloat16","sha256":"trained-B"}):
            actual=copy.deepcopy(expected);actual["q.lora_B.weight"]=replacement
            with self.assertRaises(ValueError):check_adapter_records(expected,actual)
        actual=copy.deepcopy(expected);del actual["q.lora_B.weight"]
        with self.assertRaises(ValueError):check_adapter_records(expected,actual)

    def test_trainable_parameter_contract(self):
        rows=[{"name":"model.layers.0.attention.q_proj.lora_A.default.weight","requires_grad":True,"dtype":"torch.float32"},
              {"name":"model.layers.0.mlp.gate.weight","requires_grad":False,"dtype":"torch.float32"}]
        self.assertEqual(len(check_inventory(rows)),1)
        wrong=copy.deepcopy(rows);wrong[1]["requires_grad"]=True
        with self.assertRaises(ValueError):check_inventory(wrong)
        wrong=copy.deepcopy(rows);wrong[0]["dtype"]="torch.bfloat16"
        with self.assertRaises(ValueError):check_inventory(wrong)
        check_frozen({"base":"hash1"},{"base":"hash1"})
        with self.assertRaises(ValueError):check_frozen({"base":"hash1"},{"base":"hash2"})
        with self.assertRaises(ValueError):check_frozen({"base":"hash1"},{})

    def test_explicit_token_list_contract(self):
        self.assertEqual(token_ids([1,2,3]),[1,2,3])
        for value in ({"input_ids":[1,2]},[[1,2]],[],[True],[1.0]):
            with self.subTest(value=value),self.assertRaises(ValueError):token_ids(value)

    def receipt(self):
        now = dt.datetime(2026, 10, 6, 5, tzinfo=dt.timezone.utc)
        r = json.loads((ROOT / "launch-admission.template.json").read_text())
        for k, v in list(r.items()):
            if v is False: r[k] = True
        r.update(credit_balance_usd=30, hourly_usd=.60, reserved_noncompute_usd=.50,
                 billing_quantum_seconds=60, provider="synthetic-provider", offer_id="synthetic-offer",
                 quote_verified_at_utc=now.isoformat(), billing_start_at_utc=now.isoformat(),
                 termination_at_utc=(now+dt.timedelta(hours=2)).isoformat(),
                 termination_receipt="synthetic-timer-proof", backup_destination="synthetic-private-destination")
        r["instance_id"] = "synthetic-instance-1"
        r["allocation_evidence"] = {k:r[k] for k in ("instance_id", "provider", "offer_id", "billing_start_at_utc")}
        r["termination_evidence"] = {k:r[k] for k in ("instance_id", "provider", "offer_id", "termination_at_utc")}
        return r, now

    def test_admission_template_cannot_launch(self):
        with self.assertRaises(ValueError): validate(json.loads((ROOT / "launch-admission.template.json").read_text()))

    def test_valid_cost_includes_reserves(self):
        r, now = self.receipt()
        self.assertAlmostEqual(validate(r, now)["estimated_inclusive_max_usd"], 2.20)

    def test_missing_safety_or_billing_facts_block(self):
        r, now = self.receipt()
        for key, value in r.items():
            if value is True:
                changed = copy.deepcopy(r); changed[key] = False
                with self.subTest(key=key), self.assertRaises(ValueError): validate(changed, now)

    def test_over_budget_expired_and_nonfinite_block(self):
        r, now = self.receipt()
        for change in ({"hourly_usd": 8}, {"hourly_usd": float("nan")}, {"smoke_cap_usd": 5.01},
                       {"earlier_spend_usd": 29}, {"credit_balance_usd": 4}, {"reserved_teardown_usd": 0},
                       {"termination_at_utc": (now-dt.timedelta(seconds=1)).isoformat()},
                       {"quote_verified_at_utc": (now-dt.timedelta(hours=2)).isoformat()},
                       {"gpu": "H100"}, {"billing_quantum_seconds": 0}):
            changed = r | change
            with self.subTest(change=change), self.assertRaises(ValueError): validate(changed, now)

    def test_future_start_and_instance_swap_block(self):
        r, now = self.receipt()
        r["billing_start_at_utc"] = (now + dt.timedelta(minutes=30)).isoformat()
        r["allocation_evidence"]["billing_start_at_utc"] = r["billing_start_at_utc"]
        with self.assertRaises(ValueError): validate(r, now)
        r, now = self.receipt(); r["instance_id"] = "different"
        with self.assertRaises(ValueError): validate(r, now)

    def test_elapsed_cost_is_not_reset(self):
        r, now = self.receipt()
        r["billing_start_at_utc"] = (now-dt.timedelta(hours=5)).isoformat()
        r["allocation_evidence"]["billing_start_at_utc"] = r["billing_start_at_utc"]
        with self.assertRaises(ValueError): validate(r, now)

    def test_label_audit_invalidates_on_input_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ["smoke.yml", "pins.json", "ling-chat-template.jinja", "cloud_probe.py", "data/train.jsonl", "data/dev.jsonl", "artifacts/prepared/data.arrow", "artifacts/runtime.json", "artifacts/tokenization.json", "artifacts/preprocess.log"]
            for name in paths:
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text("original")
            audit={"passed":True,"input_identity":identity(root),"prompt_and_prior_assistant_masked":True,"complete_final_assistant_and_eos_supervised":True}
            (root/'artifacts/label-audit.json').write_text(json.dumps(audit))
            check_audit(root)
            for name in paths:
                (root/name).write_text("changed")
                with self.subTest(name=name), self.assertRaises(ValueError): check_audit(root)
                (root/name).write_text("original")

    def test_smoke_integrity_and_no_cross_split_families(self):
        manifest = json.loads((ROOT / "data/manifest.json").read_text())
        family_sets = []
        prompts = set()
        for split, count in (("train", 20), ("dev", 6)):
            raw = (ROOT / "data" / (split+".jsonl")).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest["files"][split+".jsonl"]["sha256"])
            rows = [json.loads(line) for line in raw.splitlines()]
            self.assertEqual(len(rows), count)
            family_sets.append({r["family"] for r in rows})
            for row in rows:
                self.assertNotIn(row["messages"][-2]["content"], prompts)
                prompts.add(row["messages"][-2]["content"])
                self.assertTrue(row["messages"][-1]["training"])
                for m in row["messages"][:-1]:
                    self.assertNotIn("reasoning_content", m)
                    if m["role"] == "assistant": self.assertFalse(m["training"])
                if row["category"] == "routing":
                    self.assertEqual(json.loads(row["messages"][-1]["content"]), row["expected"])
                    self.assertEqual(set(row["expected"]), {"version", "kind", "sources", "excluded_sources", "unsupported_constraints"})
                    if row["expected"]["kind"] != "read": self.assertEqual(row["expected"]["sources"], [])
                    self.assertFalse({s["domain"] for s in row["expected"]["sources"]} & set(row["expected"]["excluded_sources"]))
        self.assertFalse(family_sets[0] & family_sets[1])


if __name__ == "__main__": unittest.main()
