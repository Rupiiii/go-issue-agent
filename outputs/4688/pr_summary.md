fix(render): encode non-BMP characters as surrogate pairs in AsciiJSON

## What this PR does

This PR fixes a data corruption bug in `render.AsciiJSON` where Unicode characters outside the Basic Multilingual Plane (e.g., emoji) were incorrectly escaped. The renderer now correctly encodes these characters as a UTF-16 surrogate pair, ensuring the generated JSON is valid and can be deserialized without data loss.

## Changes made

*   `render/json.go`: Modified `AsciiJSON.Render` to detect runes with code points above U+FFFF and encode them as a proper UTF-16 surrogate pair (`\uXXXX\uYYYY`).
*   `render/render_test.go`: Added a new test case to `TestRenderAsciiJSON` that renders a map containing an emoji, asserts the output contains the correct surrogate pair, and verifies the data round-trips correctly after unmarshaling.

## Testing

A new unit test was added to verify that non-BMP characters are correctly serialized and can be deserialized back to their original value. The build, all tests, and vet checks pass.

## Related issue

Closes #4688