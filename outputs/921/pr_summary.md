Fix: Make MarkFlagRequired aware of inherited flags

## What this PR does
This PR fixes an issue where `MarkFlagRequired` and `MarkPersistentFlagRequired` failed to find flags inherited from parent commands. By ensuring persistent flags are merged before the lookup, these functions can now correctly mark inherited flags as required on subcommands. This makes the behavior consistent and avoids the need for workarounds.

## Changes made
*   `shell_completions.go`:
    *   In `MarkFlagRequired`, added a call to `c.mergePersistentFlags()` to ensure the command's flag set includes flags from parent commands.
    *   Modified `MarkPersistentFlagRequired` to call the updated `MarkFlagRequired`, unifying the logic and applying the fix to persistent flags as well.
*   `command_test.go`:
    *   Added the new test `TestMarkInheritedFlagRequired` to validate that a subcommand can successfully mark a persistent flag from its parent as required.

## Testing
A new test case, `TestMarkInheritedFlagRequired`, was added to verify the fix. The build, tests, and vet all passed.

## Related issue
Closes #921