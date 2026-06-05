Add `dns_label` validator for single DNS label validation

## What this PR does
This PR introduces a new built-in validation tag, `dns_label`, to validate strings as single DNS labels according to RFC 1123. This validator ensures the string starts and ends with an alphanumeric character, contains only alphanumeric characters or hyphens, and has a length between 1 and 63 characters, strictly forbidding dots. It addresses the need for validating single-part names common in subdomain prefixes or Kubernetes resource names.

## Changes made
*   `baked_in.go`: Registered the new `dns_label` validator and implemented its corresponding `isDNSLabel` function.
*   `doc.go`: Added documentation for the new `dns_label` validator, explaining its rules and usage.
*   `regexes.go`: Defined the regular expression `dnsLabelRegexString` and its compiled version `dnsLabelRegex` for the `dns_label` validator.
*   `validator_test.go`: Added `TestDNSLabelValidation` with comprehensive test cases to verify the correct behavior of the `dns_label` validator.

## Testing
Added new test cases in `TestDNSLabelValidation` within `validator_test.go` to cover valid and invalid scenarios for the `dns_label` validator.

Build result: `True`
Tests result: `True`
Vet result: `True`

## Related issue
Closes #1543