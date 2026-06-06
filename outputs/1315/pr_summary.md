feat: Improve error message for 'required' tag on bool fields

## What this PR does
This PR improves the error message for `required` validation on `bool` fields. The new message clarifies that the validation fails because `false` is the zero-value for booleans, preventing confusion where users might think the field is missing. This provides more direct and actionable feedback.

## Changes made
*   `errors.go`: Updated the `fieldError.Error()` method to append a clarifying note to the error message when a `required` validation fails on a `bool` field.
*   `validator_test.go`: Added `TestRequiredBoolErrorMessage` to verify that the new error message is correctly produced for `bool` fields, while ensuring the error message for other types remains unchanged.

## Testing
A new unit test, `TestRequiredBoolErrorMessage`, was added to confirm the new error message for `bool` fields with the `required` tag.

Build, tests, and vet all passed.

## Related issue
Closes #1315