# Remnant website publication notifications

These notification hooks are **disabled by default**. They request a rebuild in
[`trueChristian/remnant.truechristian.church`](https://github.com/trueChristian/remnant.truechristian.church)
only after a successfully validated, durably published display snapshot changes.
The website receives `remnant-content-updated`, resolves current trusted source
branches, and revalidates exports. No article wording, translation policy, or
publication status is changed by this integration.

The receiving implementation is under review in
[website draft PR #2](https://github.com/trueChristian/remnant.truechristian.church/pull/2),
with the project contract in
[website issue #1](https://github.com/trueChristian/remnant.truechristian.church/issues/1).
The receiver must be reviewed, merged and configured before activation here.

## Owner-controlled activation

After review and separate owner authorization:

1. Securely configure `REMNANT_DISPATCH_TOKEN` as a repository Actions secret.
   Use an approved GitHub App installation token or fine-grained credential
   restricted to **the website repository only**, with `Contents: write`, as
   required by GitHub's repository-dispatch endpoint. Prefer a maintained GitHub
   App flow; short-lived tokens need an independently approved renewal/minting
   setup. An ordinary source-repository `GITHUB_TOKEN` cannot dispatch to another
   repository. This change does not create credentials or configure access.
2. Set the repository Actions variable `REMNANT_NOTIFICATIONS_ENABLED` to the
   exact string `true`. Until then every notification step is skipped.
3. Run the trusted main workflow described below and inspect its notification
   result, then verify the receiving website build separately. No source workflow
   deploys the website.

Never paste credentials into a pull request, issue, artifact, file, or command
argument. The helper reads the token only from the step environment, sends it
only to the fixed GitHub repository-dispatch API endpoint, refuses redirects,
and suppresses private response bodies and exception details.

## Delivery and recovery

The public-display fingerprint includes exported metadata, article HTML and
images. Generated manifests and revision-only metadata are excluded, so internal
checkpoints do not cause repeated notifications. Translation notifications also
fingerprint the language registry's public names, tags, direction and aliases;
internal translation guidance and runtime records are excluded. New zero-article
locales therefore request a website interface-validation build.

A restored Actions cache remembers the last accepted fingerprint. The helper
checks clean source inputs, an export manifest matching HEAD, and HEAD equal to
current remote `main` before sending. Only HTTP 204 saves a success marker. A
rejected/failed/uncertain request leaves the old marker in place. Cache loss can
cause a safe duplicate. If a newer source commit arrives while checking, rerun
against current main instead of notifying a stale revision.

Notification acceptance is not proof of website deployment. Inspect the website
workflow separately; it owns coalescing, duplicate-output handling and recovery.
After a failed notification, correct the setup/connectivity problem and rerun the
trusted source workflow. To force a website recovery when a previously accepted
notification did not produce a deployment, use the website's manual build flow.

## Offline tests

`python3 -m unittest discover -s tests -v` includes mocked notification tests. They
never send a real dispatch, use real credentials, or start paid translation work.
The copied `.github/remnant/dispatch.py` and `validate_event.py` share the versioned
website event contract. Keep them synchronized with the website implementation
when that contract changes.

References: [GitHub token behavior](https://docs.github.com/en/actions/concepts/security/github_token)
and [repository dispatch permissions](https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event).

## Translation publishing path

`AI — Collect and discover` notifies directly after `tick --publish` succeeds and
the resulting runtime records validate. It uses the same selected English
checkout for a fresh compatibility-filtered export. This covers newly completed
translations, committed human-review notice removal, and stale/withdrawn output.
The direct step is essential: runtime commits made with `GITHUB_TOKEN` normally
do not trigger another push workflow. It does not depend on that extra workflow.

Notification steps run only for collection cycles, on trusted `main`, with the
enabling variable set. Explicit `cancel` and `resolve-absent` maintenance runs
are excluded because they do not acquire an English export checkout or publish
new display content. The next collection cycle handles current display state.
For recovery, run `AI — Collect and discover`, operation `collect`, on `main`.
This resumes only previously authorized work; it does not authorize a campaign.
