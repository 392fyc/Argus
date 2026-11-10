---
name: pr-flow
description: |
  Automate the PR lifecycle with the Argus review bot, which only gives advice: create PR, read Argus's suggestions, fix or answer each one, push, and merge under the repository's current merge rules (Argus never approves and never blocks). Use this skill when the user says "PR", "pull request", "create PR", "merge PR", "提PR", "合并", "PR流程", "开PR", "check PR status", "review comments", "标准PR流程". Use this skill after dev work reaches implementation-complete and the branch is pushed. Works in any GitHub repo that has Argus review bot wired up.
user-invocable: true
allowed-tools: Bash, Read, Grep, Glob, Edit, Write, WebSearch, WebFetch, Agent, TodoWrite
---

# PR Flow — Argus-Compatible Sequential Protocol

Every phase has a **GATE** that MUST pass before proceeding. Do NOT skip gates.

## Argus Behavior Model

Understand these Argus capabilities before executing:

- **Advice only**: every Argus review is a GitHub COMMENT. Argus never requests changes and never approves, so it neither blocks nor unblocks a merge. There is no APPROVED state to wait for.
- **Whole-PR review**: every review, including the one triggered by a push, reads the whole PR diff, not just the newest commit. A new push produces a fresh review of the whole PR.
- **Fewer, firmer findings**: only medium-or-higher findings are posted, and a finding that repeats an existing thread (resolved by a person, answered, or still open) is not posted again.
- **Fix-detection resolve**: Argus compares a new commit against open threads by file+line and resolves threads whose lines changed. This says the lines were edited, not that the problem is fixed; a still-unfixed problem may come back as a new finding.
- **Reply-aware resolution**: when a thread has a human/agent reply, Argus uses an LLM to classify it: ACCEPT (resolve + ack), REJECT (keep open + follow-up), ESCALATE (mark for human). Max 3 reply rounds per thread.
- **Spend cap**: when the monthly budget is used up Argus comments once and stops reviewing until the next reset; that is not a failure of the PR.

**Agent behavioral rules:**

| Rule | Detail |
|------|--------|
| **禁止手动 resolve thread** | Threads are resolved by Argus fix-detection or reply-aware resolution |
| **禁止回复 fix comments** | Don't reply "Fixed in xxx" — the diff is the explanation |
| **不轮询、不睡眠等待** | Argus reviews arrive on their own after a push. Check once with a single `gh` call when you need the result (for example when the user asks); do not create cron jobs or sleep loops |
| **只在 disagree 时回复** | Reply only when NOT fixing — Argus LLM will classify the reply |
| **建议不是门槛** | Treat Argus findings as input to decide on, not as a gate; the merge follows the repository's own rules |

## Variables

```bash
PR_NUMBER=<number>
PR_URL=<url>
# Auto-detect repo + base branch — works in any GitHub repo, not just Mercury.
OWNER=$(gh repo view --json owner --jq '.owner.login')
REPO_NAME=$(gh repo view --json name --jq '.name')
BASE_BRANCH=$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name')
# Argus PRs target the default branch (master); the dormant develop branch is never a base.
ITERATION=0
MAX_ITERATIONS=5
```

## Phase 1: Create PR

**MANDATORY**: Push branch, then create PR with metadata.

```bash
BRANCH=$(git branch --show-current)
# Git writes go through Mercury's git-safe pack (see CLAUDE.md, Branching).
powershell -ExecutionPolicy Bypass -File .mercury/packs/git-safe/scripts/git-safe.ps1 push -u origin "$BRANCH"

# Assignee: prefer the authenticated user so this works in any repo, not just 392fyc's.
# Fall back to @me if the API call fails.
ASSIGNEE=$(gh api user --jq '.login' 2>/dev/null || echo "@me")

# Labels: only apply labels that actually exist in the target repo. Unknown labels fail the PR create.
# To add labels, extend this array and ensure each exists in the target repo first.
LABEL_ARG=""
for L in "enhancement"; do
  if MSYS_NO_PATHCONV=1 gh api "repos/${OWNER}/${REPO_NAME}/labels/${L}" --silent 2>/dev/null; then
    LABEL_ARG="${LABEL_ARG}${LABEL_ARG:+,}${L}"
  fi
done

gh pr create --base "$BASE_BRANCH" \
  --title "<type>(<scope>): description (#issue)" \
  --body "$(cat <<'BODY'
## Summary
- bullet points

## Test plan
- [ ] test items

Generated with Claude Code
BODY
)" \
  --assignee "$ASSIGNEE" \
  ${LABEL_ARG:+--label "$LABEL_ARG"}
```

**GATE 1**: PR created. Extract and store `PR_NUMBER` and `PR_URL`.

## Phase 2: Read the Initial Review

Argus reviews a new PR on its own. **Do not poll and do not sleep.** Check once:

