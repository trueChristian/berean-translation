# Translation runtime verification — 23 September 2026

## Exact verified revisions

- Translation implementation: `7b091df122b4d38b1e353f0ed1ecc7f2215fa20b`.
- Repaired authoritative English archive: `62e7fd23b886a01da2755240e7a6c7ee28174bda`.
- GitHub-hosted verification: https://github.com/trueChristian/berean-translation/actions/runs/35846360662
- Source repair PR: https://github.com/trueChristian/berean-voice/pull/50
- Source repair validation: https://github.com/trueChristian/berean-voice/actions/runs/35843806469

The verification run installed the official OpenAI SDK and test dependencies, ran **75 tests with no failures or skips**, validated the translation repository, discovered the real repaired source over HTTPS, and validated **all 492 article snapshots across 44 issues**, including their raw HTML hashes, supported structure and shared image references. The artifact `repaired-source-compatibility` contains the test output and JSON reports.

## Repairs included

1. The collector supplies its explicit bot identity during a concurrent Git rebase, not only during its original checkpoint commit. A real local Git regression test exercises a runner without a configured user identity.
2. The translation reader accepts source sidebars, article footers and valid table-header scope attributes while preserving their exact structural positions. These remain part of the article, distinct from the application-owned translation notice.
3. Existing well-formed HTML comments are immutable, non-visible source metadata. They are not translated, treated as article prose, or followed as instructions. Adding, removing, moving or changing comments fails structural validation. The translation prompt states this explicitly; its version is now `1.0.1`.
4. Article/notice splitting uses parsed HTML boundaries instead of matching the first closing-tag string. A lookalike closing tag inside a comment or quoted attribute cannot end the article prematurely.
5. Permanent CI now checks every article from the exact source revision it discovered, not just the source index and manifest. The checkout validator uses a fresh cache so old cached bytes cannot conceal changed source files.

The temporary read-only repaired-source verification workflow was removed after success. The four production workflows remain. Removing that temporary workflow and adding this report do not alter the tested application, prompts, configuration or tests.

## Merge-order dependency

The ordinary live-source check intentionally reads `berean-voice/main`. At the time of the recorded test, the corrected source projections were still on source PR #50 rather than `main`. Consequently, the ordinary check continued to reject the old inconsistent source revision. The successful pinned-source run proves the corrected pair works; it does not claim that the then-current English `main` was valid.

Merge source PR #50 first, rerun the translation PR's current checks, then merge translation PR #1 after those checks pass. Do not bypass source hashes, silently substitute an older source, or configure production to consume the repair branch. No source article wording, image bytes, canonical identities or rights decisions were changed by the projection repair.

## Boundaries of verification

No real OpenAI translation request, paid Batch submission, human language review or theological quality benchmark was performed. The workflow secret is named `OPENAI_API_KEY`; its presence or account permissions are not established by an offline test. The initial successful live translation still needs a manually authorized issue/language selection and budget after merge. Neither PR was merged by this implementation work.
