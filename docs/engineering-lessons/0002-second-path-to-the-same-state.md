# 0002: a second screen wrote the same state through the wrong door

Status: proposed (2026-11-19). Only the repo owner adopts a lesson.

**Observed failure.** The shortlist's "is wrong" button reused `set_tag(..., review_state="needs_review")`.
That call means "a reviewer decided", so the new label-quality log counted the flag as a rejection, hid
the label from the "waiting" count, and lost the reviewer's real decision made later in the queue.
Found by the independent review; the tests covered the queue's Accept and Reject but not the flag.

**Verified root cause.** One function served two meanings (decide, and ask for another look). When a
side effect was added for the first meaning, the second caller inherited it.

**Correction.** `db.flag_tag` only moves a label back to the queue and keeps who proposed it. Regression
test: `test_flagging_an_ai_label_puts_it_back_in_the_queue_and_its_final_fate_is_counted`.

**Smallest general lesson.** Before adding a side effect to a shared write function, list its callers
and test each caller's path, not just the one the feature was built for.

**Narrowest enforcing layer.** Test (in place). Same round, same kind of gap: the 7-day window was
tested at day 6 and day 42 but not at the boundary (7 days 23 hours).
