fix(context): Avoid calling chmod on existing directories in SaveUploadedFile

## What this PR does
This PR fixes a regression in `SaveUploadedFile` that caused it to fail with a permission error when saving to a pre-existing directory not owned by the current user, such as `/tmp`. The function was unconditionally calling `os.Chmod` on the destination directory, even if it already existed. This change ensures `os.Chmod` is only called when the directory is newly created.

## Changes made
*   `context.go`: Modified `SaveUploadedFile` to check if the destination directory exists before calling `os.MkdirAll`. The subsequent `os.Chmod` call is now conditional and only runs if the directory did not previously exist.
*   `context_test.go`: Added the new test `TestSaveUploadedFileToExistingDir` to reproduce the issue and verify the fix. This test saves a file to a temporary directory, ensuring no permission errors occur on existing directories.

## Testing
A new regression test, `TestSaveUploadedFileToExistingDir`, was added to cover saving files to pre-existing directories.
The build, all tests, and vet checks passed successfully.

## Related issue
Closes #4622