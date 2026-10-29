import math
import random
import statistics
import unittest

from second_scribe.stats import (bootstrap_ci, cohen_kappa, derangements, paired_bootstrap_ci,
                                 pairwise_bootstrap_ci, permutation_rank, sign_test)


class TestStats(unittest.TestCase):
    def test_sign_test(self):
        r = sign_test([1, 1, 1, 1, 1, 1, 1, 1, 0, -1])
        self.assertEqual((r["n_pos"], r["n_neg"], r["n_zero"]), (8, 1, 1))
        self.assertAlmostEqual(r["p"], 0.0390625)

    def test_sign_test_edges(self):
        self.assertEqual(sign_test([])["p"], 1.0)
        self.assertEqual(sign_test([0, 0])["p"], 1.0)
        self.assertEqual(sign_test([1, -1])["p"], 1.0)          # capped at 1
        self.assertAlmostEqual(sign_test([1, 1, 1, 1])["p"], 0.125)

    def test_bootstrap_deterministic(self):
        self.assertEqual(bootstrap_ci([1, 2, 3, 4], seed=3), bootstrap_ci([1, 2, 3, 4], seed=3))

    def test_bootstrap_seed_changes_result_and_covers_point(self):
        vals = [0.1, 0.5, 0.9, 0.3, 0.7, 0.2, 0.8]
        a, b = bootstrap_ci(vals, seed=1), bootstrap_ci(vals, seed=2)
        self.assertNotEqual(a, b)
        point, lo, hi = a
        self.assertAlmostEqual(point, statistics.mean(vals))
        self.assertLessEqual(lo, point)
        self.assertGreaterEqual(hi, point)

    def test_bootstrap_does_not_touch_global_random(self):
        random.seed(123)
        expected = random.random()
        random.seed(123)
        bootstrap_ci([1, 2, 3, 4], seed=5)
        paired_bootstrap_ci([1, 2, 3], [0, 1, 2], seed=5)
        permutation_rank([[1.0, 0.0], [0.0, 1.0]])
        derangements(4, 3, seed=1)
        self.assertEqual(random.random(), expected)

    def test_bootstrap_constant_values_zero_width(self):
        self.assertEqual(bootstrap_ci([2.0, 2.0, 2.0]), (2.0, 2.0, 2.0))

    def test_bootstrap_empty_is_nan(self):
        self.assertTrue(all(math.isnan(x) for x in bootstrap_ci([])))

    def test_paired_bootstrap_resamples_pairs_not_elements(self):
        # a - b is constant 1 for every pair. Resampling a and b independently would give
        # spread; resampling pairs keeps every resampled mean difference exactly 1.
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [0.0, 1.0, 2.0, 3.0, 4.0]
        point, lo, hi = paired_bootstrap_ci(a, b, seed=7)
        self.assertEqual((point, lo, hi), (1.0, 1.0, 1.0))

    def test_paired_bootstrap_length_mismatch(self):
        with self.assertRaises(ValueError):
            paired_bootstrap_ci([1.0], [1.0, 2.0])

    def test_pairwise_bootstrap_resamples_participants(self):
        # 4 participants, set A pairwise 1.0 everywhere, set B pairwise 0.0: diff is 1 whatever is drawn
        n = 4
        ma = [[1.0] * n for _ in range(n)]
        mb = [[0.0] * n for _ in range(n)]
        point, lo, hi = pairwise_bootstrap_ci(ma, mb, seed=1)
        self.assertEqual((point, lo, hi), (1.0, 1.0, 1.0))

    def test_pairwise_bootstrap_mask_and_degenerate(self):
        n = 3
        ma = [[0.5] * n for _ in range(n)]
        mb = [[0.2] * n for _ in range(n)]
        mask = [[False] * n for _ in range(n)]
        self.assertTrue(all(math.isnan(x) for x in pairwise_bootstrap_ci(ma, mb, mask=mask, seed=1)))

    def test_pairwise_bootstrap_no_usable_resample_gives_nan_bounds(self):
        ma = [[0.6] * 3 for _ in range(3)]
        mb = [[0.2] * 3 for _ in range(3)]
        point, lo, hi = pairwise_bootstrap_ci(ma, mb, B=0, seed=1)         # no resample at all
        self.assertAlmostEqual(point, 0.4)
        self.assertTrue(math.isnan(lo) and math.isnan(hi))
        # one usable pair among 4 participants: resamples that miss it are skipped, the others still count
        mask = [[False] * 4 for _ in range(4)]
        mask[0][1] = mask[1][0] = True
        ma4 = [[0.5] * 4 for _ in range(4)]
        mb4 = [[0.0] * 4 for _ in range(4)]
        self.assertEqual(pairwise_bootstrap_ci(ma4, mb4, B=300, seed=2, mask=mask), (0.5, 0.5, 0.5))

    def test_permutation_rank_perfect(self):
        sim = [[1.0 if i == j else 0.1 for j in range(10)] for i in range(10)]
        r = permutation_rank(sim)
        self.assertEqual(r["mean_rank"], 1.0)
        self.assertLess(r["p"], 0.001)
        self.assertEqual(r["share_rank1"], 1.0)
        self.assertEqual(r["expected_rank_null"], 5.5)

    def test_permutation_rank_uninformative(self):
        sim = [[0.5] * 10 for _ in range(10)]               # all tied: average rank, no signal
        r = permutation_rank(sim)
        self.assertEqual(r["mean_rank"], 5.5)
        self.assertEqual(r["share_rank1"], 0.0)
        self.assertGreater(r["p"], 0.5)

    def test_permutation_rank_worst(self):
        sim = [[0.0 if i == j else 1.0 for j in range(6)] for i in range(6)]
        r = permutation_rank(sim)
        self.assertEqual(r["mean_rank"], 6.0)
        self.assertGreater(r["p"], 0.99)

    def test_permutation_rank_deterministic_and_n_one(self):
        sim = [[0.3, 0.1, 0.2], [0.2, 0.9, 0.1], [0.1, 0.4, 0.5]]
        self.assertEqual(permutation_rank(sim, seed=4), permutation_rank(sim, seed=4))
        self.assertTrue(math.isnan(permutation_rank([[1.0]])["mean_rank"]))

    def test_kappa(self):
        self.assertAlmostEqual(cohen_kappa(list("aabb"), list("aabb")), 1.0)

    def test_kappa_chance_and_disagreement(self):
        self.assertAlmostEqual(cohen_kappa(list("aabb"), list("abab")), 0.0)
        self.assertAlmostEqual(cohen_kappa(list("aabb"), list("bbaa")), -1.0)
        # hand computed: po = 3/4, pe = .5*.75 + .5*.25 = .5 -> kappa .5
        self.assertAlmostEqual(cohen_kappa(list("aabb"), list("aaba")), 0.5)

    def test_kappa_degenerate_and_mismatch(self):
        self.assertTrue(math.isnan(cohen_kappa(list("aaa"), list("aaa"))))
        self.assertTrue(math.isnan(cohen_kappa([], [])))
        with self.assertRaises(ValueError):
            cohen_kappa(["a"], ["a", "b"])

    def test_derangements(self):
        perms = derangements(5, 4, seed=1)
        self.assertEqual(len(perms), 4)
        for perm in perms:
            self.assertTrue(all(perm[i] != i for i in range(5)))
            self.assertEqual(sorted(perm), list(range(5)))
        self.assertEqual(perms, derangements(5, 4, seed=1))
        self.assertNotEqual(perms, derangements(5, 4, seed=2))

    def test_derangements_small_n(self):
        self.assertEqual(derangements(2, 3, seed=0), [[1, 0]] * 3)
        with self.assertRaises(ValueError):
            derangements(1, 1)


if __name__ == "__main__":
    unittest.main()
