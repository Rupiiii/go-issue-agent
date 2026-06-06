Exclude hidden commands from usage padding calculation

## What this PR does
This PR fixes an issue where hidden commands were incorrectly included when calculating the maximum string lengths for usage message formatting. This could lead to unnecessarily wide padding in the help output. The change ensures that only visible commands are considered, resulting in a more compact and correctly formatted usage display.

## Changes made
*   `command.go`: Modified `AddCommand` and `RemoveCommand` to exclude hidden commands from the calculations that determine padding for the usage message.

## Testing
The existing test suite passes. All build, test, and vet checks were successful.

## Related issue
Closes #1272