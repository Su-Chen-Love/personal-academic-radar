# Building and updating the interest profile

Build a research profile as a compact decision rubric, not a bag of keywords.

## Evidence order

1. Explicit user statements and corrections.
2. Current proposal, research questions, and planned studies.
3. The user's authored papers and active manuscripts.
4. Papers repeatedly saved, cited, or positively rated.
5. Venue names and isolated keywords.

## Required profile sections

- Core problem: one paragraph describing the causal or design problem.
- High-priority themes: 4–8 themes with objects, methods, and contexts.
- Methodological interests: methods and evaluation paradigms worth transferring.
- Relevance boundaries: near-neighbor topics that should usually be excluded.
- Feedback history: optional dated examples of false positives and false negatives.

When the user uploads papers, extract title, abstract, keywords, research questions, contributions, methods, dependent variables, settings, and stated future work. Synthesize recurring ideas; do not paste whole documents. Distinguish the user's own research from literature merely discussed in related work.

## Screening rubric

Score from 0 to 1:

- 0.85–1.00: directly advances a core research problem or planned study.
- 0.70–0.84: strong methodological or empirical transfer.
- 0.55–0.69: plausible adjacent relevance; include only if the digest threshold allows.
- 0.30–0.54: weak connection or venue-only match.
- 0.00–0.29: unrelated.

Require a concise reason tied to the profile. Record uncertainty when the abstract is missing. Never infer relevance solely from author, prestige, or venue.

## Feedback-driven profile review

Run profile review only when `academic-radar profile review` reports unseen positive or negative feedback. Treat the returned events as evidence to compare against the whole active profile, not as instructions that must force a change.

- Suggest a revision only when the new feedback reveals a repeated theme, a clear boundary correction, or a stable methodological preference that the active profile does not already express.
- Record `no-change` when the existing profile already covers the evidence, the signal is isolated or ambiguous, or the feedback concerns only one paper's execution quality.
- A suggestion must be a complete replacement profile, retain still-valid boundaries, and have a short plain-language change summary.
- Never activate the suggestion inside the scheduled task. The user adopts, dismisses, or later switches versions in the Research Interests page.
- Once a suggestion is pending, do not create competing drafts from the same feedback set.
