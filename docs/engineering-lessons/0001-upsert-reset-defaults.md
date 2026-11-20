# 0001: an upsert reset flags to their column default

Status: proposed (2026-11-19). Only the repo owner adopts a lesson.

**Observed failure.** Re-importing a post or creator without naming `sponsored` or `is_owner` set
them back to 0, so a sponsored post re-entered the ranking sample. Found by the independent review,
not by the first 49 tests.

**Verified root cause.** `ON CONFLICT DO UPDATE SET x = COALESCE(excluded.x, x)`. For a
`NOT NULL DEFAULT 0` column, `excluded.x` is the default, never NULL, so COALESCE always overwrote.

**Correction.** `db._upsert` updates only the columns the caller supplied. Regression tests:
`test_a_later_import_does_not_unmark_sponsored_posts_or_the_owner` and
`test_ranking_leaves_out_sponsored_posts_and_samples_the_five_most_recent`.

**Smallest general lesson.** A rule the product states ("sponsored posts are excluded") needs a test
that sets the state, replays the import, and checks the state survived.

**Narrowest enforcing layer.** Test (in place).
