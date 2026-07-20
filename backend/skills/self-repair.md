---
name: self-repair
description: Iron laws for recovering from failed tool calls — read the error, one hypothesis at a time, different approach each retry, 3 strikes then report.
---

# Self-Repair — when a tool call fails

Iron laws (no exceptions):

1. **READ THE ERROR FIRST.** The root cause is usually stated verbatim
   ("No such file", "command not found", "not allowed", "-1743").
   Never attempt a fix for a failure you haven't read in full.
2. **ONE HYPOTHESIS AT A TIME.** Name the suspected root cause, run the
   SMALLEST test or fix for it, observe. No shotgun fixes changing three
   things at once — you learn nothing from those.
3. **DIFFERENT APPROACH AFTER EVERY FAILURE.** Change something material:
   - another tool: bash ↔ applescript ↔ click_ui ↔ see_screen
   - install the missing dependency (confirmation is automatic)
   - an alternate CLI or flag that does the same job
   NEVER re-issue an identical failing call — same input, same failure.
4. **3 STRIKES → STOP.** If the same step has failed 3 times, stop and tell
   the user exactly what is blocking and what you tried. A clear blocker
   report beats a fourth guess.

Debug loop: root cause → hypothesis → minimal test → fix → verify from tool output.

Instant fixes for common errors:
- "command not found"      → `brew install <tool>` or a preinstalled equivalent
- "No such file/directory" → list the parent directory first; never guess a path twice
- "-1743" / "not allowed"  → macOS permission missing; tell the user which Settings pane
- timeout                  → smaller step, longer timeout_s, or background=true
