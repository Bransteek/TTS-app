# Task 19a report: lector/services/recovery.py

## Implemented
- `recovery.check_file(doc, stored_path, stored_stat=None) -> Path` (sync): raises FileMovedOrChanged if missing/unreadable, or if (size, mtime) differ from stored_stat AND chunked SHA-256 != doc.file_hash. Equal size+mtime skips the hash. If doc.file_hash is empty, an existing file passes (same as before).
- `recovery.relocate(doc, new_path) -> Document` (sync): chunked SHA-256 must equal doc.file_hash, else (or unreadable) raises `FileMovedOrChanged` (user_message in Spanish). Returns `replace(doc, origin=str(new_path))`; does NOT persist.
- `recovery.last_session(repos) -> ProgressRecord | None` (sync): first row with progress from `repos.documents.list_recent(50)` (ordered by last_read_at).
- Single implementation: `document_opener.check_file(doc, stored_stat=None)` stays async and delegates via `asyncio.to_thread` to `recovery.check_file`; behaviour preserved. `reading_controller._session_for` now fetches the stored stat (`documents.file_stat`) and passes it, so resume skips hashing when the file is untouched.
- Size/mtime: `Document` does not carry them, but `documents` table stores file_size/file_mtime (written by `store_opened`). Added `DocumentRepo.file_stat(doc_id)` and `DocumentRepo.update_origin(doc_id, origin, size, mtime)` (updates documents + progress.origin). Without stored_stat the rule degrades to "missing, or hash differs".

## Tests
tests/test_recovery.py: 7 tests (the 5 required + fast-path/touched-file + repo methods). Full suite: 535 passed, no warnings.
TDD note: the implementation was written before the tests were run; no separate RED run was captured.

## Notes for main.py
- Last session: `rec = await asyncio.to_thread(recovery.last_session, repos)`; if rec, show "Continuar {rec.title} — {chapter}" (rec.position.chapter_locator / controller chapter title). Do not play automatically; on click use `controller.play(rec.doc_id)`.
- Relocate (after FileMovedOrChanged from play/open): user picks file, then
  `doc = await asyncio.to_thread(repos.documents.get, doc_id)`;
  `new = await asyncio.to_thread(recovery.relocate, doc, path)` (catch FileMovedOrChanged and show its user_message);
  `st = Path(path).stat()`; `await asyncio.to_thread(repos.documents.update_origin, doc_id, new.origin, st.st_size, st.st_mtime)`; then retry play.

## Fix round 1
- `ProgressRepo.latest()` (ORDER BY last_read_at DESC LIMIT 1); `recovery.last_session` uses it (no more 50-row window). Test with 60 newer unread documents.
- Added tests isolating each half of the stat rule (same mtime + changed size/content detected; same size + changed mtime + same content accepted; same size + changed mtime + different content detected) and the empty `file_hash` path.
- `DocumentRepo.update_file_stat`; `document_opener.check_file(doc, stored_stat, documents=None)` refreshes size/mtime via `asyncio.to_thread` after a successful check when the stat differs (failures to refresh are logged, never block opening). `reading_controller` passes `self._repos.documents` (one-line change). Tests: refresh on success, no refresh on failure.
- `recovery.relocate_and_store(repos, doc, new_path) -> Document` (sync): relocate, then `update_origin` with fresh stat; nothing persisted if validation fails. Tests added.
- RED (before implementation): `pytest tests/test_recovery.py` -> 6 failed, 11 passed (latest, >50 unread, relocate_and_store x2, refresh x2).
- GREEN: `.venv/bin/pytest -q` -> 556 passed, no warnings.
