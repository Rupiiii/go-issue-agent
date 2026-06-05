# go-playground/validator — Code Conventions

## Adding a new built-in validator

1. **Register in `baked_in.go`**: Add an entry to the `bakedInValidators` map:
   ```go
   "your_tag": hasYourTag,
   ```

2. **Implement in `baked_in.go`**: Add the function immediately after similar validators.
   Function signature must be:
   ```go
   func hasYourTag(fl FieldLevel) bool {
       // fl.Field() gives you the reflect.Value of the field being validated
   }
   ```

3. **Document in `doc.go`**: Add a comment block explaining the tag.

4. **Test in `validator_test.go`**: Add test cases to the existing table-driven test.
   (Built-in validator tests live in `validator_test.go` on current master — there is
   no separate `baked_in_test.go`.) Find the relevant `Test...` function and add a row.
   Match the pattern of existing rows exactly (same struct fields, same assertion style).

## Naming conventions
- Tag names: lowercase, underscores allowed, e.g. `required_with`, `min`, `datetime`
- Function names: `has` + PascalCase tag name, e.g. `hasDatetime`, `hasRequiredWith`
- Test case descriptions: match the tag name exactly

## Error handling
- Return `false` to indicate validation failure — do not return errors
- Panic only for programmer errors (misconfigured tags), not invalid input

## Common patterns
- Numeric range validators: see `hasMin`, `hasMax` in `baked_in.go`
- String format validators: see `hasEmail`, `hasURL` — typically use regex
- Cross-field validators: see `hasRequiredWith`, `hasRequiredWithout`
- Struct-level validators: registered differently — check existing examples

## Do not modify
- `validator.go` (core reflection logic) — unless the issue explicitly requires it
- `cache.go` — do not touch
- `errors.go` — only if the issue is specifically about error messages

## Test running
- `go test ./... -v -run TestBakedIn` runs only the baked-in tests (faster for iteration)
- Full suite: `go test ./...`
