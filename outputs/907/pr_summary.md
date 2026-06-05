Fix: Prevent panic when numeric comparison validators encounter nil pointers

## What this PR does
This PR addresses a panic that occurred when numeric comparison validators (e.g., `gte`, `gt`, `lte`, `lt`) were applied to a nil pointer field (e.g., `*int64`). The panic happened when such a field was also marked with `omitempty` and `required_if`, and the `required_if` condition evaluated to true while the pointer was nil. The fix ensures these validators correctly handle `reflect.Ptr` types, preventing the panic.

## Changes made
*   `baked_in.go`: Added a `reflect.Ptr` case to the `isGte`, `isGt`, `isLte`, and `isLt` functions. This case checks if the pointer is nil; if so, it returns `false`. If the pointer is not nil, it dereferences the pointer to validate the underlying element.

## Testing
A new test case was added based on the provided issue reproduction code to confirm the fix.
Build: `True`
Tests: `True`
Vet: `True`

## Related issue
Closes #907