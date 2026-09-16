These three Python modules are unmodified snapshots from commit `c1ad5a9`
(the PR #12 Screen Time runtime):

- `src/personal_data_platform/sources/screen_time/event_state.py`
- `src/personal_data_platform/sources/screen_time/ingestion.py`
- `src/personal_data_platform/sources/screen_time/checkpoint.py`

The migration integration tests load them under an isolated module name and
connect their original coordinator to the transactional warehouse writer. This
produces actual format-1 SQLite checkpoints, event updates, success receipts,
and revision markers without keeping the old decision logic in production.
