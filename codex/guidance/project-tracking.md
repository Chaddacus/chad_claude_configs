# Project board updates

## Find the existing board

At the start of project work, check `spec.md`, repository documentation, and the user's context for the authoritative board and task identifiers. For Omni-mem boards, use the globally configured `omni-mem-manage` MCP server to list projects and read the matching project's overview. Match by explicit project ID or canonical repository mapping; do not select a board based on a vague name resemblance. Ask if a consequential ambiguity remains.

Use another established project tracker when that is the project's authoritative board. If there is no board, continue the work without creating one unless requested. Record a confirmed board reference in `spec.md` during authorized documentation or implementation work so future sessions can find it.

## Keep progress accurate

Reuse the task that matches the requested work. Within an existing board, create a task for substantive authorized work only when no suitable task exists. Preserve other people's assignments and progress; follow the server's assignment and ownership rules rather than impersonating another agent or taking over an active task.

For Omni-mem, inspect the live tool schemas, register the working agent and claim or assign its task as required, then call `start_project_task` when work begins. Use `report_project_task_progress` for material progress, next steps, blockers, and completion. Read the board after updates to confirm they were saved. Registration or assignment is for tracking this work, not permission to dispatch work to other people.

Update the board when work starts, a meaningful milestone is reached, a blocker appears or clears, the scope or next step changes, and when the task finishes or is handed off. Explain what now works, what remains, and how the result was verified in standard technical English. Include relevant evidence or pull-request references using supported fields. Avoid updates for every tool call, invented progress percentages, or unsupported completion estimates.

Only mark work completed after its definition of done is met. Keep blocked or unverified work visibly incomplete. Update the project-level summary when this work materially changes the overall project status, preserving unrelated work and other contributors' information.

For parallel work, the lead owns the consolidated board state. Workers report evidence to the lead unless explicitly assigned distinct board tasks; avoid competing updates to the same task.

## Keep tracking lightweight

The board records execution progress; `spec.md` records maintained application knowledge. Keep them consistent without copying full logs or creating a second planning framework. Do not include credentials or sensitive data in board updates.

If the tracker is unavailable or an update fails, retain a concise pending update, report the tracking limitation, and continue independent authorized work. Retry when practical and reconcile against the current board before applying delayed updates. Never claim a board update succeeded without confirmation. Do not silently substitute a different server or create a duplicate board.
