## Summary

_One or two sentences: what does this pull request do?_

## What changed and why

_What you changed and which problem it solves. Mention the issue if there is one._

## Sources

_The public documentation you consulted (title and section), or the part of
`docs/FORMATS.md` you worked from. Write "none beyond this repository" if that is the
case._

- [ ] No code was copied, translated or adapted from another project, whatever its
  license. No third-party code was given to an AI assistant to transcribe.
- [ ] New facts about a file format are recorded in `docs/FORMATS.md` with their public
  source. (Or: this change brings no such facts.)

## Validation

_List only the commands that you actually ran, with their real results. Write "not run"
and the reason for anything you skipped._

| Command | Result |
| :-- | :-- |
| `uv run pytest` | |
| `uv run ruff check .` | |
| `uv run ruff format --check .` | |
| `uv run mypy` | |

_Anything else you ran or measured, and what you did not test:_

## Checklist

- [ ] This diff contains no game content: no game files, extracted files, dumps, keys,
  firmware, captures, disassembly or decompiler output. The same is true of the commit
  messages and of this description.
- [ ] Tests use synthetic data only, and the new tests fail without this change.
- [ ] Profiles contain no original game bytes: every patch site is verified through
  `expected_sha256`, and I wrote the replacement bytes myself. (Or: no profile is
  touched.)
- [ ] For a profile: I analyzed a copy of the game that I lawfully own, every patch has a
  description in my own words, and `NOTES.md` records the hardware, the game version and
  what was tested. (Or: no profile is touched.)
- [ ] Every commit is signed off (`git commit -s`) and follows Conventional Commits.
- [ ] The documentation is updated: the documents that describe the changed behavior, and
  `THIRD_PARTY_NOTICES.md` for a changed dependency. (Or: no update is needed.)
- [ ] I reviewed the final diff myself and I am responsible for it. If an AI assistant
  helped, I ticked these boxes, not the assistant.

The rules behind this checklist are in `CONTRIBUTING.md`.
