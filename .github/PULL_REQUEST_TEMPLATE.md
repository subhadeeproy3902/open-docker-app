## What does this change?

<!-- A short description. Link the issue it fixes: "Fixes #123". -->

## Why?

## How was it tested?

- [ ] `make check`
- [ ] `tests/drive.py` (and a new check for any new behaviour)
- [ ] `tests/clickall.py` reports no errors
- [ ] Screenshots before / after in **light and dark** (UI changes)

## Checklist

- [ ] No blocking calls on the UI thread (`util.bg` for Engine calls)
- [ ] No new heavy dependencies; nothing runs while idle
- [ ] README / CHANGELOG updated if users will notice the change
