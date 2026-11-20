# 0003: the model's self-test was shown the creators it had trained on

Status: proposed (2026-11-19). Only the repo owner adopts a lesson.

**Observed failure.** `learner.evaluate` split the videos into five folds by position, so one creator's
videos landed on both the training and the test side. A creator's signature words then "predicted" that
creator's other videos. On pure noise (12 creators, one unique word each, a coin-flip label per creator)
the reviewer measured the model being reported as "better than guessing" in 99.5% of 200 runs, and that
verdict was shown in green on the Assistant screen and passed to the AI model as a fact. Found by the
independent review.

**Verified root cause.** The examples are not independent: videos cluster by creator, and the label
(fast against the creator's own usual) is shared within the cluster. A split that ignores the cluster
measures recognition, not learning. The first tests could not see it: the "nothing to learn" test used
one unique word per video, which the count-of-two rule removes before the leak can act.

**Correction.** Folds are assigned by creator; training needs at least five creators; "useful" needs a
margin of 1 / sqrt(videos), never under five points. Regression tests:
`test_the_models_self_test_never_shows_it_a_creator_it_trained_on`,
`test_training_needs_several_creators_not_one_prolific_one`.

**Smallest general lesson.** Before trusting a held-out score, name what the rows are grouped by
(creator, channel, day, brief) and split by that group. Test the evaluator on noise that has the same
grouping as the real data; it must say "no better than guessing".

**Narrowest enforcing layer.** Test (in place).
