"""The fixed column layout. The tool owns these columns and only these, in this order."""

COLUMNS = ("name", "email", "title", "company_domain", "source", "confidence",
           "signal_evidence", "email_draft", "reviewed", "run_id", "written_at")
# Written empty on append and never written again: a person edits these.
PROTECTED_COLUMNS = ("email_draft", "reviewed")
LETTERS = tuple(chr(ord("A") + i) for i in range(len(COLUMNS)))
FIRST_COLUMN, LAST_COLUMN = LETTERS[0], LETTERS[-1]
