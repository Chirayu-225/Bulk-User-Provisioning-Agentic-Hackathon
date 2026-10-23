# Test case results

| Test | Scenario | Expected | Verdict | Detail |
|---|---|---|---|---|
| TC1 | Approved CSV of 20 users | Users created with mapped roles | PASS | all checks passed |
| TC2 | Same email twice in CSV | One user created | PASS | all checks passed |
| TC3 | Console defaults new users to Admin | Corrected before save | PASS | all checks passed |
| TC4 | Batch fails at user 12 | Retry creates only the missing users | PASS | all checks passed |