```bash
gh pr view <PR_NUMBER> --json reviews,comments
```

If Argus has not reviewed yet, tell the user the PR is open and that the review will appear; come back when asked. If the only Argus message says the monthly budget is used up, there is no review this period: report that to the user and continue under the repository's merge rules without it.

**GATE 2**: Argus's review is in, or the user has been told it is pending or unavailable.

## Phase 3: Read + Triage ALL Findings

**MANDATORY**: Read every finding before fixing anything.

### Step 3a: Fetch all inline comments

```bash
MSYS_NO_PATHCONV=1 gh api "repos/${OWNER}/${REPO_NAME}/pulls/${PR_NUMBER}/comments" \
  --jq '.[] | select(.user.login == "argus-review[bot]") | {id, path, line, body}'
```

### Step 3b: Fetch review body

```bash
MSYS_NO_PATHCONV=1 gh api "repos/${OWNER}/${REPO_NAME}/pulls/${PR_NUMBER}/reviews" \
  --jq '.[] | select(.user.login == "argus-review[bot]") | {id, state, body}'
```

### Step 3c: Parse Argus comment format

Each inline comment has this structure:

```
_<SEVERITY_EMOJI> <Severity>_ | _<Category>_ [| importance: N/10]

**<Description>**

<details><summary>📝 Suggestion</summary>
```code
<suggested fix>
```
</details>

<details><summary>📝 Committable suggestion</summary>
```suggestion
<ready-to-apply code>
```
</details>

<details><summary>🤖 Prompt for AI Agents</summary>
```text
In file `<path>` around lines <N>-<N>:
<machine-readable description>
```
</details>
```

**Severity levels**:
- `🔴 Critical` / `importance: 9-10` → MUST fix
- `🟡 Medium` / `importance: 7-8` → SHOULD fix unless strong reason to disagree
- `🔵 Minor` / `importance: 1-6` → Fix if trivial, explain if opinionated

### Step 3d: Build triage list

For each finding, decide: `fix` or `disagree`

**GATE 3**: All findings enumerated with action decisions.

## Phase 4: Fix Code

For each finding marked `fix`:

### Step 4a: Locate and read the code

Use `🤖 Prompt for AI Agents` block or `path:line` to find exact location.

### Step 4b: Apply fix

| Severity | Committable suggestion? | Action |
|----------|------------------------|--------|
| 🔴 Critical | Yes | Apply the suggestion |
| 🔴 Critical | No | Read context, write fix |
| 🟡 Medium | Yes | Apply unless incorrect |
| 🟡 Medium | No | Fix based on description |
| 🔵 Minor | Any | Fix if trivial (<5 lines) |

### Step 4c: Handle disagree findings

For findings marked `disagree`: reply with reasoning. Do NOT resolve.

```bash
MSYS_NO_PATHCONV=1 gh api -X POST \
  "repos/${OWNER}/${REPO_NAME}/pulls/${PR_NUMBER}/comments/<COMMENT_ID>/replies" \
  -f body="<reasoning why this is by design or out of scope>"
```

Argus will LLM-classify the reply:
- **ACCEPT** → Argus resolves the thread automatically
- **REJECT** → Argus posts follow-up, thread stays open → read follow-up, decide again
- **ESCALATE** → Thread marked for human intervention → stop processing this thread

**GATE 4**: All `fix` items have code changes. All `disagree` items have reply posted.

## Phase 5: Push and Wait

### Step 5a: Commit and push

```bash
GS=.mercury/packs/git-safe/scripts
powershell -ExecutionPolicy Bypass -File $GS/git-safe.ps1 add <changed-files>
git diff --cached          # inspect what is staged; run /dual-verify and act on its result
powershell -ExecutionPolicy Bypass -File $GS/guard.ps1 mark-review
powershell -ExecutionPolicy Bypass -File $GS/git-safe.ps1 commit -Message "fix: address review feedback — <summary> (#issue)"
powershell -ExecutionPolicy Bypass -File $GS/git-safe.ps1 push origin "$(git branch --show-current)"
```

### Step 5b: Look at the new review (once)

**MANDATORY**: Do NOT resolve threads. Do NOT post re-review triggers. Do NOT poll or sleep.

After a push Argus re-reviews the whole PR by itself. When the user asks for the result (or at your next natural step), look once:

```bash
gh pr view <PR_NUMBER> --json reviews,comments
MSYS_NO_PATHCONV=1 gh api "repos/${OWNER}/${REPO_NAME}/pulls/${PR_NUMBER}/reviews" \
  --jq '.[] | select(.user.login == "argus-review[bot]") | {id, state, submitted_at, commit_id}'
```

If no review for the pushed commit exists yet, report that it is still pending.

### Step 5c: Process the review

Argus reviews are always `COMMENTED`; read the findings instead of a state:
- **New findings** → Increment ITERATION, return to Phase 3.
- **No new findings** → Proceed to Phase 6.

