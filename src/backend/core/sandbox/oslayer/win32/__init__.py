"""Windows-only implementation details of the low-integrity sandbox.

Kept deliberately empty: :mod:`ffi` refuses to import off Windows, so anything
that re-exported it here would make the whole package unimportable on the hosts
where the pure-Python parts (the policy model, the plan builder) still need to
be testable.
"""
