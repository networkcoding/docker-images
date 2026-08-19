---
name: cpp-merge-request-review
description: Review C and C++ merge-request diffs for actionable defects in ownership and lifetime, RAII, iterator validity, concurrency and lock ordering, undefined behavior, and logical regressions. Use for automated or interactive C/C++ code review where findings must be evidence-based and formatted for a merge-request comment.
---

# Review a C++ merge request

Review only defects introduced or exposed by the merge-request diff. Read surrounding implementations, callers, tests, and relevant history when needed to establish impact. Do not modify files.

## Checks

Prioritize:

1. Memory ownership and lifetime
   - Detect leaks, double frees, use-after-free, dangling pointers/references/views, invalid captures, ownership ambiguity, and missing RAII.
   - Check iterator, pointer, and reference invalidation after mutation, reallocation, erase, moves, callbacks, or asynchronous work.
2. Concurrency
   - Detect data races, deadlocks, inconsistent lock ordering, missing synchronization, unsafe callback re-entry, incorrect atomic ordering, and object lifetime races.
   - Trace all shared-state access and lock acquisition paths before reporting a finding.
3. Undefined behavior
   - Check bounds, invalid shifts, signed overflow, uninitialized reads, invalid casts, alignment, strict aliasing, iterator misuse, null dereferences, format mismatches, and object-lifetime violations.
4. Logical regressions
   - Compare changed behavior with existing callers, tests, invariants, protocols, error handling, and previous behavior.
   - Report changes that can break existing functionality, including edge cases, cleanup paths, state transitions, retries, timeouts, and platform-specific behavior.

Skip style-only feedback, speculative concerns without a reachable failure mode, pre-existing issues unrelated to the diff, praise, and broad refactoring suggestions.

## Validate findings

For each candidate issue:

- Identify a concrete input, state, or interleaving that triggers it.
- Verify the cited line is changed by the merge request and is the best location for the comment.
- Inspect guards, ownership transfer, synchronization, and callers that could disprove it.
- Assign `P0`, `P1`, `P2`, or `P3`; omit issues below `P3`.
- Keep line ranges minimal and recommendations concise.

## Output

Return Markdown with exactly these top-level sections:

```markdown
## Summary
<Brief description of the change and overall risk.>

## Found issues

### [P1] <Actionable title>
- **Location:** `path/to/file.cpp:123`
- **Category:** Memory lifetime | Concurrency | Undefined behavior | Logical regression
- **Impact:** <What breaks and under which conditions.>
- **Evidence:** <Concrete control flow, state, input, or interleaving.>
- **Recommendation:** <Smallest practical correction.>
```

Order issues by severity. If there are no qualifying findings, write `No issues found.` under `## Found issues`. Do not add other top-level sections.
