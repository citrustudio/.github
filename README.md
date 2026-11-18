# .github

Community health files and the public-text and DCO check for the citrustudio organization.

# Public history checks

The reusable workflow checks added text in every pull request commit, commit messages,
commit author and committer identities, the pull request title and body, and DCO
trailers. It reports categories only: work item numbers, local paths, session links, the
private provenance trailer, seat names, non-noreply email addresses and private literals. When a commit carries an `Agent-Run` trailer, the pull
request body must repeat that line, because a squash merge can keep only the pull request text. For
same-repository pull requests configure the organization secret `PUBLIC_TEXT_DENY`
as newline-separated private literals; missing or empty configuration fails the check.
Fork pull requests receive only the public-pattern tier because secrets are unavailable;
reviewers must review the private tier separately. Binary changes require manual review.

The DCO check requires a matching `Signed-off-by` trailer for human commits. A commit
whose author GitHub identifies as the agents App requires an `Assisted-by` trailer
instead. Agent commits end with `Assisted-by: Claude:<model>` (or `Codex:`, `Agy:`), then
`Agent-Effort:` and `Agent-Run:` when the launch sets them; an `unknown` effort is left out and an `unknown` model is refused. GitHub's author mapping is not cryptographic proof of authorship. The
owner's required pull request review provides the human attestation.

Install the hooks in a public checkout after cloning this repository to a trusted
location. Private repositories keep their own provenance hooks; pointing a public checkout
here is what switches it to the public trailers.

```sh
git config --local core.hooksPath /absolute/path/to/organization-config/hooks
```

The `commit-msg` hook refuses a message or author and committer identity that fails either
tier, before the commit exists. Comment lines are skipped only when git's editor comment block is present, because only
then does git drop them; a `-m` message is checked whole. For an agent launch (`AGENT_ENGINE`, `AGENT_MODEL`, and
optionally `AGENT_EFFORT` and `AGENT_RUN` in the environment) it then appends the trailers
above; an amend keeps the ones the commit already has. The `pre-push` hook scans both tiers
for every push from a checkout where it is installed, including remotes configured through
SSH aliases.
Create `~/.config/citrustudio/public-text-deny` as a private, nonempty UTF-8 file
with one sensitive literal per line and mode 600. Keep it outside all repositories.
It must include sensitive hosts, account identifiers, vault names and personal
information. The first push scans all history; start public projects with fresh history.
The workflow and the hook do not replace secret scanning or required owner review.
