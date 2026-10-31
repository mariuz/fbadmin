"""Test-only AI provider for the e2e tests (checkup-ai.spec.ts): put this folder on PYTHONPATH and set
FBADMIN_AI_PROVIDER=e2e-fake. It answers from the prompt, without any network."""

import os
import re

if os.environ.get("FBADMIN_AI_PROVIDER") == "e2e-fake":
    from fbadmin import ai
    from fbadmin.ai.providers import AIProvider

    class FakeProvider(AIProvider):
        name = "e2e-fake"

        def describe(self):
            return {"provider": "e2e-fake", "model": "canned-1"}

        def complete(self, system, prompt):
            q = re.search(r"Follow-up question from the administrator: (.*)$", prompt)
            if q:
                return f"You asked: **{q.group(1)}**. The cache holds `2048` pages, which is enough here."
            idx = re.findall(r"^\s+- (IX_\w+)", prompt, re.M)
            return (
                "## Diagnosis\n"
                "The optimizer has no selectivity for the indices of a loaded table (finding 1).\n\n"
                "## Recommendations\n"
                "1. **Recompute selectivity** of " + ", ".join(idx or ["the indices"]) + " (finding 1).\n"
                "   Low risk, takes seconds.\n"
                "2. Turn on history so regressions can be found.\n\n"
                "```sql\n" + "\n".join(f"SET STATISTICS INDEX {i};" for i in idx) + "\n```\n\n"
                "## What to check next\n- The plan of the slowest statement.\n"
            )

    ai.register_provider("e2e-fake", FakeProvider)
