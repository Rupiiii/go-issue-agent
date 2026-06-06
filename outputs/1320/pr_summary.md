Fix: prevent panic in excluded_if with nil parameter

## What this PR does
This PR fixes a panic that occurs when using the `excluded_if` validation tag with a `nil` parameter on a pointer field. The underlying code was attempting to parse the string "nil" as an integer, causing a `strconv` panic. The fix introduces a specific check for the "nil" keyword to correctly evaluate if the target field is nil.

## Changes made
*   `baked_in.go`: Modified the `requireCheckFieldValue` function to check if the comparison value is the string "nil". If it is, the function now correctly checks if the target field's value `IsNil()` instead of attempting a string-to-type conversion.
*   `baked_in_test.go`: Added a new test case to reproduce the panic with `excluded_if` on a pointer field compared to `nil`, ensuring the fix is effective and preventing regressions.

## Testing
A new test case was added to cover the panic scenario. The build, tests, and vet commands all pass.
*   build: True
*   tests: True
*   vet: True

## Related issue
Closes #1320