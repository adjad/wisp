# Synthetic browser/discovery contract corpus

The JSON files contain independent cases executed unchanged by Python, Node and
Swift. All sites, source records, text, identities and approvals are synthetic.
Approval examples describe wire shape only and grant no real authorization.

Cases declare an operation (`validate`, `negotiate`, `proposal`, `completion`),
its inputs, expected validity, and an error code for rejection. Validation cases
also require lossless round-trip output, including explicit nulls. Negotiation
cases declare the expected common capabilities. `rejections.json` contains
malformed and unsafe boundary inputs; accepting one is a failing check.

Run `python3 scripts/check_browser_contracts.py` from the repository root.
