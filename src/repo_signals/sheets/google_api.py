"""Every Google endpoint, JWT claim and response field name the sheets package uses.

They are kept in this one block so they can be checked in one place. Some were checked live on
2026-10-22 and the rest are unchecked general knowledge; the block comment says which.
``auth.py`` and ``client.py`` import from here and hard-code nothing else.
"""

# --- Google API details -------------------------------------------------------------------
# Checked live on 2026-10-22: the token exchange (TOKEN_URL with a form-encoded POST, SCOPE,
# JWT_HEADER and the claims with a 3600-second lifetime, the grant type and assertion fields,
# RESP_ACCESS_TOKEN and the key file fields), signing with openssl, the header check and the
# duplicate skip (SHEETS_HOST, VALUES_URL with a GET on the quoted Sheet1 range, RESP_VALUES for
# a range with rows) and the append (APPEND_SUFFIX, RAW, INSERT_ROWS, BODY_VALUES).
# Still unchecked: RESP_EXPIRES_IN, RESP_TOKEN_TYPE, the header write on an empty sheet (a PUT
# with BODY_RANGE and BODY_MAJOR_DIMENSION), RESP_VALUES being absent for an empty range, and
# the status codes for an unshared sheet and for rate limiting.
# For every unchecked item: verify against Google's current documentation before first live use.
TOKEN_HOST = "oauth2.googleapis.com"
SHEETS_HOST = "sheets.googleapis.com"
ALLOWED_HOSTS = frozenset({SHEETS_HOST, TOKEN_HOST})
TOKEN_URL = "https://oauth2.googleapis.com/token"     # POST, form-encoded
SCOPE = "https://www.googleapis.com/auth/spreadsheets"
JWT_HEADER = {"alg": "RS256", "typ": "JWT"}
CLAIM_ISSUER = "iss"              # the service account's client_email
CLAIM_SCOPE = "scope"
CLAIM_AUDIENCE = "aud"            # TOKEN_URL
CLAIM_ISSUED_AT = "iat"
CLAIM_EXPIRES_AT = "exp"
JWT_LIFETIME_SECONDS = 3600
FORM_GRANT_TYPE = "grant_type"
GRANT_TYPE_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
FORM_ASSERTION = "assertion"
RESP_ACCESS_TOKEN = "access_token"
RESP_EXPIRES_IN = "expires_in"
RESP_TOKEN_TYPE = "token_type"    # "Bearer"
CRED_TYPE = "type"
CRED_TYPE_SERVICE_ACCOUNT = "service_account"
CRED_CLIENT_EMAIL = "client_email"
CRED_PRIVATE_KEY = "private_key"
REQUIRED_CRED_KEYS = (CRED_TYPE, CRED_CLIENT_EMAIL, CRED_PRIVATE_KEY)
# Sheets API v4 "values" endpoints. Only read, append and update are used: never clear,
# delete or any structural change.
VALUES_URL = "https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{range}"
APPEND_SUFFIX = ":append"         # POST VALUES_URL + APPEND_SUFFIX
PARAM_VALUE_INPUT_OPTION = "valueInputOption"
VALUE_INPUT_RAW = "RAW"           # values are stored as typed, never parsed as formulas
PARAM_INSERT_DATA_OPTION = "insertDataOption"
INSERT_ROWS = "INSERT_ROWS"       # append inserts new rows, never overwrites
BODY_VALUES = "values"
BODY_RANGE = "range"
BODY_MAJOR_DIMENSION = "majorDimension"
ROWS_DIMENSION = "ROWS"
RESP_VALUES = "values"            # absent when the range is empty
# --------------------------------------------------------------------------------------------
