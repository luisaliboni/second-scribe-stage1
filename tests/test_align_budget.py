"""R51 (FABLE review 3.3): the align call's token budget is 400 + 60 x chunk, so a dense alignment of 60 labels
(every left label denoting several right labels) is not cut off at T = 0 and sent to the lexical fallback."""
import tempfile
import unittest
from pathlib import Path

from second_scribe.audit.registry import AuditLog
from second_scribe.llm import MockClient
from second_scribe.match import align_labels

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


class _Spy(MockClient):
    def __init__(self):
        super().__init__(fn=lambda m: "{}")
        self.max_tokens = []

    def complete(self, messages, *, temperature, seed, max_tokens, params=None):
        self.max_tokens.append(max_tokens)
        return super().complete(messages, temperature=temperature, seed=seed, max_tokens=max_tokens, params=params)


class TestAlignBudget(unittest.TestCase):
    def budgets(self, n_left):
        with tempfile.TemporaryDirectory() as d:
            spy = _Spy()
            align_labels(AuditLog(Path(d) / "log.jsonl", "t"), spy, [f"left {i}" for i in range(n_left)],
                         ["right a", "right b"], PROMPTS, item_id="p")
            return sorted(set(spy.max_tokens))

    def test_sixty_labels_get_4000_tokens_not_2800(self):
        self.assertEqual(self.budgets(60), [400 + 60 * 60])

    def test_budget_follows_the_chunk_size(self):
        self.assertEqual(self.budgets(3), [400 + 60 * 3])
        self.assertEqual(self.budgets(70), sorted([400 + 60 * 60, 400 + 60 * 10]))


if __name__ == "__main__":
    unittest.main()