```bash
ITERATION=$((ITERATION + 1))
if [ "$ITERATION" -ge "$MAX_ITERATIONS" ]; then
  echo "Max iterations reached. Requesting human intervention."
  gh pr comment "$PR_NUMBER" --body "Max review iterations ($MAX_ITERATIONS) reached. Requesting human guidance."
  # STOP — do not continue
fi
```

**GATE 5**: The review for the pushed commit has been read (or reported pending), and the action is decided.

## Phase 6: Merge

Merge follows the repository's current rules (see its CLAUDE.md/AGENTS.md): the PR goes to the trunk by pull request, and the human reviewer decides. Argus does not approve, so there is no `reviewDecision == APPROVED` to check. Check:

```bash
# 1. CI passes
gh pr checks "$PR_NUMBER"

# 2. Every Argus finding is dealt with (fixed, or answered), and the threads still open
#    are listed for the user to disposition
MSYS_NO_PATHCONV=1 gh api graphql -f query="
query {
  repository(owner: \"${OWNER}\", name: \"${REPO_NAME}\") {
    pullRequest(number: ${PR_NUMBER}) {
      reviewThreads(first: 100) { nodes { isResolved path comments(first: 1) { nodes { author { login } body } } } }
    }
  }
}" --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved==false) | {path, body: .comments.nodes[0].body[0:120]}'
```

**GATE 6**: CI is green and the open threads were reported to the user. Ask the user to confirm the merge; then:

```bash
gh pr merge "$PR_NUMBER" --squash --delete-branch
```

## Phase 7: Cleanup

```bash
rm -f .pr-flow-iteration-* .pr-flow-check-count-* .pr-flow-multi.txt

BRANCH=$(gh pr view "$PR_NUMBER" --json headRefName --jq '.headRefName')

# Snapshot worktree paths BEFORE switching branch (switch changes main repo's association)
# Identify main worktree to exclude it from cleanup
WT_FAIL=0
MAIN_WT=$(git rev-parse --show-toplevel)
WT_LIST=$(git worktree list --porcelain 2>&1) || {
  echo "WARNING: git worktree list failed — skipping worktree and branch cleanup"
  WT_FAIL=1
}
WT_PATHS=""
if [ "$WT_FAIL" -eq 0 ]; then
  WT_PATHS=$(echo "$WT_LIST" | awk -v ref="branch refs/heads/$BRANCH" '
    /^worktree / { path = substr($0, 10) }
    $0 == ref { print path }
  ')
fi

# Switch off branch (must happen before branch deletion).
# Reuse BASE_BRANCH computed at the start of the skill (the default branch, master).
if [ "$(git rev-parse --abbrev-ref HEAD)" = "$BRANCH" ]; then
  if ! git switch "$BASE_BRANCH" 2>/dev/null && ! git checkout "$BASE_BRANCH" 2>/dev/null; then
    echo "WARNING: failed to switch off branch $BRANCH to $BASE_BRANCH — skipping cleanup"
    WT_FAIL=1
  fi
fi

# Clean up worktree(s) using pre-switch snapshot
if [ "$WT_FAIL" -eq 0 ]; then
  while IFS= read -r wt_path; do
    [ -z "$wt_path" ] && continue
    # Skip main worktree — only remove additional worktrees
    [ "$wt_path" = "$MAIN_WT" ] && continue
    if ! git -C "$wt_path" status --porcelain >/dev/null 2>&1; then
      echo "WARNING: worktree $wt_path is inaccessible — pruning"
      git worktree prune --expire=now || { WT_FAIL=1; continue; }
      # Verify prune removed THIS specific path (not just any branch association)
      if git worktree list --porcelain | grep -Fq "worktree $wt_path"; then
        echo "WARNING: worktree $wt_path still registered after prune"
        WT_FAIL=1
      fi
    elif [ -n "$(git -C "$wt_path" status --porcelain)" ]; then
      echo "WARNING: worktree $wt_path has uncommitted changes — skipping"
      WT_FAIL=1
    else
      git worktree remove "$wt_path" || WT_FAIL=1
    fi
  done <<< "$WT_PATHS"
fi

# Delete local branch only if all worktree cleanup succeeded
if [ "$WT_FAIL" -eq 0 ]; then
  git branch -d "$BRANCH" || echo "NOTE: local branch $BRANCH could not be deleted"
else
  echo "Skipping branch deletion — worktree cleanup incomplete"
fi
```

Update related issues and Mercury task state if applicable.

## Output

After each phase, report status:

```text
PR: #<number> (<url>)
Review: argus commented | pending | budget used up (advice only, never a gate)
Threads: <total> total, <resolved> resolved, <open> open
Iteration: <N>/<MAX>
Merge: merged | waiting | blocked (<reason>)
```
